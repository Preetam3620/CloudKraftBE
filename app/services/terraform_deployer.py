import logging
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from app.config import RUNTIME_DATA_DIR
from app.database import SessionLocal
from app.models.deployment import Deployment, DeploymentLog
from app.schemas.workflow import WorkflowState
from app.services.terraform_generator import generate_terraform_files
from app.services.terraform_runner import (
    WARM_WORKSPACE_DIR,
    _TERRAFORM_BIN,
    _build_tf_env,
    _warm_workspace_ready,
    _strip_ansi,
)
from app.utils.security import decrypt_aws_credentials

logger = logging.getLogger(__name__)

WORKSPACES_DIR = RUNTIME_DATA_DIR / "workspaces"

_SENSITIVE_PATTERNS = [
    (re.compile(r'\b(AKIA|ASIA)[A-Z0-9]{16}\b'), "***AWS_KEY***"),
    (re.compile(r'(?i)(secret[_\-]?key|aws_secret|password|token|api[_\-]?key)\s*[=:]\s*\S+'), "***REDACTED***"),
    (re.compile(r'gAAAAA[A-Za-z0-9+/\-_]{50,}={0,2}'), "***ENCRYPTED***"),
]


def _scrub_log(message: str) -> str:
    for pattern, replacement in _SENSITIVE_PATTERNS:
        message = pattern.sub(replacement, message)
    return message


# ── DB helpers ────────────────────────────────────────────────────────────────

def _add_log(db, deployment_id: int, message: str, level: str = "info") -> None:
    db.add(DeploymentLog(deployment_id=deployment_id, message=_scrub_log(message), level=level))
    db.commit()


def _set_status(db, deployment: Deployment, status: str, **kwargs) -> None:
    deployment.status = status
    for k, v in kwargs.items():
        setattr(deployment, k, v)
    db.commit()


# ── AWS env ───────────────────────────────────────────────────────────────────

def _decrypt_or_raise(value: str, salt: str, field: str) -> str:
    from cryptography.fernet import InvalidToken
    try:
        return decrypt_aws_credentials(value, user_salt=salt)
    except (InvalidToken, Exception):
        raise RuntimeError(
            f"AWS credentials could not be decrypted ({field}). "
            "Please reconnect your AWS account in Profile → AWS Connect."
        )


def _aws_env(user) -> Dict[str, str]:
    from app.config import settings
    env = _build_tf_env()
    region = user.aws_region or "us-east-1"

    if user.auth_method == "assume_role" and user.role_arn:
        import boto3
        sts_kwargs: dict = {"region_name": region}
        if settings.BACKEND_AWS_ACCESS_KEY and settings.BACKEND_AWS_SECRET_KEY:
            sts_kwargs["aws_access_key_id"] = settings.BACKEND_AWS_ACCESS_KEY
            sts_kwargs["aws_secret_access_key"] = settings.BACKEND_AWS_SECRET_KEY

        sts = boto3.client("sts", **sts_kwargs)
        assume_params: dict = {
            "RoleArn": user.role_arn,
            "RoleSessionName": "cloudkraft-deploy",
        }
        if user.external_id:
            from cryptography.fernet import InvalidToken
            try:
                assume_params["ExternalId"] = decrypt_aws_credentials(
                    user.external_id, user_salt=user.credential_salt
                )
            except (InvalidToken, Exception):
                pass

        resp = sts.assume_role(**assume_params)
        creds = resp["Credentials"]
        env["AWS_ACCESS_KEY_ID"] = creds["AccessKeyId"]
        env["AWS_SECRET_ACCESS_KEY"] = creds["SecretAccessKey"]
        env["AWS_SESSION_TOKEN"] = creds["SessionToken"]
    else:
        if user.aws_access_key:
            env["AWS_ACCESS_KEY_ID"] = _decrypt_or_raise(
                user.aws_access_key, user.credential_salt, "aws_access_key"
            )
        if user.aws_secret_key:
            env["AWS_SECRET_ACCESS_KEY"] = _decrypt_or_raise(
                user.aws_secret_key, user.credential_salt, "aws_secret_key"
            )

    env["AWS_DEFAULT_REGION"] = region
    env["AWS_REGION"] = region
    return env


# ── S3 remote state helpers ───────────────────────────────────────────────────

def _state_key(user_id: int, deployment_id: int) -> str:
    """S3 object key for a deployment's state file."""
    return f"deployments/{user_id}/{deployment_id}/terraform.tfstate"


def _backend_init_args(state_key: str) -> List[str]:
    """Return extra -backend-config flags for terraform init when S3 is configured.

    Credentials for the state bucket come from the platform's own keys so that
    user credentials never need access to the state bucket.
    """
    from app.config import settings
    if not settings.TF_STATE_BUCKET or not state_key:
        return []

    args = [f"-backend-config=key={state_key}"]
    if settings.BACKEND_AWS_ACCESS_KEY:
        args += [
            f"-backend-config=access_key={settings.BACKEND_AWS_ACCESS_KEY}",
            f"-backend-config=secret_key={settings.BACKEND_AWS_SECRET_KEY}",
            f"-backend-config=region={settings.TF_STATE_REGION}",
        ]
    return args


# ── Subprocess helper ─────────────────────────────────────────────────────────

def _run_tf(workspace: str, args: List[str], env: Dict, db, deployment_id: int, timeout: int = 600) -> int:
    if not _TERRAFORM_BIN:
        _add_log(db, deployment_id, "terraform binary not found on server", "error")
        return 1

    proc = subprocess.Popen(
        [_TERRAFORM_BIN] + args,
        cwd=workspace,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    try:
        for raw in proc.stdout:
            line = _strip_ansi(raw.rstrip())
            if line:
                lvl = "error" if "error" in line.lower() else "info"
                _add_log(db, deployment_id, line, lvl)
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        _add_log(db, deployment_id, "Command timed out after 10 minutes", "error")
        return 1

    return proc.returncode


# ── Workspace setup ───────────────────────────────────────────────────────────

def _prepare_workspace(workspace: Path, tf_files: Dict[str, str], db, deployment_id: int) -> bool:
    workspace.mkdir(parents=True, exist_ok=True)

    for filename, content in tf_files.items():
        (workspace / filename).write_text(content, encoding="utf-8")
        _add_log(db, deployment_id, f"  wrote {filename}")

    # Create placeholder zip if any Lambda function references lambda_function.zip
    needs_zip = any(
        "lambda_function.zip" in content
        for content in tf_files.values()
    )
    if needs_zip:
        zip_path = workspace / "lambda_function.zip"
        if not zip_path.exists():
            import zipfile, io
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("index.py", 'def handler(event, context):\n    return {"statusCode": 200, "body": "ok"}\n')
            zip_path.write_bytes(buf.getvalue())
            _add_log(db, deployment_id, "  wrote lambda_function.zip (placeholder)")

    dot_terraform = workspace / ".terraform"
    if _warm_workspace_ready() and not dot_terraform.exists():
        try:
            shutil.copytree(str(WARM_WORKSPACE_DIR / ".terraform"), str(dot_terraform), symlinks=True)
            warm_lock = WARM_WORKSPACE_DIR / ".terraform.lock.hcl"
            if warm_lock.exists():
                shutil.copy2(str(warm_lock), str(workspace / ".terraform.lock.hcl"))
            _add_log(db, deployment_id, "Provider cache loaded from warm workspace")
        except Exception as e:
            _add_log(db, deployment_id, f"Cache copy failed, will run full init: {e}", "warning")

    return True


# ── Public: run plan ──────────────────────────────────────────────────────────

def run_plan(deployment_id: int, workflow_state_dict: dict, user_id: int) -> None:
    """F-012: run terraform plan, store output, set status to planned (or failed)."""
    db = SessionLocal()
    try:
        from app.models.user import User
        deployment = db.query(Deployment).filter(Deployment.id == deployment_id).first()
        user = db.query(User).filter(User.id == user_id).first()
        if not deployment or not user:
            return

        _set_status(db, deployment, "planning", started_at=datetime.now(timezone.utc))
        _add_log(db, deployment_id, "Planning started")

        has_access_key = user.auth_method == "access_key" and user.aws_access_key and user.aws_secret_key
        has_assume_role = user.auth_method == "assume_role" and user.role_arn
        if not has_access_key and not has_assume_role:
            _add_log(db, deployment_id, "AWS credentials not found. Connect your AWS account first.", "error")
            _set_status(db, deployment, "failed", completed_at=datetime.now(timezone.utc))
            return

        _add_log(db, deployment_id, "Generating Terraform configuration...")
        sk = _state_key(user_id, deployment_id)
        try:
            workflow_state = WorkflowState(**workflow_state_dict)
            tf_files = generate_terraform_files(workflow_state, suffix=f"-ck{deployment_id}", state_key=sk)
        except Exception as e:
            _add_log(db, deployment_id, f"HCL generation failed: {e}", "error")
            _set_status(db, deployment, "failed", completed_at=datetime.now(timezone.utc))
            return

        workspace = WORKSPACES_DIR / str(deployment_id)
        _prepare_workspace(workspace, tf_files, db, deployment_id)
        deployment.workspace_path = str(workspace)
        db.commit()

        env = _aws_env(user)

        _add_log(db, deployment_id, "Running terraform init...")
        init_args = ["init", "-no-color", "-input=false"] + _backend_init_args(sk)
        rc = _run_tf(str(workspace), init_args, env, db, deployment_id)
        if rc != 0:
            _set_status(db, deployment, "failed", completed_at=datetime.now(timezone.utc))
            return
        _add_log(db, deployment_id, "✓ Init complete", "success")

        # Run plan and capture output separately so we can store it
        _add_log(db, deployment_id, "Running terraform plan...")
        plan_file = workspace / "deploy.tfplan"
        rc = _run_tf(str(workspace), ["plan", "-out", str(plan_file), "-no-color", "-input=false"], env, db, deployment_id)
        if rc != 0:
            _set_status(db, deployment, "failed", completed_at=datetime.now(timezone.utc))
            return

        # Capture human-readable plan summary
        plan_summary = _capture_plan_show(str(workspace), str(plan_file), env)
        deployment.plan_output = plan_summary
        _set_status(db, deployment, "planned")
        _add_log(db, deployment_id, "✓ Plan complete — review and approve to apply", "success")

    except Exception as e:
        logger.exception("Plan %d crashed", deployment_id)
        try:
            dep = db.query(Deployment).filter(Deployment.id == deployment_id).first()
            if dep:
                _add_log(db, deployment_id, f"Unexpected error: {e}", "error")
                _set_status(db, dep, "failed", completed_at=datetime.now(timezone.utc))
        except Exception:
            pass
    finally:
        db.close()


def _capture_plan_show(workspace: str, plan_file: str, env: Dict) -> str:
    """Return human-readable `terraform show` output for a saved plan file."""
    if not _TERRAFORM_BIN:
        return ""
    try:
        result = subprocess.run(
            [_TERRAFORM_BIN, "show", "-no-color", plan_file],
            cwd=workspace,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        return _strip_ansi(result.stdout)[:32768]  # cap at 32 KiB
    except Exception:
        return ""


# ── Public: run deployment ─────────────────────────────────────────────────────

def run_deployment(deployment_id: int, workflow_state_dict: dict, user_id: int) -> None:
    db = SessionLocal()
    try:
        from app.models.user import User
        deployment = db.query(Deployment).filter(Deployment.id == deployment_id).first()
        user = db.query(User).filter(User.id == user_id).first()
        if not deployment or not user:
            return

        _set_status(db, deployment, "running", started_at=datetime.now(timezone.utc))
        _add_log(db, deployment_id, "Deployment started")

        # Check credentials
        has_access_key = user.auth_method == "access_key" and user.aws_access_key and user.aws_secret_key
        has_assume_role = user.auth_method == "assume_role" and user.role_arn
        if not has_access_key and not has_assume_role:
            _add_log(db, deployment_id, "AWS credentials not found. Connect your AWS account first.", "error")
            _set_status(db, deployment, "failed", completed_at=datetime.now(timezone.utc))
            return

        _add_log(db, deployment_id, "Generating Terraform configuration...")
        sk = _state_key(user_id, deployment_id)
        try:
            workflow_state = WorkflowState(**workflow_state_dict)
            tf_files = generate_terraform_files(workflow_state, suffix=f"-ck{deployment_id}", state_key=sk)
        except Exception as e:
            _add_log(db, deployment_id, f"HCL generation failed: {e}", "error")
            _set_status(db, deployment, "failed", completed_at=datetime.now(timezone.utc))
            return

        workspace = WORKSPACES_DIR / str(deployment_id)
        _prepare_workspace(workspace, tf_files, db, deployment_id)
        deployment.workspace_path = str(workspace)
        db.commit()

        env = _aws_env(user)

        _add_log(db, deployment_id, "Running terraform init...")
        init_args = ["init", "-no-color", "-input=false"] + _backend_init_args(sk)
        rc = _run_tf(str(workspace), init_args, env, db, deployment_id)
        if rc != 0:
            _set_status(db, deployment, "failed", completed_at=datetime.now(timezone.utc))
            return
        _add_log(db, deployment_id, "✓ Init complete", "success")

        # terraform plan
        _add_log(db, deployment_id, "Running terraform plan...")
        rc = _run_tf(str(workspace), ["plan", "-no-color", "-input=false"], env, db, deployment_id)
        if rc != 0:
            _set_status(db, deployment, "failed", completed_at=datetime.now(timezone.utc))
            return
        _add_log(db, deployment_id, "✓ Plan complete", "success")

        # terraform apply
        _add_log(db, deployment_id, "Running terraform apply -auto-approve...")
        rc = _run_tf(str(workspace), ["apply", "-auto-approve", "-no-color", "-input=false"], env, db, deployment_id)
        if rc != 0:
            _set_status(db, deployment, "failed", completed_at=datetime.now(timezone.utc))
            return

        from app.config import settings as _settings
        if not _settings.TF_STATE_BUCKET:
            state_file = workspace / "terraform.tfstate"
            if state_file.exists():
                deployment.terraform_state = state_file.read_text(encoding="utf-8")

        resource_count = len(workflow_state.nodes)
        _set_status(db, deployment, "succeeded",
                    completed_at=datetime.now(timezone.utc),
                    resource_count=resource_count)
        _add_log(db, deployment_id, f"✓ Deployment complete — {resource_count} resource(s) created", "success")

    except Exception as e:
        logger.exception("Deployment %d crashed", deployment_id)
        try:
            dep = db.query(Deployment).filter(Deployment.id == deployment_id).first()
            if dep:
                _add_log(db, deployment_id, f"Unexpected error: {e}", "error")
                _set_status(db, dep, "failed", completed_at=datetime.now(timezone.utc))
        except Exception:
            pass
    finally:
        db.close()


# ── Public: apply a planned deployment ────────────────────────────────────────

def run_apply_planned(deployment_id: int, user_id: int) -> None:
    db = SessionLocal()
    try:
        from app.models.user import User
        deployment = db.query(Deployment).filter(Deployment.id == deployment_id).first()
        user = db.query(User).filter(User.id == user_id).first()
        if not deployment or not user:
            return

        _set_status(db, deployment, "running", started_at=datetime.now(timezone.utc))
        _add_log(db, deployment_id, "Applying approved plan...")

        workspace = Path(deployment.workspace_path) if deployment.workspace_path else None
        if not workspace or not workspace.exists():
            _add_log(db, deployment_id, "Workspace not found", "error")
            _set_status(db, deployment, "failed", completed_at=datetime.now(timezone.utc))
            return

        env = _aws_env(user)
        plan_file = workspace / "deploy.tfplan"

        if plan_file.exists():
            _add_log(db, deployment_id, "Running terraform apply (saved plan)...")
            rc = _run_tf(str(workspace), ["apply", "-no-color", "-input=false", str(plan_file)], env, db, deployment_id)
        else:
            _add_log(db, deployment_id, "Plan file missing — running apply -auto-approve...", "warning")
            rc = _run_tf(str(workspace), ["apply", "-auto-approve", "-no-color", "-input=false"], env, db, deployment_id)

        if rc != 0:
            _set_status(db, deployment, "failed", completed_at=datetime.now(timezone.utc))
            return

        from app.config import settings as _settings
        if not _settings.TF_STATE_BUCKET:
            state_file = workspace / "terraform.tfstate"
            if state_file.exists():
                deployment.terraform_state = state_file.read_text(encoding="utf-8")

        try:
            workflow_state_dict = None
            if deployment.workflow_id:
                from app.models.workflow import Workflow
                wf = db.query(Workflow).filter(Workflow.id == deployment.workflow_id).first()
                if wf:
                    workflow_state_dict = wf.workflow_state
            resource_count = len(WorkflowState(**workflow_state_dict).nodes) if workflow_state_dict else 0
        except Exception:
            resource_count = 0

        _set_status(db, deployment, "succeeded",
                    completed_at=datetime.now(timezone.utc),
                    resource_count=resource_count)
        _add_log(db, deployment_id, f"✓ Deployment complete — {resource_count} resource(s) created", "success")

    except Exception as e:
        logger.exception("Apply %d crashed", deployment_id)
        try:
            dep = db.query(Deployment).filter(Deployment.id == deployment_id).first()
            if dep:
                _add_log(db, deployment_id, f"Unexpected error: {e}", "error")
                _set_status(db, dep, "failed", completed_at=datetime.now(timezone.utc))
        except Exception:
            pass
    finally:
        db.close()


# ── Public: run destroy ───────────────────────────────────────────────────────

def run_destroy(deployment_id: int, user_id: int) -> None:
    db = SessionLocal()
    try:
        from app.models.user import User
        deployment = db.query(Deployment).filter(Deployment.id == deployment_id).first()
        user = db.query(User).filter(User.id == user_id).first()
        if not deployment or not user:
            return

        _set_status(db, deployment, "destroying", started_at=datetime.now(timezone.utc))
        _add_log(db, deployment_id, "Destroy started")

        workspace = Path(deployment.workspace_path) if deployment.workspace_path else None
        if not workspace or not workspace.exists():
            _add_log(db, deployment_id, "Workspace not found — may have been cleaned up already", "error")
            _set_status(db, deployment, "failed", completed_at=datetime.now(timezone.utc))
            return

        from app.config import settings as _settings
        state_file = workspace / "terraform.tfstate"

        if _settings.TF_STATE_BUCKET:
            # State lives in S3 — no local restore needed; re-init to connect backend
            sk = _state_key(user_id, deployment_id)
        else:
            if not state_file.exists() and deployment.terraform_state:
                state_file.write_text(deployment.terraform_state, encoding="utf-8")
                _add_log(db, deployment_id, "Restored terraform state from database")

            if not state_file.exists():
                _add_log(db, deployment_id, "No terraform state found — nothing to destroy", "warning")
                _set_status(db, deployment, "destroyed", completed_at=datetime.now(timezone.utc))
                return
            sk = ""

        has_access_key = user.auth_method == "access_key" and user.aws_access_key and user.aws_secret_key
        has_assume_role = user.auth_method == "assume_role" and user.role_arn
        if not has_access_key and not has_assume_role:
            _add_log(db, deployment_id, "AWS credentials not found", "error")
            _set_status(db, deployment, "failed", completed_at=datetime.now(timezone.utc))
            return

        env = _aws_env(user)

        if _settings.TF_STATE_BUCKET and not (workspace / ".terraform").exists():
            _add_log(db, deployment_id, "Running terraform init (reconnecting to remote state)...")
            init_args = ["init", "-no-color", "-input=false"] + _backend_init_args(sk)
            if _run_tf(str(workspace), init_args, env, db, deployment_id) != 0:
                _set_status(db, deployment, "failed", completed_at=datetime.now(timezone.utc))
                return

        _add_log(db, deployment_id, "Running terraform destroy -auto-approve...")
        rc = _run_tf(str(workspace), ["destroy", "-auto-approve", "-no-color", "-input=false"], env, db, deployment_id)
        if rc != 0:
            _set_status(db, deployment, "failed", completed_at=datetime.now(timezone.utc))
            return

        _set_status(db, deployment, "destroyed", completed_at=datetime.now(timezone.utc))
        _add_log(db, deployment_id, "✓ All resources destroyed", "success")

    except Exception as e:
        logger.exception("Destroy %d crashed", deployment_id)
        try:
            dep = db.query(Deployment).filter(Deployment.id == deployment_id).first()
            if dep:
                _add_log(db, deployment_id, f"Unexpected error: {e}", "error")
                _set_status(db, dep, "failed", completed_at=datetime.now(timezone.utc))
        except Exception:
            pass
    finally:
        db.close()

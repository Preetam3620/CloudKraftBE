import asyncio
import json
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.database import get_db
from app.limiter import limiter
from app.models.audit import AuditLog
from app.models.deployment import Deployment, DeploymentLog
from app.models.user import User
from app.models.workflow import Workflow
from app.schemas.deployment import (
    DeployRequest,
    DeploymentLogsResponse,
    DeploymentLogItem,
    DeploymentResponse,
)
from app.schemas.workflow import WorkflowState
from app.services.terraform_deployer import run_deployment, run_destroy, run_plan, run_apply_planned
from app.services.terraform_runner import is_terraform_available

router = APIRouter(prefix="/api/deploy", tags=["deployment"])

_TERMINAL_STATUSES = {"succeeded", "failed", "destroyed"}


def _require_terraform() -> None:
    """Refuse to queue work the server can't run (e.g. on Vercel, which has no terraform binary)."""
    if not is_terraform_available():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Deployments are unavailable on this server: the terraform binary is not installed.",
        )


def _to_response(d: Deployment) -> DeploymentResponse:
    return DeploymentResponse(
        id=d.id,
        status=d.status,
        workflow_name=d.workflow_name,
        resource_count=d.resource_count,
        plan_output=d.plan_output,
        created_at=d.created_at,
        started_at=d.started_at,
        completed_at=d.completed_at,
    )


def _write_audit(db: Session, action: str, user_id: int, resource_id=None, ip=None, details=None):
    db.add(AuditLog(
        user_id=user_id,
        action=action,
        resource_type="deployment",
        resource_id=resource_id,
        ip_address=ip,
        details=json.dumps(details) if details else None,
    ))
    db.commit()


@router.post("/plan", response_model=DeploymentResponse, status_code=status.HTTP_202_ACCEPTED,
             dependencies=[Depends(_require_terraform)])
@limiter.limit("5/minute")
async def deploy_plan(
    request: Request,
    deploy_request: DeployRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Run terraform plan. Poll /{id}/logs until status is 'planned', then POST /{id}/approve to apply."""
    workflow_state_dict: Optional[dict] = None
    workflow_name = deploy_request.workflow_name or "Unnamed Workflow"

    if deploy_request.workflow_id:
        wf = db.query(Workflow).filter(
            Workflow.id == deploy_request.workflow_id,
            Workflow.user_id == current_user.id,
        ).first()
        if not wf:
            raise HTTPException(status_code=404, detail="Workflow not found")
        workflow_state_dict = wf.workflow_state
        workflow_name = wf.name
    elif deploy_request.workflow:
        workflow_state_dict = (
            deploy_request.workflow
            if isinstance(deploy_request.workflow, dict)
            else deploy_request.workflow.model_dump()
        )
    else:
        raise HTTPException(status_code=400, detail="Provide workflow_id or workflow")

    try:
        WorkflowState(**workflow_state_dict)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid workflow state: {e}")

    deployment = Deployment(
        user_id=current_user.id,
        workflow_id=deploy_request.workflow_id,
        workflow_name=workflow_name,
        status="pending",
    )
    db.add(deployment)
    db.commit()
    db.refresh(deployment)

    _write_audit(db, "deploy_plan", current_user.id, resource_id=deployment.id,
                 ip=request.client.host if request.client else None)

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, run_plan, deployment.id, workflow_state_dict, current_user.id)

    return _to_response(deployment)


@router.post("/{deployment_id}/approve", response_model=DeploymentResponse, status_code=status.HTTP_202_ACCEPTED,
             dependencies=[Depends(_require_terraform)])
@limiter.limit("5/minute")
async def approve_deployment(
    request: Request,
    deployment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Apply a deployment that has been planned and reviewed."""
    deployment = db.query(Deployment).filter(
        Deployment.id == deployment_id,
        Deployment.user_id == current_user.id,
    ).first()
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    if deployment.status != "planned":
        raise HTTPException(
            status_code=400,
            detail=f"Deployment must be in 'planned' status to approve. Current status: '{deployment.status}'",
        )

    _write_audit(db, "deploy_approve", current_user.id, resource_id=deployment_id,
                 ip=request.client.host if request.client else None)

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, run_apply_planned, deployment.id, current_user.id)

    return _to_response(deployment)


@router.post("/apply", response_model=DeploymentResponse, status_code=status.HTTP_202_ACCEPTED,
             dependencies=[Depends(_require_terraform)])
@limiter.limit("5/minute")
async def deploy_apply(
    request: Request,
    deploy_request: DeployRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Start a deployment. Returns immediately; terraform runs in background."""
    workflow_state_dict: Optional[dict] = None
    workflow_name = deploy_request.workflow_name or "Unnamed Workflow"

    if deploy_request.workflow_id:
        wf = db.query(Workflow).filter(
            Workflow.id == deploy_request.workflow_id,
            Workflow.user_id == current_user.id,
        ).first()
        if not wf:
            raise HTTPException(status_code=404, detail="Workflow not found")
        workflow_state_dict = wf.workflow_state
        workflow_name = wf.name
    elif deploy_request.workflow:
        workflow_state_dict = (
            deploy_request.workflow
            if isinstance(deploy_request.workflow, dict)
            else deploy_request.workflow.model_dump()
        )
    else:
        raise HTTPException(status_code=400, detail="Provide workflow_id or workflow")

    try:
        WorkflowState(**workflow_state_dict)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid workflow state: {e}")

    deployment = Deployment(
        user_id=current_user.id,
        workflow_id=deploy_request.workflow_id,
        workflow_name=workflow_name,
        status="pending",
    )
    db.add(deployment)
    db.commit()
    db.refresh(deployment)

    _write_audit(db, "deploy_apply", current_user.id, resource_id=deployment.id,
                 ip=request.client.host if request.client else None)

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, run_deployment, deployment.id, workflow_state_dict, current_user.id)

    return _to_response(deployment)


# ---------------------------------------------------------------------------
# List / get
# ---------------------------------------------------------------------------

@router.get("/", response_model=List[DeploymentResponse])
def list_deployments(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    deployments = (
        db.query(Deployment)
        .filter(Deployment.user_id == current_user.id)
        .order_by(Deployment.created_at.desc())
        .limit(50)
        .all()
    )
    return [_to_response(d) for d in deployments]


@router.get("/{deployment_id}", response_model=DeploymentResponse)
def get_deployment(
    deployment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    d = db.query(Deployment).filter(
        Deployment.id == deployment_id,
        Deployment.user_id == current_user.id,
    ).first()
    if not d:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return _to_response(d)


@router.get("/{deployment_id}/logs", response_model=DeploymentLogsResponse)
def get_deployment_logs(
    deployment_id: int,
    after_id: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    deployment = db.query(Deployment).filter(
        Deployment.id == deployment_id,
        Deployment.user_id == current_user.id,
    ).first()
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    logs = (
        db.query(DeploymentLog)
        .filter(
            DeploymentLog.deployment_id == deployment_id,
            DeploymentLog.id > after_id,
        )
        .order_by(DeploymentLog.id.asc())
        .limit(200)
        .all()
    )
    return DeploymentLogsResponse(
        logs=[DeploymentLogItem.model_validate(l) for l in logs],
        has_more=False,
        deployment_status=deployment.status,
    )


# ---------------------------------------------------------------------------
# Destroy
# ---------------------------------------------------------------------------

@router.post("/{deployment_id}/destroy", response_model=DeploymentResponse,
             status_code=status.HTTP_202_ACCEPTED,
             dependencies=[Depends(_require_terraform)])
@limiter.limit("10/minute")
async def destroy_deployment(
    request: Request,
    deployment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    deployment = db.query(Deployment).filter(
        Deployment.id == deployment_id,
        Deployment.user_id == current_user.id,
    ).first()
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    if deployment.status not in {"succeeded", "failed"}:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot destroy deployment with status '{deployment.status}'",
        )

    deployment.status = "destroying"
    db.commit()
    db.refresh(deployment)

    _write_audit(db, "deploy_destroy", current_user.id, resource_id=deployment_id,
                 ip=request.client.host if request.client else None)

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, run_destroy, deployment.id, current_user.id)

    return _to_response(deployment)


@router.post("/destroy-all", status_code=status.HTTP_202_ACCEPTED,
             dependencies=[Depends(_require_terraform)])
@limiter.limit("3/minute")
async def destroy_all_deployments(
    request: Request,
    confirm: bool = Body(..., embed=True),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Destroy all active deployments.  Body must contain ``{"confirm": true}``."""
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail='Send {"confirm": true} in the request body to confirm mass destruction.',
        )

    active = (
        db.query(Deployment)
        .filter(
            Deployment.user_id == current_user.id,
            Deployment.status.in_(["succeeded", "failed"]),
        )
        .all()
    )
    if not active:
        return {"message": "No active deployments to destroy", "count": 0}

    loop = asyncio.get_event_loop()
    for deployment in active:
        deployment.status = "destroying"
        db.commit()
        _write_audit(db, "deploy_destroy_all", current_user.id, resource_id=deployment.id,
                     ip=request.client.host if request.client else None)
        loop.run_in_executor(None, run_destroy, deployment.id, current_user.id)

    return {"message": f"Destroying {len(active)} deployment(s)", "count": len(active)}

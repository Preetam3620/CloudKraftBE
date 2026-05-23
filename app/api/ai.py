import json
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.api.auth import get_current_user
from app.config import settings
from app.limiter import limiter
from app.models.user import User
from app.utils.security import decrypt_aws_credentials

logger = logging.getLogger(__name__)

_PROMPT_MAX_LEN = 1000
_PROMPT_STRIP_RE = re.compile(r'[<>{}()\[\]`\\$|;&]')

router = APIRouter(prefix="/api/ai", tags=["ai"])

# ---------------------------------------------------------------------------
# System prompt — teaches Claude the WorkflowState schema and node types
# ---------------------------------------------------------------------------
from app.services.ai_prompts import WORKFLOW_GENERATE_PROMPT as _SYSTEM_PROMPT


class AIGenerateRequest(BaseModel):
    prompt: str


class AIGenerateResponse(BaseModel):
    workflow_state: dict
    description: str


@router.post("/generate", response_model=AIGenerateResponse)
@limiter.limit("20/minute")
async def generate_architecture(
    http_request: Request,
    request: AIGenerateRequest,
    current_user: User = Depends(get_current_user),
):
    """Generate a WorkflowState from a natural language architecture description."""
    # Prefer user's own key; fall back to server-level env var
    api_key: str | None = None
    if current_user.anthropic_api_key:
        try:
            api_key = decrypt_aws_credentials(
                current_user.anthropic_api_key,
                user_salt=current_user.credential_salt,
            )
        except Exception:
            pass
    if not api_key:
        api_key = settings.ANTHROPIC_API_KEY
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="AI generation is not configured. Add your Anthropic API key in Profile → Environment Variables.",
        )

    raw_prompt = request.prompt.strip()
    if not raw_prompt:
        raise HTTPException(status_code=400, detail="Prompt cannot be empty.")
    safe_prompt = _PROMPT_STRIP_RE.sub("", raw_prompt)[:_PROMPT_MAX_LEN]
    if not safe_prompt:
        raise HTTPException(status_code=400, detail="Prompt contained no usable text after sanitization.")

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Generate a CloudKraft architecture for: {safe_prompt}",
                }
            ],
        )

        raw = message.content[0].text.strip()

        # Strip markdown fences if the model wrapped it anyway
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        if raw.endswith("```"):
            raw = raw[: raw.rfind("```")].strip()

        workflow_state = json.loads(raw)

        # Basic sanity check
        if "nodes" not in workflow_state or not isinstance(workflow_state["nodes"], list):
            raise ValueError("Generated JSON is missing 'nodes' array")

        # Ensure connections array exists
        workflow_state.setdefault("connections", [])
        workflow_state.setdefault("metadata", {})

        logger.info(
            "AI generated architecture with %d nodes for user %s",
            len(workflow_state["nodes"]),
            current_user.email,
        )

        return AIGenerateResponse(
            workflow_state=workflow_state,
            description=f"Generated {len(workflow_state['nodes'])} resources for: {safe_prompt}",
        )

    except json.JSONDecodeError as e:
        logger.error("Claude returned invalid JSON: %s", e)
        raise HTTPException(
            status_code=500,
            detail="AI returned an invalid response. Please try rephrasing your prompt.",
        )
    except Exception as e:
        logger.exception("AI generation failed")
        raise HTTPException(status_code=500, detail=f"AI generation failed: {str(e)}")

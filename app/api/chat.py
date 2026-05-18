"""
Chat API — REST session management + SSE streaming + WebSocket streaming.

REST endpoint  POST /sessions/{id}/message  streams via Server-Sent Events.
WebSocket endpoint  WS /ws/{id}?token=  keeps the connection open for multi-turn chat.

WebSocket auth uses ?token= query param because the browser WebSocket API
cannot send custom headers.
"""
import json
import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import (
    APIRouter, Depends, HTTPException, Query, Request,
    WebSocket, WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, selectinload

from app.api.auth import get_current_user
from app.database import get_db
from app.limiter import limiter
from app.models.chat import ChatSession, ChatMessage
from app.models.user import User
from app.schemas.chat import (
    ChatSessionCreate, ChatSessionResponse,
    ChatHistoryResponse, ChatMessageRequest,
)
from app.services.chat_graph import build_graph, stream_response, seed_memory_from_history, is_thread_seeded
from app.utils.security import decode_access_token, is_token_revoked, decrypt_aws_credentials

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])

_PROVIDER_KEY_FIELD = {
    "claude": "anthropic_api_key",
    "openai": "openai_api_key",
    "gemini": "gemini_api_key",
}


def _auth_ws(token: str, db: Session) -> Optional[User]:
    # WebSocket can't use Depends(get_current_user) — token comes via query param
    payload = decode_access_token(token)
    if not payload:
        return None
    jti = payload.get("jti")
    if jti and is_token_revoked(jti, db):
        return None
    email = payload.get("sub")
    if not email:
        return None
    return db.query(User).filter(User.email == email).first()


def _decrypt_key(encrypted: Optional[str], user: User) -> Optional[str]:
    if not encrypted:
        return None
    try:
        return decrypt_aws_credentials(encrypted, user.credential_salt)
    except Exception:
        logger.warning("Failed to decrypt API key for user=%d", user.id)
        return None


def _get_owned(session_id: int, user_id: int, db: Session) -> ChatSession:
    s = db.query(ChatSession).filter(
        ChatSession.id == session_id,
        ChatSession.user_id == user_id,
    ).first()
    if not s:
        raise HTTPException(404, "Chat session not found")
    return s


def _resolve_graph(provider: str, model: Optional[str], user: User):
    encrypted = getattr(user, _PROVIDER_KEY_FIELD.get(provider, ""), None)
    user_key = _decrypt_key(encrypted, user)
    return build_graph(provider, model, user_key)


def _get_session_messages(session_id: int, db: Session) -> list:
    return (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.id)
        .all()
    )


async def _seed_session_memory(session: ChatSession, thread_id: str, db: Session) -> None:
    if not await is_thread_seeded(thread_id):
        db_messages = _get_session_messages(session.id, db)
        if db_messages:
            await seed_memory_from_history(thread_id, db_messages)


@router.post("/sessions", response_model=ChatSessionResponse, status_code=201)
@limiter.limit("20/minute")
def create_session(
    request: Request,
    body: ChatSessionCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = ChatSession(
        user_id=current_user.id,
        llm_provider=body.llm_provider,
        llm_model=body.llm_model,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


@router.get("/sessions", response_model=List[ChatSessionResponse])
def list_sessions(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return (
        db.query(ChatSession)
        .filter(ChatSession.user_id == current_user.id)
        .order_by(ChatSession.updated_at.desc().nullslast(), ChatSession.created_at.desc())
        .all()
    )


@router.get("/sessions/{session_id}", response_model=ChatHistoryResponse)
def get_session(
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    s = (
        db.query(ChatSession)
        .options(selectinload(ChatSession.messages))
        .filter(ChatSession.id == session_id, ChatSession.user_id == current_user.id)
        .first()
    )
    if not s:
        raise HTTPException(404, "Chat session not found")
    return ChatHistoryResponse(session=s, messages=s.messages)


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    s = _get_owned(session_id, current_user.id, db)
    db.delete(s)
    db.commit()


@router.post("/sessions/{session_id}/message")
@limiter.limit("30/minute")
async def send_message(
    request: Request,
    session_id: int,
    body: ChatMessageRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Stream a chat response as Server-Sent Events (text/event-stream).

    Events: `{"type":"token","content":"..."}` ... `{"type":"done"}`
    On error: `{"type":"error","detail":"..."}`
    """
    s = _get_owned(session_id, current_user.id, db)

    try:
        graph = _resolve_graph(body.llm_provider, body.llm_model, current_user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    thread_id = str(session_id)

    # Only hit the DB if LangGraph memory lost this thread's history (e.g. after restart)
    if not await is_thread_seeded(thread_id):
        await seed_memory_from_history(thread_id, _get_session_messages(session_id, db))

    db.add(ChatMessage(session_id=session_id, role="user", content=body.message))
    if not s.title:
        s.title = body.message[:60]
    s.updated_at = datetime.now(timezone.utc)
    db.commit()

    user_message = body.message

    async def generate():
        chunks: list[str] = []
        try:
            async for chunk in stream_response(graph, user_message, thread_id):
                chunks.append(chunk)
                yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"
        except Exception as exc:
            logger.exception("LLM stream error session=%d", session_id)
            yield f"data: {json.dumps({'type': 'error', 'detail': str(exc)})}\n\n"
            return

        full = "".join(chunks)
        if full:
            db.add(ChatMessage(session_id=session_id, role="assistant", content=full))
            db.commit()

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.websocket("/ws/{session_id}")
async def chat_ws(
    websocket: WebSocket,
    session_id: int,
    token: str = Query(...),
    db: Session = Depends(get_db),
):
    """
    Client sends:  {"message":"text","llm_provider":"claude","llm_model":"..."}
                   llm_provider/llm_model are optional; fall back to session defaults.
    Server sends:  {"type":"token","content":"<chunk>"}  {"type":"done"}
                   {"type":"error","detail":"<msg>"}
    Close codes: 4001 = bad/expired token, 4004 = session not found.
    """
    user = _auth_ws(token, db)
    if not user:
        await websocket.close(code=4001)
        return

    session = db.query(ChatSession).filter(
        ChatSession.id == session_id,
        ChatSession.user_id == user.id,
    ).first()
    if not session:
        await websocket.close(code=4004)
        return

    await websocket.accept()
    thread_id = str(session_id)

    # Restore prior conversation context into MemorySaver if lost after restart
    await _seed_session_memory(session, thread_id, db)

    # Build graph once per connection; rebuild only when provider/model changes
    graph = None
    graph_provider: Optional[str] = None
    graph_model: Optional[str] = None

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
                user_text = str(data.get("message", "")).strip()
                msg_provider = (data.get("llm_provider") or "").strip() or session.llm_provider
                msg_model = (data.get("llm_model") or "").strip() or session.llm_model
            except (json.JSONDecodeError, AttributeError):
                await websocket.send_json({"type": "error", "detail": "Invalid JSON"})
                continue

            if not user_text:
                continue

            if msg_provider not in _PROVIDER_KEY_FIELD:
                await websocket.send_json({"type": "error", "detail": f"Unknown provider: {msg_provider!r}"})
                continue

            if msg_provider != graph_provider or msg_model != graph_model:
                try:
                    graph = _resolve_graph(msg_provider, msg_model, user)
                    graph_provider = msg_provider
                    graph_model = msg_model
                except Exception as exc:
                    await websocket.send_json({"type": "error", "detail": str(exc)})
                    continue

            db.add(ChatMessage(session_id=session_id, role="user", content=user_text))
            if not session.title:
                session.title = user_text[:60]
            session.updated_at = datetime.now(timezone.utc)
            db.commit()

            chunks: list[str] = []
            try:
                async for chunk in stream_response(graph, user_text, thread_id):
                    chunks.append(chunk)
                    await websocket.send_json({"type": "token", "content": chunk})
            except Exception as exc:
                logger.exception("LLM error session=%d", session_id)
                await websocket.send_json({"type": "error", "detail": str(exc)})
                continue

            full = "".join(chunks)
            if full:
                db.add(ChatMessage(session_id=session_id, role="assistant", content=full))
                db.commit()

            await websocket.send_json({"type": "done"})

    except WebSocketDisconnect:
        logger.info("Chat WS disconnected session=%d user=%d", session_id, user.id)
    except Exception:
        logger.exception("Unexpected WS error session=%d", session_id)

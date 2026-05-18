# Feature: LLM Chatbot — Backend

## Overview

LangGraph-powered chatbot added to the FastAPI backend. Fully independent of the Terraform workflow. Supports Claude, OpenAI, and Gemini via Server-Sent Events (REST) and WebSocket streaming. Chat sessions and messages are persisted in PostgreSQL/SQLite. API keys use a hybrid model: user-stored encrypted key → env var fallback.

**Status: Implemented.**

---

## Files

| Action | File | Notes |
|--------|------|-------|
| Created | `app/models/chat.py` | `ChatSession`, `ChatMessage` ORM models |
| Created | `app/schemas/chat.py` | Pydantic request/response schemas |
| Created | `app/services/chat_graph.py` | LangGraph service — LLM wiring + streaming |
| Created | `app/api/chat.py` | REST + SSE + WebSocket router |
| Modified | `app/models/user.py` | Added `openai_api_key`, `gemini_api_key` columns |
| Modified | `app/models/__init__.py` | Imported `ChatSession`, `ChatMessage` |
| Modified | `app/main.py` | Column migration + `chat.router` registration |
| Modified | `app/config.py` | Added `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` settings |
| Modified | `requirements.txt` | Added LangGraph + LangChain provider packages |

---

## Database Models (`app/models/chat.py`)

**`ChatSession`** — `id`, `user_id` (FK → users CASCADE), `title` (auto-set from first message, max 60 chars), `llm_provider`, `llm_model` (NULL = use provider default), `created_at`, `updated_at`.

**`ChatMessage`** — `id`, `session_id` (FK → chat_sessions CASCADE), `role` (`"user"` | `"assistant"`), `content`, `created_at`.

Tables are auto-created by `Base.metadata.create_all()` on startup. No Alembic migration needed for fresh installs. The column migration loop in `_run_column_migrations()` in `main.py` backfills `openai_api_key` and `gemini_api_key` on existing databases.

---

## Schemas (`app/schemas/chat.py`)

- **`ChatSessionCreate`** — `llm_provider` (validated against `{"claude","openai","gemini"}`), optional `llm_model`
- **`ChatMessageRequest`** — inherits provider validation + `message` (stripped, non-empty, ≤3000 chars)
- **`ChatSessionResponse`** — session fields with ORM mode
- **`ChatMessageResponse`** — message fields with ORM mode
- **`ChatHistoryResponse`** — `session` + `messages` list

---

## LangGraph Service (`app/services/chat_graph.py`)

**`_memory`** — one `MemorySaver` instance at module level, shared across all connections, isolated by `thread_id = str(session_id)`.

**`_seed_graph`** — noop-node `StateGraph` compiled once at startup. Shares `_memory` but has no LLM node. Used only as a handle to call `aget_state` / `aupdate_state` on the checkpointer without instantiating any LLM client.

**`is_thread_seeded(thread_id)`** — reads from `_seed_graph`; returns `True` if the checkpointer already has messages for the thread. Used to gate DB queries and seed writes.

**`seed_memory_from_history(thread_id, db_messages)`** — replays DB rows into the shared `MemorySaver` via `_seed_graph.aupdate_state`. Called after server restart when `is_thread_seeded` returns `False`. No LLM call.

**`build_graph(provider, model, user_key)`** — builds a real LLM-backed `StateGraph` for the given provider. Called once per SSE request and cached per-connection in WebSocket. Provider defaults: `claude-sonnet-4-6`, `gpt-4o-mini`, `gemini-2.0-flash`.

**`stream_response(graph, user_message, thread_id)`** — async generator using `graph.astream_events(..., version="v2")`; yields token strings from `on_chat_model_stream` events.

**API key resolution** — `_resolve_key(user_key, env_var)`: user-provided decrypted key wins; falls back to `settings.<env_var>` (pydantic-settings reads `.env`).

---

## API Router (`app/api/chat.py`)

### REST Endpoints

| Method | Path | Auth | Rate limit | Description |
|--------|------|------|-----------|-------------|
| `POST` | `/api/chat/sessions` | Bearer JWT | 20/min | Create chat session |
| `GET` | `/api/chat/sessions` | Bearer JWT | — | List user's sessions (ordered by `updated_at` desc) |
| `GET` | `/api/chat/sessions/{id}` | Bearer JWT | — | Session + full message history (`selectinload` prevents N+1) |
| `DELETE` | `/api/chat/sessions/{id}` | Bearer JWT | — | Delete session + cascade messages |
| `POST` | `/api/chat/sessions/{id}/message` | Bearer JWT | 30/min | Send message — streams response via SSE |

### SSE Protocol (`POST /sessions/{id}/message`)

Request body: `{"message": "...", "llm_provider": "claude", "llm_model": "..."}` (`llm_provider`/`llm_model` optional — override session defaults per message).

Response `text/event-stream`:
```
data: {"type":"token","content":"<chunk>"}
...
data: {"type":"done"}
data: {"type":"error","detail":"<msg>"}   ← on failure
```

Before streaming, checks `is_thread_seeded` and calls `seed_memory_from_history` if needed (restores context after server restart).

### WebSocket (`WS /api/chat/ws/{id}?token=<jwt>`)

Auth via query param — browser WebSocket API cannot send custom headers.

Client sends: `{"message":"...","llm_provider":"claude","llm_model":"..."}` (provider/model optional per message, falls back to session defaults).

Server sends:
```
{"type":"token","content":"<chunk>"}
{"type":"done"}
{"type":"error","detail":"<msg>"}
```

Close codes: `4001` = bad/expired token, `4004` = session not found.

Graph is built lazily and cached per-connection; rebuilt only when `llm_provider` or `llm_model` changes within the same connection. Memory is seeded once on connect via `_seed_session_memory`.

### Key helpers

- **`_auth_ws(token, db)`** — decodes JWT + checks revocation; returns `User` or `None`. Reuses `decode_access_token()` and `is_token_revoked()` from `app/utils/security.py`.
- **`_decrypt_key(encrypted, user)`** — wraps `decrypt_aws_credentials(encrypted, user.credential_salt)`; logs a warning and returns `None` on failure.
- **`_resolve_graph(provider, model, user)`** — looks up encrypted key from `_PROVIDER_KEY_FIELD`, decrypts, calls `build_graph`.
- **`_seed_session_memory(session, thread_id, db)`** — gates DB query via `is_thread_seeded` before replaying history.

---

## Key Design Decisions

**SSE + WebSocket dual interface** — REST SSE (`POST /sessions/{id}/message`) is the primary interface for simple integrations; WebSocket is for persistent multi-turn connections where the client manages the connection lifecycle.

**Module-level MemorySaver** — shared across all connections, isolated by `thread_id = str(session_id)`. Reconnecting to the same session resumes in-memory context within the same server process.

**MemorySaver vs DB** — MemorySaver is ephemeral (lost on restart). `ChatMessage` rows are the durable record. `is_thread_seeded` + `seed_memory_from_history` replay DB history into the checkpointer on first use after restart — one checkpoint read gates both the DB query and the seed write.

**`_seed_graph` (noop graph)** — avoids building a throwaway LLM client just to access the checkpointer. Seeding only writes to `MemorySaver`; there's no need for a real LLM node.

**Per-message provider/model override** — both SSE and WebSocket accept `llm_provider`/`llm_model` per message, falling back to the session's stored values. WebSocket caches the graph and only rebuilds when these values change.

**Hybrid API key** — user-stored (Fernet-encrypted via `credential_salt`) wins; env var is the fallback for shared/team deployments.

---

## Environment Variables

| Variable | Notes |
|----------|-------|
| `ANTHROPIC_API_KEY` | Fallback for Claude; per-user key stored in `users.anthropic_api_key` |
| `OPENAI_API_KEY` | Fallback for OpenAI; per-user key stored in `users.openai_api_key` |
| `GEMINI_API_KEY` | Fallback for Gemini; per-user key stored in `users.gemini_api_key` |

---

## Verification

```bash
# Install deps
pip install -r requirements.txt

# Start server — tables auto-created
uvicorn app.main:app --reload --port 8000

# Create session
curl -X POST http://localhost:8000/api/chat/sessions \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"llm_provider":"openai"}'

# Send message via SSE
curl -N -X POST http://localhost:8000/api/chat/sessions/1/message \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"llm_provider":"openai","message":"hello"}'

# Connect via WebSocket (wscat or browser DevTools)
# ws://localhost:8000/api/chat/ws/1?token=<jwt>
# send: {"message":"hello"}

# Fetch history
curl http://localhost:8000/api/chat/sessions/1 \
  -H "Authorization: Bearer <token>"
```

"""
LangGraph chat service.

One MemorySaver instance is shared at module level.
Each session is isolated by thread_id = str(session_id).
State is in-process only — ChatMessage rows in DB are the durable record.
"""
from __future__ import annotations
import logging
from typing import AsyncIterator, Optional

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langgraph.graph import StateGraph, MessagesState, END
from langgraph.checkpoint.memory import MemorySaver

from app.config import settings

logger = logging.getLogger(__name__)

_memory = MemorySaver()


def _make_seed_graph():
    """Compiled once at module load; used only for aget_state/aupdate_state — no LLM."""
    async def _noop(state: MessagesState):
        return {}
    b = StateGraph(MessagesState)
    b.add_node("noop", _noop)
    b.set_entry_point("noop")
    b.add_edge("noop", END)
    return b.compile(checkpointer=_memory)


_seed_graph = _make_seed_graph()

_SYSTEM_PROMPT = (
    "You are a helpful, knowledgeable AI assistant. "
    "Be concise, accurate, and friendly."
)

_PROVIDER_DEFAULTS: dict[str, str] = {
    "claude": "claude-sonnet-4-6",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.0-flash",
}


def _resolve_key(user_key: Optional[str], env_var: str) -> str:
    return (user_key or "").strip() or getattr(settings, env_var, "")


def _build_llm(provider: str, model: Optional[str], user_key: Optional[str]):
    resolved_model = (model or "").strip() or _PROVIDER_DEFAULTS[provider]

    if provider == "claude":
        from langchain_anthropic import ChatAnthropic
        key = _resolve_key(user_key, "ANTHROPIC_API_KEY")
        if not key:
            raise ValueError("No Anthropic API key configured")
        return ChatAnthropic(model=resolved_model, api_key=key, streaming=True)

    elif provider == "openai":
        from langchain_openai import ChatOpenAI
        key = _resolve_key(user_key, "OPENAI_API_KEY")
        if not key:
            raise ValueError("No OpenAI API key configured")
        return ChatOpenAI(model=resolved_model, api_key=key, streaming=True)

    elif provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        key = _resolve_key(user_key, "GEMINI_API_KEY")
        if not key:
            raise ValueError("No Google API key configured")
        return ChatGoogleGenerativeAI(model=resolved_model, google_api_key=key, streaming=True)

    raise ValueError(f"Unknown provider: {provider!r}")


def build_graph(provider: str, model: Optional[str], user_key: Optional[str] = None):
    llm = _build_llm(provider, model, user_key)

    async def chat_node(state: MessagesState):
        messages = [SystemMessage(content=_SYSTEM_PROMPT)] + list(state["messages"])
        response = await llm.ainvoke(messages)
        return {"messages": [response]}

    builder = StateGraph(MessagesState)
    builder.add_node("chat", chat_node)
    builder.set_entry_point("chat")
    builder.add_edge("chat", END)
    return builder.compile(checkpointer=_memory)


async def is_thread_seeded(thread_id: str) -> bool:
    config = {"configurable": {"thread_id": thread_id}}
    state = await _seed_graph.aget_state(config)
    return bool(state.values.get("messages"))


async def seed_memory_from_history(thread_id: str, db_messages: list) -> None:
    config = {"configurable": {"thread_id": thread_id}}
    lc_messages = []
    for msg in db_messages:
        if msg.role == "user":
            lc_messages.append(HumanMessage(content=msg.content))
        elif msg.role == "assistant":
            lc_messages.append(AIMessage(content=msg.content))

    if lc_messages:
        await _seed_graph.aupdate_state(config, {"messages": lc_messages})
        logger.debug("Seeded %d messages into thread_id=%s", len(lc_messages), thread_id)


async def stream_response(
    graph,
    user_message: str,
    thread_id: str,
) -> AsyncIterator[str]:
    config = {"configurable": {"thread_id": thread_id}}
    input_state = {"messages": [HumanMessage(content=user_message)]}

    async for event in graph.astream_events(input_state, config=config, version="v2"):
        if event.get("event") == "on_chat_model_stream":
            chunk = event.get("data", {}).get("chunk")
            if chunk and hasattr(chunk, "content") and chunk.content:
                yield chunk.content

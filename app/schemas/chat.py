from pydantic import BaseModel, field_validator
from typing import Optional, List
from datetime import datetime

_VALID_PROVIDERS = {"claude", "openai", "gemini"}


class _ProviderBase(BaseModel):
    llm_provider: str
    llm_model: Optional[str] = None

    @field_validator("llm_provider")
    @classmethod
    def validate_provider(cls, v: str) -> str:
        if v not in _VALID_PROVIDERS:
            raise ValueError(f"llm_provider must be one of {_VALID_PROVIDERS}")
        return v


class ChatSessionCreate(_ProviderBase):
    pass


class ChatSessionResponse(BaseModel):
    id: int
    title: Optional[str]
    llm_provider: str
    llm_model: Optional[str]
    created_at: datetime
    updated_at: Optional[datetime]
    model_config = {"from_attributes": True}


class ChatMessageResponse(BaseModel):
    id: int
    session_id: int
    role: str
    content: str
    created_at: datetime
    model_config = {"from_attributes": True}


class ChatHistoryResponse(BaseModel):
    session: ChatSessionResponse
    messages: List[ChatMessageResponse]


class ChatMessageRequest(_ProviderBase):
    message: str

    @field_validator("message")
    @classmethod
    def validate_message(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message cannot be empty")
        if len(v) > 3000:
            raise ValueError("message cannot exceed 3000 characters")
        return v

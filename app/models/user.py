from sqlalchemy import Column, Integer, String, DateTime, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    full_name = Column(String, nullable=True)
    aws_access_key = Column(Text, nullable=True)      # Encrypted — used for access_key auth
    aws_secret_key = Column(Text, nullable=True)      # Encrypted — used for access_key auth
    aws_region = Column(String, nullable=True)
    auth_method = Column(String, nullable=True)        # "access_key" | "assume_role"
    role_arn = Column(String, nullable=True)           # used for assume_role auth
    external_id = Column(Text, nullable=True)          # Encrypted — used for assume_role auth
    anthropic_api_key = Column(Text, nullable=True)   # Encrypted — user-provided Anthropic key
    openai_api_key    = Column(Text, nullable=True)   # Encrypted — user-provided OpenAI key
    gemini_api_key    = Column(Text, nullable=True)   # Encrypted — user-provided Gemini API key
    # Per-user salt (hex-encoded 32 bytes) used to derive the Fernet encryption key.
    # NULL means the row was created before per-user salts were introduced; those rows
    # still decrypt correctly via the legacy static salt.
    credential_salt = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    workflows = relationship("Workflow", back_populates="owner", cascade="all, delete-orphan")
    projects = relationship("Project", back_populates="owner", cascade="all, delete-orphan")


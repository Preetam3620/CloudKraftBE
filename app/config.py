from pydantic_settings import BaseSettings
from pydantic import field_validator
from typing import List, Union


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "sqlite:///./cloudkraft.db"
    
    # JWT
    SECRET_KEY: str = "your-secret-key-change-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480  # 8 hours
    
    # CORS - Can be string (comma-separated) or list
    CORS_ORIGINS: Union[str, List[str]] = "http://localhost:5173,http://localhost:3000"
    
    # AWS Credentials Encryption
    ENCRYPTION_KEY: str = "your-encryption-key-change-in-production"

    # Optional: backend's own AWS credentials for assume_role validation + deployment
    BACKEND_AWS_ACCESS_KEY: str = ""
    BACKEND_AWS_SECRET_KEY: str = ""
    BACKEND_AWS_REGION: str = "us-east-1"

    CLOUDKRAFT_IAM_ARN: str = "arn:aws:iam::REPLACE_WITH_YOUR_ACCOUNT_ID:user/cloudkraft"

    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    GEMINI_API_KEY: str = ""

    COOKIE_SECURE: bool = False

    # Terraform remote state (S3 + DynamoDB).
    # Leave blank to use local state (dev / demo mode).
    TF_STATE_BUCKET: str = ""
    TF_STATE_LOCK_TABLE: str = ""
    TF_STATE_REGION: str = "us-east-1"
    
    @field_validator('CORS_ORIGINS', mode='before')
    @classmethod
    def parse_cors_origins(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, str):
            # Parse comma-separated string into list
            return [origin.strip() for origin in v.split(',') if origin.strip()]
        return v
    
    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()


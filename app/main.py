import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import inspect, text
from app.config import settings
from app.database import engine, Base
from app import models
from app.api import auth, workflows, codegen, validation, ai, deploy, chat
from app.limiter import limiter
from app.services.terraform_runner import prewarm_plugin_cache
from app.services.workspace_cleanup import cleanup_stale_workspaces

Base.metadata.create_all(bind=engine)


def _run_column_migrations() -> None:
    """Add new columns to existing tables without dropping data."""
    inspector = inspect(engine)
    with engine.connect() as conn:
        user_cols = {c["name"] for c in inspector.get_columns("users")}
        new_user_cols = [
            ("auth_method",       "VARCHAR"),
            ("role_arn",          "VARCHAR"),
            ("external_id",       "TEXT"),
            ("anthropic_api_key", "TEXT"),
            ("credential_salt",   "VARCHAR"),
            ("openai_api_key",    "TEXT"),
            ("gemini_api_key",    "TEXT"),
        ]
        for col, col_type in new_user_cols:
            if col not in user_cols:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {col_type}"))
        deploy_cols = {c["name"] for c in inspector.get_columns("deployments")}
        if "plan_output" not in deploy_cols:
            conn.execute(text("ALTER TABLE deployments ADD COLUMN plan_output TEXT"))
        conn.commit()


_run_column_migrations()


class SecurityHeadersMiddleware(BaseHTTPMiddleware):

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=()"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    import subprocess as _sp
    try:
        _sp.run(["pkill", "-9", "-f", "terraform-provider-aws"], capture_output=True)
    except Exception:
        pass
    prewarm_plugin_cache()
    cleanup_stale_workspaces()
    yield


_disable_docs = os.getenv("DISABLE_DOCS", "false").lower() in ("1", "true", "yes")

app = FastAPI(
    title="CloudKraft API",
    description="Backend API for CloudKraft - Visual workflow designer for AWS infrastructure",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None if _disable_docs else "/docs",
    redoc_url=None if _disable_docs else "/redoc",
    openapi_url=None if _disable_docs else "/openapi.json",
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(workflows.router)
app.include_router(codegen.router)
app.include_router(validation.router)
app.include_router(ai.router)
app.include_router(deploy.router)
app.include_router(chat.router)


@app.get("/")
def root():
    return {"message": "CloudKraft API", "version": "1.0.0", "docs": "/docs"}


@app.get("/health")
def health_check():
    return {"status": "healthy"}

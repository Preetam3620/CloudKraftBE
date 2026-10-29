from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from app.config import settings, IS_VERCEL

_is_sqlite = settings.DATABASE_URL.startswith("sqlite")

if IS_VERCEL and _is_sqlite:
    raise RuntimeError(
        "DATABASE_URL points at SQLite, which cannot persist on Vercel. "
        "Attach a Postgres database (e.g. Neon) and set DATABASE_URL."
    )

if _is_sqlite:
    engine = create_engine(settings.DATABASE_URL, connect_args={"check_same_thread": False})
else:
    # pre_ping/recycle: serverless instances sit idle and the DB drops their connections
    engine = create_engine(
        settings.DATABASE_URL,
        pool_pre_ping=True,
        pool_recycle=300,
        pool_size=5,
        max_overflow=5,
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """Dependency for getting database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


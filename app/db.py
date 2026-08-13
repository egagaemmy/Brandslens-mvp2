"""Database session. SQLite by default for local dev — genuinely zero setup.
Set DATABASE_URL to a Postgres URL in production so the web service and the
worker share one real, persistent database instead of each having its own
private SQLite file that the other can never see.
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .models import Base

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./brandslens_mvp.db")

# Render (and formerly Heroku) hand out connection strings starting with
# "postgres://", but modern SQLAlchemy requires "postgresql://" — without
# this, the app crashes on startup with a confusing dialect error. This one
# line is the fix for that specific, very common gotcha.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

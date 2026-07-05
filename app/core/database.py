from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import get_settings

settings = get_settings()

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


def init_db() -> None:
    from app.models import uploaded_file  # noqa: F401 - registers models before create_all

    Base.metadata.create_all(bind=engine)


def get_db_session():
    """FastAPI dependency: yields a request-scoped session, closed afterwards."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

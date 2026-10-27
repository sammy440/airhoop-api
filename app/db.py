from collections.abc import Iterator

from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

from .config import get_settings

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings().db_url, pool_pre_ping=True)
    return _engine


def set_engine(engine: Engine | None) -> None:
    """Tests point the app at the test database."""
    global _engine
    _engine = engine


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session

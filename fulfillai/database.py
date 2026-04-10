from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base

DB_PATH = Path(__file__).parent / "fulfillai.db"
ENGINE = create_engine(f"sqlite:///{DB_PATH}", echo=False)
SessionLocal = sessionmaker(bind=ENGINE)


def init_db():
    Base.metadata.create_all(ENGINE)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

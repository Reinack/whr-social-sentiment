"""
db.py — conexión centralizada a PostgreSQL via SQLAlchemy

Prioridad de conexión:
  1. DATABASE_URL  (Neon / Render — una sola variable, igual que Football-Analytics)
  2. DB_USER + DB_PASSWORD + DB_HOST + DB_PORT + DB_NAME  (Docker local / .env)
"""
import os
from contextlib import contextmanager
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

load_dotenv()

def get_engine():
    url = os.getenv("DATABASE_URL")

    if url:
        url = url.strip()
        # Neon puede entregar "postgres://" o "postgresql://" — SQLAlchemy necesita "postgresql+psycopg2://"
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    else:
        url = (
            f"postgresql+psycopg2://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
            f"@{os.getenv('DB_HOST', 'localhost')}:{os.getenv('DB_PORT', 5432)}"
            f"/{os.getenv('DB_NAME')}"
        )

    return create_engine(url, pool_pre_ping=True, echo=False)

engine = get_engine()
SessionLocal = sessionmaker(bind=engine)

@contextmanager
def get_session():
    """Context manager para sesiones con commit/rollback automático."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

def test_connection():
    with get_session() as s:
        result = s.execute(text("SELECT version()")).scalar()
        print(f"✓ Conectado: {result[:40]}...")

if __name__ == "__main__":
    test_connection()

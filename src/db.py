"""PostgreSQL persistence layer for Sentinel.

Provides SQLAlchemy models and repository abstractions for all persistent entities.
Failures are surfaced explicitly — the trading path never depends on DB availability
for risk decisions, but persistence is used for audit, decisions, executions,
and observability.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    metadata = metadata


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    role = Column(String(20), nullable=False, default="VIEWER")
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)


class Account(Base):
    __tablename__ = "accounts"

    id = Column(String(64), primary_key=True)
    paper_trading = Column(Boolean, nullable=False, default=True)
    cash = Column(Float, default=0.0)
    buying_power = Column(Float, default=0.0)
    equity = Column(Float, default=0.0)
    daily_pnl = Column(Float, default=0.0)
    currency = Column(String(8), default="USD")
    account_status = Column(String(32), default="ACTIVE")
    trading_mode = Column(String(16), default="paper")
    as_of = Column(DateTime(timezone=True), default=_now)


class Position(Base):
    __tablename__ = "positions"

    id = Column(String(64), primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    quantity = Column(Float, nullable=False)
    average_entry = Column(Float, nullable=False)
    market_value = Column(Float, nullable=False)
    unrealized_pnl = Column(Float, default=0.0)
    as_of = Column(DateTime(timezone=True), default=_now)


class Order(Base):
    __tablename__ = "orders"

    id = Column(String(128), primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    side = Column(String(8), nullable=False)
    qty = Column(Float, nullable=False)
    order_type = Column(String(16), default="market", nullable=False)
    status = Column(String(32), nullable=False)
    decision_id = Column(String(64), ForeignKey("decisions.id"), nullable=True)
    run_id = Column(String(64), nullable=False, index=True)
    submitted_at = Column(DateTime(timezone=True), default=_now)
    filled_at = Column(DateTime(timezone=True), nullable=True)
    filled_qty = Column(Float, default=0.0)
    avg_fill_price = Column(Float, nullable=True)


class Execution(Base):
    __tablename__ = "executions"

    id = Column(String(64), primary_key=True)
    order_id = Column(String(128), ForeignKey("orders.id"), nullable=False)
    symbol = Column(String(16), nullable=False, index=True)
    side = Column(String(8), nullable=False)
    qty = Column(Float, nullable=False)
    client_order_id = Column(String(128), nullable=False, unique=True, index=True)
    status = Column(String(32), nullable=False)
    executed_at = Column(DateTime(timezone=True), default=_now)
    decision_id = Column(String(64), nullable=True)
    run_id = Column(String(64), nullable=True, index=True)


class Decision(Base):
    __tablename__ = "decisions"

    id = Column(String(64), primary_key=True)
    run_id = Column(String(64), nullable=False, index=True)
    symbol = Column(String(16), nullable=False, index=True)
    action = Column(String(8), nullable=False)
    confidence = Column(Float, nullable=False)
    thesis = Column(Text, nullable=True)
    entry_reason = Column(Text, nullable=True)
    position_size = Column(Integer, default=0, nullable=False)
    decision_price = Column(Float, nullable=True)
    model = Column(String(128), nullable=True)
    provider = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, index=True)


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id = Column(String(64), primary_key=True)
    symbol = Column(String(16), nullable=False)
    side = Column(String(8), nullable=False)
    qty = Column(Float, nullable=False)
    allowed = Column(Boolean, nullable=False)
    reason_code = Column(String(64), nullable=False)
    reason = Column(Text, nullable=True)
    decision_id = Column(String(64), nullable=True, index=True)
    run_id = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, index=True)


class AgentEvent(Base):
    __tablename__ = "agent_events"

    id = Column(String(64), primary_key=True)
    event_type = Column(String(64), nullable=False, index=True)
    severity = Column(String(16), default="info", nullable=False)
    run_id = Column(String(64), nullable=True, index=True)
    decision_id = Column(String(64), nullable=True)
    execution_id = Column(String(64), nullable=True)
    symbol = Column(String(16), nullable=True)
    timestamp = Column(Float, nullable=False)
    fields = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), default=_now, index=True)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    id = Column(String(64), primary_key=True)
    worker_id = Column(String(64), nullable=False, unique=True, index=True)
    started_at = Column(DateTime(timezone=True), default=_now)
    last_heartbeat = Column(DateTime(timezone=True), nullable=False, index=True)
    last_cycle_started = Column(DateTime(timezone=True), nullable=True)
    last_cycle_completed = Column(DateTime(timezone=True), nullable=True)
    last_success = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    current_state = Column(String(32), default="starting")
    version = Column(String(32), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)


class WorkerLease(Base):
    __tablename__ = "worker_leases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    worker_id = Column(String(64), nullable=False, index=True)
    acquired_at = Column(DateTime(timezone=True), default=_now)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    last_renewed_at = Column(DateTime(timezone=True), default=_now)
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    released_at = Column(DateTime(timezone=True), nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id = Column(String(64), primary_key=True)
    event_type = Column(String(64), nullable=False, index=True)
    actor_id = Column(String(64), nullable=True)
    actor_role = Column(String(32), nullable=True)
    timestamp = Column(DateTime(timezone=True), default=_now, index=True)
    ip_address = Column(String(48), nullable=True)
    user_agent = Column(Text, nullable=True)
    resource = Column(String(255), nullable=True)
    outcome = Column(String(32), nullable=True)
    details = Column(JSON, nullable=False, default=dict)


class SystemHealth(Base):
    __tablename__ = "system_health"

    id = Column(Integer, primary_key=True, autoincrement=True)
    component = Column(String(64), nullable=False, index=True)
    status = Column(String(32), nullable=False)
    details = Column(JSON, nullable=False, default=dict)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)


class IdempotencyState(Base):
    __tablename__ = "idempotency_states"

    idempotency_key = Column(String(128), primary_key=True)
    run_id = Column(String(64), nullable=False)
    decision_id = Column(String(64), nullable=True)
    execution_id = Column(String(64), nullable=True)
    symbol = Column(String(16), nullable=False)
    side = Column(String(8), nullable=False)
    qty = Column(Float, nullable=False)
    status = Column(String(32), nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now)


_db_url_cache: str | None = None
_engine: Any = None
_SessionLocal: Any = None

# Every table the production runtime depends on. `ensure_database()` (startup)
# refuses to start the API/worker when any of these is missing after migration.
REQUIRED_TABLES: tuple[str, ...] = (
    "worker_heartbeats",
    "worker_leases",
    "decisions",
    "orders",
    "executions",
    "risk_events",
    "agent_events",
    "system_health",
    "audit_events",
    "idempotency_states",
)


def verify_schema() -> list[str]:
    """Return the names of required tables missing from the database.

    Empty list means the schema is complete. Raises only on inspection failure.
    """
    from sqlalchemy import inspect

    engine = get_engine()
    existing = set(inspect(engine).get_table_names())
    return [table for table in REQUIRED_TABLES if table not in existing]


def get_db_url() -> str:
    global _db_url_cache
    if _db_url_cache is None:
        _db_url_cache = os.environ.get("DATABASE_URL", "").strip()
    return _db_url_cache


def reset_db_state() -> None:
    """Reset cached engine/session state (used by tests and config reloads)."""
    global _db_url_cache, _engine, _SessionLocal
    _db_url_cache = None
    _engine = None
    _SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        url = get_db_url()
        if not url:
            from errors import ConfigurationError
            raise ConfigurationError("DATABASE_URL is not set")
        _engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)
    return _engine


def get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        engine = get_engine()
        _SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return _SessionLocal


def get_session() -> Session:
    factory = get_session_factory()
    return factory()


@contextmanager
def db_session() -> Iterator[Session]:
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def is_database_available() -> bool:
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(select(1).limit(1))
        return True
    except Exception:  # noqa: BLE001, S110
        pass
    return False


def init_db(url: str | None = None) -> None:
    if url is not None:
        global _engine, _SessionLocal, _db_url_cache
        _db_url_cache = url
        _engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, expire_on_commit=False)
    engine = get_engine()
    metadata.create_all(engine)

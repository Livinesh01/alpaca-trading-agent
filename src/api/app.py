from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import structlog
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

# Normalize imports for both uvicorn and direct test imports.
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))
from api.auth import Role, require_admin, require_operator, require_trader, require_viewer
from api.schemas import (
    Account,
    APIEnvelope,
    BacktestInfo,
    BacktestRequest,
    ErrorBody,
    EvaluationInfo,
    Health,
    KillSwitch,
    MarketData,
    OrderPreview,
    OrderPreviewRequest,
    RiskState,
    System,
)
from api.services import (
    account_service,
    activity_service,
    backtest_service,
    decision_service,
    evaluation_service,
    market_service,
    order_service,
    position_service,
    risk_service,
)
from api.services.data_source import (
    MarketDataUnavailable,
    NoDataSource,
    close_data_source,
    get_data_source,
)
from auth import auth_mode
from memory import MemoryStore
from observability import Observability

APP_VERSION = "0.3.0"
MAX_REQUEST_BYTES = 1_000_000
RATE_LIMIT_PER_MINUTE = 120
WORKER_STATUS_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "journal", "observability", "worker.json"))
_SSE_LISTENERS: list = []
_SSE_LOCK = threading.Lock()
_rate_window: dict[str, list[float]] = {}
_rate_limit_lock = threading.Lock()
shutdown_event = threading.Event()

logger = structlog.get_logger()

if os.environ.get("SENTINEL_LOAD_DOTENV", "").strip().lower() in {"1", "true", "yes", "on"}:
    load_dotenv(os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", ".env")), override=False)


def _database_available() -> bool:
    try:
        from db import is_database_available

        return is_database_available()
    except Exception:  # noqa: BLE001, S110
        pass
    return False


def _worker_healthy() -> dict[str, Any]:
    worker: dict[str, Any] = {}
    try:
        with open(WORKER_STATUS_PATH, encoding="utf-8") as stream:
            value = json.load(stream)
        if isinstance(value, dict):
            worker = value
    except (OSError, TypeError, ValueError):
        pass
    heartbeat = worker.get("last_heartbeat")
    heartbeat_fresh = isinstance(heartbeat, (int, float)) and time.time() - heartbeat <= 600
    return {
        "status": "healthy" if worker.get("status") in {"running", "cycling"} and heartbeat_fresh else "degraded",
        "worker_status": worker.get("status", "unknown"),
        "last_heartbeat": worker.get("last_heartbeat"),
        "heartbeat_fresh": heartbeat_fresh,
        "last_error": worker.get("last_error"),
        "worker_version": worker.get("version"),
        "db_available": _database_available(),
    }


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("starting_sentinel_api", version=APP_VERSION, auth_mode=str(auth_mode()))
    yield
    shutdown_event.set()
    close_data_source()
    logger.info("sentinel_api_stopped")


app = FastAPI(title="Sentinel API", version=APP_VERSION, docs_url="/docs", redoc_url=None, lifespan=lifespan)

origins = [item.strip() for item in os.environ.get("API_CORS_ORIGINS", "").split(",") if item.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False, allow_methods=["GET", "POST"], allow_headers=["Content-Type", "X-Dev-Role", "X-Request-ID"])
VIEWER_DEP = Depends(require_viewer)
TRADER_DEP = Depends(require_trader)


def _memory() -> MemoryStore:
    return MemoryStore()


def _observability() -> Observability:
    return Observability()


def _envelope(request: Request, data: Any) -> dict[str, Any]:
    return {"data": data, "request_id": getattr(request.state, "request_id", "unknown"), "mode": "READ_ONLY"}


def _data_source() -> Any:
    try:
        return get_data_source()
    except MarketDataUnavailable as exc:
        return NoDataSource(exc.reason)


def _limits() -> dict[str, Any]:
    from agent.decision_loop import default_limits

    return default_limits()


def _watchlist() -> list[str]:
    return [str(symbol).upper() for symbol in _limits().get("watchlist", [])]


def _paginate(items: list[Any], page: int, page_size: int) -> dict[str, Any]:
    page = max(int(page), 1)
    page_size = max(min(int(page_size), 200), 1)
    total = len(items)
    start = (page - 1) * page_size
    return {"items": items[start : start + page_size], "pagination": {"page": page, "page_size": page_size, "total": total}}


def _public_health() -> dict[str, Any]:
    paper = os.environ.get("PAPER_TRADING", "").strip().lower() in {"1", "true", "yes", "on"}
    alpaca_paper = os.environ.get("ALPACA_PAPER", "true").strip().lower() in {"1", "true", "yes", "on"}
    worker_info = _worker_healthy()
    return {
        "status": "healthy" if worker_info["status"] == "healthy" and worker_info["db_available"] and paper and alpaca_paper else "degraded",
        "backend": os.environ.get("AGENT_BACKEND", "decision_loop"),
        "database": "available" if worker_info["db_available"] else "unavailable",
        "worker": worker_info["worker_status"],
        "alpaca": "configured" if os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY") else "not_configured",
        "market_data": "configured" if os.environ.get("SENTINEL_DATA_MODE", "offline").strip().lower() == "proxy" else "disabled",
        "llm": os.environ.get("LLM_PROVIDER", "unknown"),
        "paper_trading": paper,
        "alpaca_paper": alpaca_paper,
        "kill_switch": os.environ.get("TRADING_KILL_SWITCH", "").strip().lower() in {"1", "true", "yes", "on"},
        "last_heartbeat": worker_info["last_heartbeat"],
        "heartbeat_fresh": worker_info["heartbeat_fresh"],
        "auth_mode": os.environ.get("API_AUTH_MODE", "disabled"),
        "version": APP_VERSION,
    }


@app.get("/health")
def public_health() -> dict[str, Any]:
    return _public_health()


@app.get("/ready")
def public_ready() -> dict[str, Any]:
    health_data = _public_health()
    if health_data["status"] != "healthy" or not health_data["paper_trading"]:
        return JSONResponse(status_code=503, content=health_data)
    return health_data


@app.get("/api/v1/health/stream")
async def sse_stream(request: Request):
    """Server-Sent Events stream for real-time observability events.

    Behavior:
    - Real EventSource clients (``Accept: text/event-stream``) get a live,
      fully-redacted stream of observability events with keepalive heartbeats.
    - Any other client (browser address bar, health checks, tests) gets a
      bounded JSON snapshot of the most recent events instead of a never-ending
      stream, so the endpoint never hangs a normal HTTP request.

    The stream is read-only and every event is passed through `_redact_sse`
    before leaving the process; no secret-like fields can ever be emitted.
    """
    accept = request.headers.get("accept", "")
    from observability import Observability

    obs = Observability()
    last_offset = 0
    ping_interval = 30

    if "text/event-stream" not in accept:
        events = _read_events(obs.events_path, 0)[0][-50:]
        return JSONResponse(
            {
                "data": [_redact_sse(event) for event in events],
                "request_id": getattr(request.state, "request_id", "unknown"),
                "stream": "use Accept: text/event-stream for live updates",
            }
        )

    with _SSE_LOCK:
        _SSE_LISTENERS.append(request)

    try:
        from asyncio import sleep as asyncio_sleep

        async def _generator():
            nonlocal last_offset
            yield "retry: 5000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                if shutdown_event.is_set():
                    yield 'data: {"type":"shutdown"}\n\n'
                    break
                try:
                    events, new_offset = _read_events(obs.events_path, last_offset)
                    if events:
                        last_offset = new_offset
                        for event in events:
                            safe = _redact_sse(event)
                            yield f'data: {json.dumps(safe, separators=(",", ":"))}\n\n'
                except Exception:  # noqa: BLE001, S110
                    pass
                # Sleep in small slices so client disconnects are honored promptly
                # (a full 30s sleep would delay clean teardown).
                for _ in range(int(ping_interval)):
                    if await request.is_disconnected() or shutdown_event.is_set():
                        return
                    await asyncio_sleep(1)
                yield 'data: {"type":"heartbeat"}\n\n'

        return StreamingResponse(
            _generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    finally:
        with _SSE_LOCK:
            if request in _SSE_LISTENERS:
                _SSE_LISTENERS.remove(request)


def _read_events(path: Any, since_offset: int) -> tuple[list[dict[str, Any]], int]:
    """Read new observability events from JSONL since last line offset.

    Returns ``(events, new_offset)`` where ``new_offset`` is the number of lines
    consumed so far, suitable for the next call.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return [], since_offset
    if not content:
        return [], since_offset
    lines = content.splitlines()
    if since_offset >= len(lines):
        return [], len(lines)
    results: list[dict[str, Any]] = []
    for line in lines[since_offset:]:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            if isinstance(event, dict):
                results.append(event)
        except (TypeError, json.JSONDecodeError):
            continue
    return results, len(lines)


def _redact_sse(event: dict[str, Any]) -> dict[str, Any]:
    """Ensure no secret-like fields leak through the SSE stream."""
    safe = {}
    for key, value in event.items():
        lowered = str(key).lower()
        if any(word in lowered for word in ("api_key", "secret", "token", "password", "credential")):
            safe[key] = "[REDACTED]"
        else:
            safe[key] = value
    return safe


@app.middleware("http")
async def request_controls(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or f"req-{uuid.uuid4().hex}"
    request.state.request_id = request_id[:100]
    now = time.monotonic()
    client_key = request.client.host if request.client else "unknown"
    try:
        rate_limit = max(int(os.environ.get("API_RATE_LIMIT_PER_MINUTE", str(RATE_LIMIT_PER_MINUTE))), 1)
    except ValueError:
        rate_limit = 120
    with _rate_limit_lock:
        recent = [stamp for stamp in _rate_window.get(client_key, []) if now - stamp < 60]
        if len(recent) >= rate_limit:
            return JSONResponse(status_code=429, content=ErrorBody(code="rate_limited", message="request rate limit exceeded", request_id=request.state.request_id).model_dump())
        recent.append(now)
        _rate_window[client_key] = recent
    if request.headers.get("content-length") and int(request.headers["content-length"]) > MAX_REQUEST_BYTES:
        return JSONResponse(status_code=413, content=ErrorBody(code="request_too_large", message="request is too large", request_id=request.state.request_id).model_dump())
    try:
        response = await call_next(request)
    except Exception:  # noqa: BLE001 — no stack traces cross the API boundary
        response = JSONResponse(status_code=500, content=ErrorBody(code="internal_error", message="internal server error", request_id=request.state.request_id).model_dump())
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    messages = {401: "authentication required", 403: "access forbidden", 404: "resource not found", 405: "method not allowed", 429: "request rate limit exceeded", 503: "authentication is not configured"}
    code = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed", 429: "rate_limited", 503: "not_configured"}.get(exc.status_code, "request_error")
    return JSONResponse(status_code=exc.status_code, content=ErrorBody(code=code, message=messages.get(exc.status_code, "request rejected"), request_id=getattr(request.state, "request_id", "unknown")).model_dump())


@app.get("/api/v1/health", response_model=APIEnvelope[Health])
def health(request: Request, _: Role = VIEWER_DEP):
    public = _public_health()
    health_data = _observability().read_status()
    kill = bool(health_data.get("kill_switch_enabled", False)) or public.get("kill_switch", False)
    return _envelope(request, Health(
        status=public["status"],
        paper_trading=bool(public["paper_trading"]),
        kill_switch=kill,
        llm_provider=health_data.get("provider") or public.get("llm"),
        market_data=public.get("market_data"),
        market_data_fresh=bool(public.get("heartbeat_fresh")),
        last_success=health_data.get("last_successful_run"),
        version=APP_VERSION,
        authentication=f"{public.get('auth_mode', 'unknown')} auth",
        backend=public.get("backend"),
        database=public.get("database"),
        worker=public.get("worker"),
        alpaca=public.get("alpaca"),
        alpaca_paper=public.get("alpaca_paper"),
        last_heartbeat=public.get("last_heartbeat"),
        heartbeat_fresh=public.get("heartbeat_fresh"),
        auth_mode=public.get("auth_mode"),
    ))


@app.get("/api/v1/account", response_model=APIEnvelope[Account])
def account(request: Request, _: Role = VIEWER_DEP):
    return _envelope(request, Account(**account_service.get_account(_data_source())))


@app.get("/api/v1/positions")
def positions(request: Request, _: Role = VIEWER_DEP, page: int = 1, page_size: int = 50):
    payload = position_service.get_positions(_data_source())
    payload.pop("equity", None)
    paged = _paginate(payload.pop("items", []), page, page_size)
    payload.update(paged)
    return _envelope(request, payload)


@app.get("/api/v1/orders")
def orders(request: Request, _: Role = VIEWER_DEP, page: int = 1, page_size: int = 50):
    payload = order_service.get_orders(_data_source(), _memory().decisions())
    paged = _paginate(payload.pop("items", []), page, page_size)
    payload.update(paged)
    return _envelope(request, payload)


@app.get("/api/v1/market-data", response_model=APIEnvelope[MarketData])
def market_data(
    request: Request,
    _: Role = VIEWER_DEP,
    symbol: str | None = Query(default=None, min_length=1, max_length=10),
    timeframe: str = Query(default="1Day"),
    days: int = Query(default=180, ge=1, le=1000),
    limit: int = Query(default=500, ge=2, le=2000),
):
    limits = _limits()
    source = _data_source()
    if symbol:
        return _envelope(request, MarketData(**market_service.get_market_data(source, symbol, limits=limits, timeframe=timeframe, days=days, limit=limit)))
    # No symbol: project the configured watchlist (explicit availability per row).
    rows = [market_service.get_market_data(source, item, limits=limits, timeframe=timeframe, days=days, limit=limit) for item in _watchlist()]
    return _envelope(
        request,
        MarketData(
            symbol="WATCHLIST",
            available=any(row.get("available") for row in rows),
            is_fresh=any(row.get("is_fresh") for row in rows),
            source="alpaca_paper",
            reason=None,
            signals={},
            bars=[],
        ).model_copy(update={"signals": {"watchlist": rows}}),
    )


@app.get("/api/v1/market-data/{symbol}", response_model=APIEnvelope[MarketData])
def market_data_symbol(
    symbol: str,
    request: Request,
    _: Role = VIEWER_DEP,
    timeframe: str = Query(default="1Day"),
    days: int = Query(default=180, ge=1, le=1000),
    limit: int = Query(default=500, ge=2, le=2000),
):
    return _envelope(request, MarketData(**market_service.get_market_data(_data_source(), symbol, limits=_limits(), timeframe=timeframe, days=days, limit=limit)))


@app.get("/api/v1/market-data/{symbol}/signals")
def market_signals(
    symbol: str,
    request: Request,
    _: Role = VIEWER_DEP,
    timeframe: str = Query(default="1Day"),
    days: int = Query(default=180, ge=1, le=1000),
    limit: int = Query(default=500, ge=2, le=2000),
):
    payload = market_service.get_market_data(_data_source(), symbol, limits=_limits(), timeframe=timeframe, days=days, limit=limit)
    return _envelope(
        request,
        {
            "symbol": payload.get("symbol"),
            "available": payload.get("available"),
            "signals": payload.get("signals"),
            "current_price": payload.get("current_price"),
            "data_timestamp": payload.get("data_timestamp"),
            "age_seconds": payload.get("age_seconds"),
            "is_fresh": payload.get("is_fresh"),
            "source": payload.get("source"),
            "reason": payload.get("reason"),
        },
    )


@app.get("/api/v1/decisions")
def decisions(
    request: Request,
    _: Role = VIEWER_DEP,
    page: int = 1,
    page_size: int = 50,
    symbol: str | None = None,
    action: str | None = None,
    run_id: str | None = None,
    decision_id: str | None = None,
    confidence_min: float | None = None,
    confidence_max: float | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    return _envelope(
        request,
        decision_service.list_decisions(
            _memory(),
            page=page,
            page_size=page_size,
            symbol=symbol,
            action=action,
            run_id=run_id,
            decision_id=decision_id,
            confidence_min=confidence_min,
            confidence_max=confidence_max,
            date_from=date_from,
            date_to=date_to,
        ),
    )


@app.get("/api/v1/decisions/{decision_id}/replay")
def decision_replay(decision_id: str, request: Request, _: Role = VIEWER_DEP):
    replay = decision_service.replay_decision(_memory(), decision_id)
    if replay is None:
        raise HTTPException(status_code=404)
    return _envelope(request, replay)


@app.get("/api/v1/executions")
def executions(request: Request, _: Role = VIEWER_DEP, page: int = 1, page_size: int = 50):
    items = _memory().executions()
    return _envelope(request, {"informational": True, **_paginate(items, page, page_size)})


@app.get("/api/v1/risk", response_model=APIEnvelope[RiskState])
def risk(request: Request, _: Role = VIEWER_DEP):
    return _envelope(request, RiskState(**risk_service.get_risk(_data_source(), _memory(), _observability(), _limits())))


@app.get("/api/v1/risk/kill-switch", response_model=APIEnvelope[KillSwitch])
def kill_switch(request: Request, _: Role = VIEWER_DEP):
    state = _observability().read_status()
    return _envelope(request, KillSwitch(enabled=bool(state.get("kill_switch_enabled", False))))


@app.get("/api/v1/activity")
def activity(
    request: Request,
    _: Role = VIEWER_DEP,
    page: int = 1,
    page_size: int = 50,
    event_type: str | None = None,
    run_id: str | None = None,
    decision_id: str | None = None,
    execution_id: str | None = None,
    outcome_id: str | None = None,
    symbol: str | None = None,
    time_from: str | None = None,
    time_to: str | None = None,
):
    return _envelope(
        request,
        activity_service.list_activity(
            _observability(),
            page=page,
            page_size=page_size,
            event_type=event_type,
            run_id=run_id,
            decision_id=decision_id,
            execution_id=execution_id,
            outcome_id=outcome_id,
            symbol=symbol,
            time_from=time_from,
            time_to=time_to,
        ),
    )


@app.get("/api/v1/backtests")
def backtests(request: Request, _: Role = VIEWER_DEP, page: int = 1, page_size: int = 50, symbol: str | None = None):
    return _envelope(request, backtest_service.list_backtests(page=page, page_size=page_size, symbol=symbol))


@app.post("/api/v1/backtests", response_model=APIEnvelope[BacktestInfo], status_code=201)
def backtest_create(payload: BacktestRequest, request: Request, _: Role = TRADER_DEP):
    try:
        bars = _data_source().get_bars(payload.symbol, timeframe="1Day", days=365, limit=500)
    except MarketDataUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"backtest unavailable: {exc.reason}") from exc
    if not bars:
        raise HTTPException(status_code=422, detail="no real bars available for backtest (no fake data is used)")
    record = backtest_service.run_backtest(
        symbol=payload.symbol,
        bars=bars,
        limits=_limits(),
        starting_capital=payload.starting_capital,
        transaction_cost=payload.transaction_cost,
        slippage=payload.slippage,
    )
    return _envelope(request, BacktestInfo(**record))


@app.get("/api/v1/backtests/{backtest_id}")
def backtest_detail(backtest_id: str, request: Request, _: Role = VIEWER_DEP):
    record = backtest_service.get_backtest(backtest_id)
    if record is None:
        raise HTTPException(status_code=404)
    return _envelope(request, record)


@app.get("/api/v1/evaluations")
def evaluations(request: Request, _: Role = VIEWER_DEP, page: int = 1, page_size: int = 50):
    return _envelope(request, evaluation_service.list_evaluations(page=page, page_size=page_size))


@app.post("/api/v1/evaluations", response_model=APIEnvelope[EvaluationInfo], status_code=201)
def evaluation_create(payload: BacktestRequest, request: Request, _: Role = TRADER_DEP):
    try:
        bars = _data_source().get_bars(payload.symbol, timeframe="1Day", days=365, limit=500)
    except MarketDataUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"evaluation unavailable: {exc.reason}") from exc
    if not bars:
        raise HTTPException(status_code=422, detail="no real bars available for evaluation (no fake data is used)")
    record = evaluation_service.run_evaluation(
        symbol=payload.symbol,
        bars=bars,
        limits=_limits(),
        starting_capital=payload.starting_capital,
        transaction_cost=payload.transaction_cost,
        slippage=payload.slippage,
    )
    return _envelope(request, EvaluationInfo(**record))


@app.get("/api/v1/evaluations/{evaluation_id}")
def evaluation_detail(evaluation_id: str, request: Request, _: Role = VIEWER_DEP):
    record = evaluation_service.get_evaluation(evaluation_id)
    if record is None:
        raise HTTPException(status_code=404)
    return _envelope(request, record)


@app.get("/api/v1/system", response_model=APIEnvelope[System])
def system(request: Request, _: Role = VIEWER_DEP):
    state = _observability().read_status()
    return _envelope(request, System(backend=str(state.get("backend", os.environ.get("AGENT_BACKEND", "decision_loop"))), provider=state.get("provider"), paper_trading=os.environ.get("PAPER_TRADING", "").lower() == "true", dry_run=bool(state.get("dry_run", False)), kill_switch=bool(state.get("kill_switch_enabled", False)), health=state))


@app.get("/api/v1/status")
def status_view(request: Request, _: Role = VIEWER_DEP):
    data = _public_health()
    data["config"] = {}
    try:
        from config import SentinelConfig

        cfg = SentinelConfig.load()
        data["config"] = cfg.to_safe_dict()
    except Exception as exc:  # noqa: BLE001
        data["config_error"] = type(exc).__name__
    return _envelope(request, data)


@app.post("/api/v1/auth/token")
def issue_token(request: Request, role: str = Query(default="VIEWER"), audience: str | None = None):
    """Issue a short-lived JWT for development/testing.

    In production, JWTs are issued by the OIDC provider — this endpoint is
    disabled in production mode. It exists only to bootstrap development
    and testing against the JWT auth path.
    """
    from auth import AuthMode, auth_mode, create_token

    mode = auth_mode()
    if mode == AuthMode.PRODUCTION:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="token issuance in production is handled by the OIDC provider")
    if role.upper() not in {"VIEWER", "TRADER", "OPERATOR", "ADMIN"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid role")
    import audit

    audit.log_audit_event(
        "token_issued",
        actor_id="system",
        actor_role="SYSTEM",
        resource="/api/v1/auth/token",
        outcome="success",
        request_id=getattr(request.state, "request_id", "unknown"),
        role=role,
    )
    token = create_token(user_id="dev-user", role=role.upper())
    return _envelope(request, {"token": token, "token_type": "Bearer", "expires_in": 3600, "role": role.upper()})


@app.get("/api/v1/audit")
def audit_log(
    request: Request,
    _: Role = VIEWER_DEP,
    page: int = 1,
    page_size: int = 50,
    event_type: str | None = None,
    actor_id: str | None = None,
    outcome: str | None = None,
):
    import audit

    return _envelope(request, audit.read_audit_log(page=page, page_size=page_size, event_type=event_type, actor_id=actor_id, outcome=outcome))


@app.post("/api/v1/orders/preview", response_model=APIEnvelope[OrderPreview], status_code=503)
def order_preview(payload: OrderPreviewRequest, request: Request, _: Role = TRADER_DEP):
    return _envelope(request, OrderPreview(status="UNAVAILABLE", symbol=payload.symbol.upper(), side=payload.side, paper_trading=False, risk="NOT_EVALUATED", final_gate="NOT_EVALUATED", execution="NOT_SUBMITTED", reason="preview requires a configured authenticated market-data and safety adapter"))


@app.post("/api/v1/orders", status_code=501)
def order_submission_disabled(request: Request, _: Role = TRADER_DEP):
    raise HTTPException(status_code=501, detail="order submission is disabled at the API layer")


@app.post("/api/v1/risk/kill-switch", status_code=501)
def kill_switch_mutation(request: Request, role: Role = Depends(require_admin)):  # noqa: B008
    raise HTTPException(status_code=501, detail="kill-switch mutation is disabled until production authentication and authorization are configured")

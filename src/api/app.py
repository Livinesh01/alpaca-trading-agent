from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# app.py is launched as `src.api.app:app` (uvicorn) from the container WORKDIR and
# may also be imported as `api.app`; normalize the search path so the flat
# top-level imports below (`from api...`, `from memory`, `from agent...`) resolve
# regardless of cwd or PYTHONPATH. Mirrors the convention in orchestrator.py.
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))
from api.auth import Role, require_trader, require_viewer
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
from memory import MemoryStore
from observability import Observability

APP_VERSION = "0.2.0"
MAX_REQUEST_BYTES = 1_000_000
RATE_LIMIT_PER_MINUTE = 120
_rate_window: dict[str, list[float]] = {}
_rate_limit_lock = threading.Lock()
load_dotenv(os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", ".env")), override=False)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    close_data_source()


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
    health_data = _observability().read_status()
    kill = bool(health_data.get("kill_switch_enabled", False))
    paper = os.environ.get("PAPER_TRADING", "").strip().lower() in {"1", "true", "yes", "on"}
    return _envelope(request, Health(status="healthy", paper_trading=paper, kill_switch=kill, llm_provider=health_data.get("provider"), market_data="not_connected", market_data_fresh=False, last_success=health_data.get("last_successful_run"), version=APP_VERSION, authentication="development-only"))


@app.get("/api/v1/status")
def status_view(request: Request, _: Role = VIEWER_DEP):
    return _envelope(request, _observability().read_status())


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


@app.post("/api/v1/orders/preview", response_model=APIEnvelope[OrderPreview], status_code=503)
def order_preview(payload: OrderPreviewRequest, request: Request, _: Role = TRADER_DEP):
    return _envelope(request, OrderPreview(status="UNAVAILABLE", symbol=payload.symbol.upper(), side=payload.side, paper_trading=False, risk="NOT_EVALUATED", final_gate="NOT_EVALUATED", execution="NOT_SUBMITTED", reason="preview requires a configured authenticated market-data and safety adapter"))


@app.post("/api/v1/orders", status_code=501)
def order_submission_disabled(request: Request, _: Role = TRADER_DEP):
    raise HTTPException(status_code=501, detail="order submission is disabled at the API layer")


@app.post("/api/v1/risk/kill-switch", status_code=501)
def kill_switch_mutation(request: Request, _: Role = TRADER_DEP):
    raise HTTPException(status_code=501, detail="kill-switch mutation is disabled until production authentication and authorization are configured")

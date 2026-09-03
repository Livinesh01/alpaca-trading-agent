from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class APIEnvelope(BaseModel, Generic[T]):
    data: T
    request_id: str
    mode: str = "READ_ONLY"


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str


class Health(BaseModel):
    status: str
    paper_trading: bool
    kill_switch: bool
    llm_provider: str | None = None
    market_data: str
    market_data_fresh: bool
    last_success: Any = None
    version: str
    authentication: str


class Account(BaseModel):
    available: bool = False
    portfolio_value: float | None = None
    cash: float | None = None
    buying_power: float | None = None
    equity: float | None = None
    daily_pnl: float | None = None
    currency: str | None = None
    account_status: str | None = None
    trading_mode: str | None = None
    paper_trading: bool = False
    as_of: str | None = None
    reason: str | None = None


class Position(BaseModel):
    symbol: str
    quantity: float | None = None
    average_entry: float | None = None
    current_price: float | None = None
    market_value: float | None = None
    unrealized_pnl: float | None = None
    unrealized_pnl_percent: float | None = None
    exposure: float | None = None
    portfolio_percent: float | None = None


class Order(BaseModel):
    order_id: str
    symbol: str
    side: str
    quantity: float
    order_type: str
    status: str
    submitted_at: str | None = None
    filled_at: str | None = None
    decision_id: str | None = None
    run_id: str | None = None


class Decision(BaseModel):
    model_config = ConfigDict(extra="ignore")
    decision_id: str
    run_id: str
    timestamp: str
    symbol: str
    action: str
    confidence: float
    thesis: str
    entry_reason: str
    position_size: int = 0
    model: str | None = None
    provider: str | None = None
    prompt_version: str | None = None
    market_data_timestamp: str | None = None
    decision_price: float | None = None


class Replay(BaseModel):
    decision: Decision
    execution: dict[str, Any] | None = None
    outcome: dict[str, Any] | None = None
    stages: list[dict[str, Any]]


class KillSwitch(BaseModel):
    enabled: bool
    mutable: bool = False
    reason: str = "Mutation unavailable: production authentication and authorization are not configured."


class OrderPreviewRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=10, pattern=r"^[A-Za-z][A-Za-z0-9.]*$")
    side: str = Field(pattern=r"^(BUY|SELL)$")


class OrderPreview(BaseModel):
    status: str
    symbol: str
    side: str
    quantity: int | None = None
    paper_trading: bool
    risk: str
    final_gate: str
    execution: str
    reason: str


class System(BaseModel):
    backend: str
    provider: str | None = None
    paper_trading: bool
    dry_run: bool
    kill_switch: bool
    health: dict[str, Any]


class Collection(BaseModel, Generic[T]):
    items: list[T]
    available: bool = True
    informational: bool = True


class MarketBar(BaseModel):
    timestamp: str | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None


class MarketData(BaseModel):
    symbol: str
    available: bool
    is_fresh: bool
    age_seconds: float | None = None
    data_timestamp: str | None = None
    current_price: float | None = None
    bars: list[MarketBar] = Field(default_factory=list)
    signals: dict[str, Any] = Field(default_factory=dict)
    latest_trade: dict[str, Any] | None = None
    source: str
    timeframe: str | None = None
    as_of: str | None = None
    reason: str | None = None


class RiskState(BaseModel):
    available: bool
    daily_pnl: float | None = None
    gross_exposure: float | None = None
    position_concentration: float | None = None
    risk_utilization: float | None = None
    max_position_notional_usd: float | None = None
    max_order_notional_usd: float | None = None
    stale_data_blocks: int = 0
    risk_rejections: int = 0
    final_gate_rejections: int = 0
    kill_switch: bool = False
    trading_mode: str | None = None
    paper_trading: bool = False
    authoritative_source: str
    computed_at: str | None = None
    reason: str | None = None


class ActivityEvent(BaseModel):
    timestamp: str | None = None
    age_seconds: float | None = None
    event_type: str | None = None
    severity: str = "info"
    run_id: str | None = None
    decision_id: str | None = None
    execution_id: str | None = None
    outcome_id: str | None = None
    symbol: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)


class BacktestInfo(BaseModel):
    backtest_id: str
    symbol: str
    generated_at: str | None = None
    label: str = "HYPOTHETICAL BACKTEST"
    read_only: bool = True
    actual_production_execution: bool = False
    config: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    equity_curve: list[dict[str, Any]] = Field(default_factory=list)
    trade_count: int | None = None


class EvaluationInfo(BaseModel):
    evaluation_id: str
    dataset: dict[str, Any] = Field(default_factory=dict)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    recommendation: str | None = None
    generated_at: str | None = None
    label: str = "HYPOTHETICAL EVALUATION RESULT"
    human_review_required: bool = True
    auto_deployed: bool = False


class Pagination(BaseModel):
    page: int
    page_size: int
    total: int


class BacktestRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=10, pattern=r"^[A-Za-z][A-Za-z0-9.]*$")
    starting_capital: float = Field(default=10000.0, gt=0)
    transaction_cost: float = Field(default=0.001, ge=0, le=0.1)
    slippage: float = Field(default=0.0, ge=0, le=0.1)

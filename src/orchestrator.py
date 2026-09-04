"""Headless entry point: one agent session via the Cline CLI — see https://docs.cline.bot/cli.

Cline *is* the agent loop. It loads `.cline-config/` (wiring risk_guard_proxy.py as
the `alpaca` MCP server) and emits NDJSON we parse into the journal.

Since the DecisionLoop wiring, run_once() defaults to the PAPER-trading-only
`agent.decision_loop` backend: deterministic signals + account context feed an
LLMProvider (Featherless or NVIDIA via env config), and market data plus EVERY
order flow exclusively through the risk-guard MCP proxy — never Alpaca directly.
PAPER_TRADING=true is required; anything else fails closed. AGENT_BACKEND=cline
keeps this Cline path available as the development/fallback backend.
Our job: check market open, pick the backend, run it, log the outcome.
"""

import argparse
import asyncio
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import zoneinfo
from contextlib import suppress
from typing import Any, ClassVar

import yaml

sys.path.insert(0, os.path.dirname(__file__))
import journal
import lease_guard
import observability as observability_module
from agent.llm import FeatherlessLLMProvider, LLMProvider, NVIDIAProvider
from errors import FinalGateRejectionError
from idempotency import IdempotencyStore, make_idempotency_key
from memory import MemoryStore, create_run_id
from observability import Observability
from risk_rules import AccountState, OrderRequest, check_order

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_PATH = os.path.join(REPO_ROOT, "config", "strategy.yaml")
PROMPT_PATH = os.path.join(REPO_ROOT, "config", "claude_system_prompt.md")
CLINE_CONFIG_DIR = os.path.join(REPO_ROOT, ".cline-config")

DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_MAX_RETRIES = 6


def find_cline_binary() -> str:
    """Locate the `cline` CLI on PATH, handling the Windows .cmd shim."""
    for name in ("cline", "cline.cmd", "cline.exe"):
        path = shutil.which(name)
        if path:
            return path
    raise FileNotFoundError(
        "cline CLI not found on PATH. Install it with `npm i -g cline`, then "
        "authenticate once via `cline auth` before running the orchestrator."
    )


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def market_likely_open(cfg: dict) -> bool:
    """Cheap weekday/RTH guard so we don't spin up cline for nothing; Alpaca's clock tool decides authoritatively."""
    tz = zoneinfo.ZoneInfo(cfg.get("schedule", {}).get("timezone", "America/New_York"))
    now = datetime.datetime.now(tz)
    if now.weekday() >= 5:
        return False
    open_t = datetime.time(9, 30)
    close_t = datetime.time(16, 0)
    return open_t <= now.time() <= close_t


def extract_reasoning(output: str) -> str:
    """Pull the `text`/`reasoning` fields from cline's --json output; fall back to raw output if nothing parses."""
    parts = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = obj.get("text") or obj.get("reasoning")
        if text:
            parts.append(str(text))
    return "\n".join(parts) if parts else output.strip()


def extract_error_message(output: str) -> str:
    """Surface cline's final `{"type":"error","message":...}` line instead of a full NDJSON dump."""
    messages = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = obj.get("message")
        if isinstance(msg, str) and msg.strip():
            messages.append(msg.strip())
    if messages:
        return messages[-1]
    return output.strip()


VALID_ACTIONS = frozenset({"BUY", "SELL", "HOLD"})

# Allowed fields for one structured decision. Extra fields are rejected.
CANONICAL_DECISION_FIELDS = frozenset(
    {"symbol", "action", "confidence", "position_size", "thesis", "entry_reason"}
)
# Reject excessively long reasoning text.
MAX_DECISION_REASONING_WORDS = 120


def _find_json_block(text: str) -> object | None:
    """Return the first parseable JSON object/array embedded in free text."""
    cleaned = re.sub(r"```(?:json)?", "", text)
    candidate = cleaned.strip()
    if candidate:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = cleaned.find(open_ch)
        end = cleaned.rfind(close_ch)
        if start == -1 or end <= start:
            continue
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            continue
    return None


def validate_trade_decision(d: dict) -> dict:
    """Validate one structured decision against the required schema; raise ValueError on violation."""
    if not isinstance(d, dict):
        raise TypeError(f"decision must be a JSON object, got {type(d).__name__}")

    unexpected = sorted(set(d) - CANONICAL_DECISION_FIELDS)
    if unexpected:
        raise ValueError(f"unexpected decision field(s): {', '.join(unexpected)}")

    symbol = d.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError(f"symbol must be a non-empty string, got {symbol!r}")

    action = d.get("action")
    if action not in VALID_ACTIONS:
        raise ValueError(f"action must be one of {sorted(VALID_ACTIONS)}, got {action!r}")

    confidence = d.get("confidence")
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        raise ValueError(f"confidence must be a number, got {confidence!r}") from None
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence must be within [0.0, 1.0], got {confidence!r}")

    position_size = d.get("position_size")
    if isinstance(position_size, bool) or not isinstance(position_size, int):
        raise TypeError(f"position_size must be an integer >= 0, got {position_size!r}")
    if position_size < 0:
        raise ValueError(f"position_size must be >= 0, got {position_size!r}")
    if action == "HOLD" and position_size != 0:
        raise ValueError("HOLD must use position_size 0")

    thesis = d.get("thesis")
    entry_reason = d.get("entry_reason")
    if not isinstance(thesis, str):
        raise TypeError(f"thesis must be a string, got {thesis!r}")
    if not isinstance(entry_reason, str):
        raise TypeError(f"entry_reason must be a string, got {entry_reason!r}")

    for field_name, field_text in (("thesis", thesis), ("entry_reason", entry_reason)):
        if len(str(field_text).split()) > MAX_DECISION_REASONING_WORDS:
            raise ValueError(
                f"{field_name} exceeds {MAX_DECISION_REASONING_WORDS} words "
                "(unreasonably long reasoning is rejected)"
            )

    return {
        "symbol": symbol.strip(),
        "action": action,
        "confidence": confidence,
        "thesis": thesis,
        "position_size": position_size,
        "entry_reason": entry_reason,
    }


def extract_trade_decisions(output: str, expected_symbols: set[str] | None = None) -> list[dict]:
    """Parse and validate the agent's structured decisions JSON block.

    The agent must close its response with one decision per watchlist symbol.
    Raises ValueError if the block is missing, malformed, or fails schema checks.
    """
    text_parts = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = obj.get("text") or obj.get("reasoning")
        if text:
            text_parts.append(str(text))
    chunks = text_parts or [output]

    payload = None
    for chunk in reversed(chunks):  # final message is where decisions belong
        payload = _find_json_block(chunk)
        if payload is not None:
            break
    if payload is None:
        raise ValueError("no JSON decisions block found in agent output")
    if isinstance(payload, dict):
        payload = payload.get("decisions")
    if not isinstance(payload, list):
        raise TypeError("decisions must be a JSON array under \"decisions\"")
    if not payload:
        raise ValueError("decisions array is empty")

    decisions = [validate_trade_decision(d) for d in payload]

    symbols = [d["symbol"] for d in decisions]
    if len(symbols) != len(set(symbols)):
        raise ValueError("duplicate decision for a watchlist symbol")

    if expected_symbols is not None:
        got = set(symbols)
        missing = expected_symbols - got
        if missing:
            raise ValueError(f"missing decisions for: {sorted(missing)}")
        unexpected = got - expected_symbols
        if unexpected:
            raise ValueError(f"decisions for unknown symbols: {sorted(unexpected)}")

    return decisions


def _single_line(text: str) -> str:
    """Collapse whitespace to single spaces.

    Windows `.cmd` shim re-tokenizes the command line, so a multi-line `--system`
    value would swallow the trailing prompt argument.
    """
    return " ".join(text.split())


def invoke_cline(system_prompt: str, task: str) -> str:
    """Run one non-interactive cline session and return its collected output."""
    binary = find_cline_binary()

    timeout = int(os.environ.get("CLINE_RUN_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    retries = int(os.environ.get("CLINE_MAX_RETRIES", DEFAULT_MAX_RETRIES))

    cmd = [
        binary,
        "--cwd",
        REPO_ROOT,
        "--config",
        CLINE_CONFIG_DIR,  # project-local MCP config, not global ~/.cline
        "--system",
        _single_line(system_prompt),
        "--timeout",
        str(timeout),
        "--retries",
        str(retries),
        "--json",
        task,
    ]

    proc = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        env=os.environ,  # carries Alpaca keys through to the subprocess
        capture_output=True,
        text=True,
        encoding="utf-8",  # cline emits UTF-8 NDJSON; Windows cp1252 chokes on it
        errors="replace",
        stdin=subprocess.DEVNULL,  # force headless mode (no TTY)
        timeout=timeout + 60,
        check=False,
    )

    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise RuntimeError(
            f"cline exited with code {proc.returncode}: {extract_error_message(output)}"
        )
    return output


# === PAPER-ONLY DECISIONLOOP BACKEND =======================================
# ---------------------------------------------------------------------------
# Paper-only DecisionLoop backend (default for run_once()).
#
# Safety boundaries preserved:
# * PAPER_TRADING=true is REQUIRED — anything else fails closed.
# * risk_guard_proxy stays the ONLY order executor: market data and every
#   order flow through its MCP tools; Alpaca is never called from here.
# * The proxy re-validates every order with risk_rules.check_order.
# * Risk limits, signal params, and sizing come from config/strategy.yaml +
#   risk_rules — the LLM never sets a quantity or a risk value.
# * A final executor gate (_FinalOrderGate) re-verifies paper mode, the
#   kill switch, order sanity, and risk_rules one last time immediately
#   before an order is handed to the proxy.
# ---------------------------------------------------------------------------

PROXY_SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "risk_guard_proxy.py")
PAPER_TRADING_ENV = "PAPER_TRADING"
AGENT_BACKEND_ENV = "AGENT_BACKEND"
LLM_PROVIDER_ENV = "LLM_PROVIDER"
_KILL_SWITCH_ENV = "TRADING_KILL_SWITCH"
_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_SECRET_ENV_VARS = ("FEATHERLESS_API_KEY", "NVIDIA_API_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY")


def paper_trading_enabled() -> bool:
    """True only when PAPER_TRADING is explicitly set to a truthy value."""
    return os.environ.get(PAPER_TRADING_ENV, "").strip().lower() in _TRUTHY_ENV_VALUES


def _redact(value: object) -> str:
    """Scrub known API-key values from anything we journal, print, or log."""
    cleaned = str(value or "")
    for name in _SECRET_ENV_VARS:
        secret = os.environ.get(name, "").strip()
        if len(secret) >= 4:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return cleaned


def build_llm_provider() -> LLMProvider:
    """Build the LLMProvider named by LLM_PROVIDER ('featherless' | 'nvidia').

    API keys and models are resolved by the providers themselves from the
    environment / .env (FEATHERLESS_API_KEY, FEATHERLESS_MODEL, NVIDIA_API_KEY,
    NVIDIA_MODEL) — never hard-coded, never logged, and never sent anywhere
    except the provider's own client.
    """
    name = os.environ.get(LLM_PROVIDER_ENV, "featherless").strip().lower()
    if name == "featherless":
        return FeatherlessLLMProvider()
    if name == "nvidia":
        return NVIDIAProvider()
    raise ValueError(
        f"Unsupported {LLM_PROVIDER_ENV} {name!r}; expected 'featherless' or 'nvidia'."
    )


class _RiskGuardProxySession:
    """Persistent MCP client session to risk_guard_proxy.py on a private loop thread.

    The proxy is spawned as a subprocess (the same pattern it uses for
    alpaca-mcp-server) and stays connected for the whole run so its per-run
    order cap and metrics keep working. `call()` bridges the synchronous
    DecisionLoop callbacks onto that loop; tool errors raise RuntimeError so
    every caller fails closed.
    """

    def __init__(
        self,
        server_params: Any = None,
        *,
        startup_timeout: float = 90.0,
        call_timeout: float = 180.0,
    ) -> None:
        self._server_params = server_params
        self._startup_timeout = startup_timeout
        self._call_timeout = call_timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: Any = None
        self._stack: Any = None
        self._ready = threading.Event()
        self._errors: list[BaseException] = []

    def start(self) -> None:
        """Spawn the proxy subprocess and initialize the MCP session."""
        from mcp import StdioServerParameters

        params = self._server_params
        if params is None:
            # Mirror risk_guard_proxy._upstream_server_params: inherit the full
            # environment so the child spawns reliably on Windows; the proxy
            # loads .env (Alpaca paper keys) itself and forces paper mode
            # (ALPACA_PAPER_TRADE=true) toward the upstream server.
            params = StdioServerParameters(
                command=sys.executable,
                args=[PROXY_SCRIPT_PATH],
                env=dict(os.environ),
            )
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            args=(params,),
            daemon=True,
            name="risk-guard-proxy-client",
        )
        self._thread.start()
        if not self._ready.wait(self._startup_timeout):
            raise TimeoutError("risk_guard_proxy MCP session did not initialize in time.")
        if self._errors:
            raise RuntimeError(f"risk_guard_proxy MCP session failed to start: {self._errors[0]}")

    def _run_loop(self, params: Any) -> None:
        from contextlib import AsyncExitStack

        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        asyncio.set_event_loop(self._loop)

        async def _open() -> None:
            self._stack = AsyncExitStack()
            read, write = await self._stack.enter_async_context(stdio_client(params))
            self._session = await self._stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()

        try:
            self._loop.run_until_complete(_open())
        except BaseException as exc:  # noqa: BLE001 — surfaced through start()
            self._errors.append(exc)
        finally:
            self._ready.set()
        if not self._errors:
            try:
                self._loop.run_forever()  # serves call_tool coroutines until close()
            except BaseException as exc:  # noqa: BLE001
                self._errors.append(exc)

    def call(self, tool: str, arguments: dict[str, Any], timeout: float | None = None) -> str:
        """Invoke one proxy tool synchronously; returns the first text block."""
        if self._session is None or self._loop is None or self._errors:
            raise RuntimeError("risk_guard_proxy MCP session is not running.")
        if self._thread is None or not self._thread.is_alive():
            raise RuntimeError("risk_guard_proxy MCP session thread is not alive.")

        async def _call() -> str:
            result = await self._session.call_tool(tool, arguments)
            text = next(
                (
                    str(getattr(block, "text", ""))
                    for block in getattr(result, "content", [])
                    if getattr(block, "type", None) == "text"
                ),
                "",
            )
            if getattr(result, "isError", False):
                raise RuntimeError(f"MCP tool {tool!r} failed: {text or 'unknown error'}")
            return text

        future = asyncio.run_coroutine_threadsafe(_call(), self._loop)
        return future.result(self._call_timeout if timeout is None else timeout)

    def close(self) -> None:
        """Best-effort teardown; never raises."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        async def _close() -> None:
            if self._stack is not None:
                await self._stack.aclose()

        thread = self._thread
        if thread is not None and thread.is_alive():
            with suppress(Exception):
                asyncio.run_coroutine_threadsafe(_close(), loop).result(30.0)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=10.0)
        else:
            with suppress(Exception):
                loop.run_until_complete(_close())
        with suppress(Exception):
            loop.close()
        self._session = None
        self._stack = None


class _ProxyOrderExecutor:
    """The ONLY order executor in the paper path.

    Forwards US-equity orders through the risk-guard MCP proxy's
    `place_stock_order` tool — the proxy rebuilds its own OrderRequest and
    re-runs risk_rules.check_order (per-run caps, position caps, daily loss,
    market-data freshness) before anything reaches Alpaca. Options, crypto,
    and live trading stay disabled at the proxy/upstream boundary.
    """

    ORDER_TOOL = "place_stock_order"

    def __init__(self, session: _RiskGuardProxySession) -> None:
        self._session = session

    def submit(self, order: OrderRequest) -> str:
        # Only symbol/side/qty are forwarded; the proxy owns every other check.
        # qty goes on the wire as a STRING: the upstream alpaca-mcp-server
        # declares `qty: Optional[str]` and the MCP SDK validates call_tool
        # arguments against that schema — an int is rejected with
        # "1 is not valid under any of the given schemas" before the risk
        # guard even runs. risk_rules._coerce_float normalizes it for math.
        return self._session.call(
            self.ORDER_TOOL,
            {"symbol": order.symbol, "side": order.side, "qty": str(int(order.qty))},
        )


class _DryRunExecutor:
    """Dry-run executor: a HARD guarantee that zero orders can be submitted.

    Deliberately holds NO reference to the proxy session, so it is structurally
    incapable of reaching `place_stock_order`, the risk-guard proxy, or Alpaca.
    Risk controls are NOT bypassed: the DecisionLoop has already run
    risk_rules.check_order (and journaled the verdict) before calling the
    executor — this only removes the final submission step.
    """

    def submit(self, order: OrderRequest) -> str:
        return (
            f"DRY-RUN: would submit {order.symbol} {order.side} {int(order.qty)} "
            "(not sent to the risk guard or Alpaca)"
        )


class _FinalOrderGate:
    """The last safety boundary immediately before an order leaves the process.

    Wraps the wire executor and verifies the minimum critical invariants on
    EVERY submission:

    * paper-only: PAPER_TRADING must still be enabled at submission time;
    * kill-switch: TRADING_KILL_SWITCH=true refuses every order (emergency stop);
    * symbol validity: non-empty, ticker-shaped, and inside the configured
      watchlist (when one is provided);
    * side validity: exactly "buy" or "sell" — "sell_short" can never pass;
    * quantity validity: a positive whole number of shares (notional-only
      orders are not accepted on this path);
    * required order fields: US-equity stock order with a known order type;
    * risk-approved: re-runs the SAME `risk_rules.check_order` used by the
      DecisionLoop and the proxy, with this run's cached account state — a
      third independent evaluation, not a duplicate risk engine.

    Any refusal raises before the wrapped executor is touched, so the
    DecisionLoop records it as an executor error and the run fails closed.
    The proxy remains the authoritative final check with fresh account/price
    data; this gate can only make the path stricter, never looser.
    """

    _SYMBOL_RE: ClassVar[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
    _VALID_SIDES: ClassVar[frozenset[str]] = frozenset({"buy", "sell"})
    _VALID_ORDER_TYPES: ClassVar[frozenset[str]] = frozenset({"market", "limit"})

    def __init__(
        self,
        executor: Any,
        *,
        fetch_account: Any,
        limits: dict[str, Any],
        allowed_symbols: list[str] | tuple[str, ...] = (),
        prices: dict[str, float] | None = None,
        idempotency_store: IdempotencyStore | None = None,
        observability: Observability | None = None,
        run_id: str | None = None,
    ) -> None:
        self._executor = executor
        self._fetch_account = fetch_account
        self._limits = limits
        self._allowed_symbols = {str(s).upper() for s in allowed_symbols}
        self._prices = prices if prices is not None else {}
        self._idempotency_store = idempotency_store
        self._observability = observability
        self._run_id = run_id

    def _refuse(self, reason: str, *, reason_code: str = "FINAL_GATE_REJECTED") -> None:
        message = f"final order gate refused: {reason}"
        if self._observability is not None:
            self._observability.emit(
                "final_gate_rejection",
                run_id=self._run_id,
                reason_code=reason_code,
                reason=message,
            )
        raise FinalGateRejectionError(message, reason_code=reason_code)

    def submit(self, order: OrderRequest) -> str:
        if not paper_trading_enabled():
            self._refuse("paper trading is not enabled")
        if os.environ.get(_KILL_SWITCH_ENV, "").strip().lower() in _TRUTHY_ENV_VALUES:
            self._refuse(f"{_KILL_SWITCH_ENV} is set (emergency stop)")
        if lease_guard.is_lost():
            # C2: the worker can no longer prove ownership of its PostgreSQL
            # lease — no order may leave the process (fail closed).
            self._refuse(
                "worker lease is lost; trading is halted until the lease is recovered",
                reason_code="LEASE_LOST",
            )

        symbol = str(getattr(order, "symbol", "") or "")
        if not symbol or not self._SYMBOL_RE.fullmatch(symbol):
            self._refuse(f"invalid symbol {symbol!r}")
        if self._allowed_symbols and symbol.upper() not in self._allowed_symbols:
            self._refuse(f"symbol {symbol!r} is not in the configured watchlist")

        side = str(getattr(order, "side", "") or "")
        if side not in self._VALID_SIDES:
            self._refuse(f"invalid side {side!r} (short selling is never accepted)")

        qty = getattr(order, "qty", None)
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
            self._refuse(f"invalid quantity {qty!r}")

        asset_class = str(getattr(order, "asset_class", "") or "")
        if asset_class != "us_equity":
            self._refuse(f"only US-equity orders pass this executor, got {asset_class!r}")
        order_type = str(getattr(order, "order_type", "") or "")
        if order_type not in self._VALID_ORDER_TYPES:
            self._refuse(f"invalid order type {order_type!r}")

        account = self._fetch_account(order.symbol)
        risk = check_order(order, account, self._limits, last_price=self._prices.get(order.symbol))
        if not risk.allowed:
            self._refuse(f"risk re-check rejected the order ({risk.reason})", reason_code=f"RISK_{risk.reason_code}")

        if self._idempotency_store is not None:
            idempotency_key = order.idempotency_key or make_idempotency_key(
                symbol=order.symbol,
                side=order.side,
                qty=order.qty,
                run_id=self._run_id or "",
                decision_id="",
            )
            record = self._idempotency_store.check_and_claim(
                idempotency_key=idempotency_key,
                run_id=self._run_id or "",
                decision_id="",
                symbol=order.symbol,
                side=order.side,
                qty=order.qty if isinstance(order.qty, int) else int(order.qty),
            )
            if record.status != "completed":
                self._refuse(
                    f"duplicate execution rejected (idempotency_key={idempotency_key})",
                    reason_code="DUPLICATE_EXECUTION",
                )

        if self._observability is not None:
            self._observability.emit(
                "execution_attempt",
                run_id=self._run_id,
                symbol=order.symbol,
                side=order.side,
                qty=order.qty,
            )

        result = self._executor.submit(order)

        if self._observability is not None:
            self._observability.emit(
                "execution_success",
                run_id=self._run_id,
                symbol=order.symbol,
                side=order.side,
                qty=order.qty,
            )
        return result


def _build_persistence() -> Any:
    """Wire the fail-closed PostgreSQL persistence adapter (C1).

    Returns None when no database is configured (development/tests): the
    repository layer is never invoked without DATABASE_URL, so existing local
    behavior is unchanged. When a database IS configured, the adapter is
    fail-closed: any persistence failure aborts the run with zero orders.
    """
    import repositories as repo

    if not repo.is_db_configured():
        return None
    from persistence import SentinelPersistence

    return SentinelPersistence()


def _build_decision_loop(
    cfg: dict,
    *,
    provider: LLMProvider,
    session: _RiskGuardProxySession,
    limits: dict[str, Any] | None = None,
    dry_run: bool = False,
    run_id: str | None = None,
    observability: Observability | None = None,
    persistence: Any = None,
) -> Any:
    """Wire a DecisionLoop to the risk-guard proxy (paper trading only).

    With dry_run=True the executor is swapped for _DryRunExecutor so the
    workflow (market data, LLM, validation, sizing, risk checks) runs exactly
    as in production but no order can ever reach the proxy or Alpaca.
    """
    # Lazy imports: agent.decision_loop imports orchestrator (this module), and
    # the Cline fallback path must not require MCP.
    import risk_guard_proxy as rgp
    from agent.decision_loop import DecisionLoop, default_limits

    if limits is None:
        limits = default_limits()
    signal_params = dict(limits.get("signal_params", {}) or {})
    max_age_seconds = rgp._market_data_max_age_seconds(limits)

    def fetch_bars(symbol: str) -> list[dict[str, Any]]:
        """Deterministic OHLC bars via the proxy (mirrors its signals-tool call)."""
        payload = {
            "symbols": str(symbol).upper(),
            "timeframe": signal_params.get("timeframe", "1Day"),
            "days": int(signal_params.get("lookback_days", 180)),
            "limit": int(signal_params.get("bars_limit", 500)),
            "sort": "asc",
        }
        raw = session.call(rgp.STOCK_BARS_TOOL, payload)
        parsed = rgp._unwrap_payload(rgp._to_json_or_none(raw))
        bars = rgp._extract_stock_bars(parsed, symbol)
        if not bars:
            raise ValueError(f"No bar data returned for {str(symbol).upper()}.")
        return bars

    prices_seen: dict[str, float] = {}

    def fetch_price(symbol: str) -> float:
        """Fresh last-trade price via the proxy (staleness enforced by its limits)."""
        raw = session.call(rgp.LATEST_TRADE_TOOL, {"symbols": str(symbol).upper()})
        parsed = rgp._unwrap_payload(rgp._to_json_or_none(raw))
        price = float(rgp._extract_fresh_last_price(str(symbol).upper(), parsed, max_age_seconds))
        prices_seen[str(symbol).upper()] = price
        return price

    account_cache: dict[str, AccountState] = {}

    def fetch_account(symbol: str) -> AccountState:
        """Account/position context via the proxy; mirrors its _build_account_state."""
        key = str(symbol).upper()
        if key not in account_cache:
            account_json = rgp._unwrap_payload(
                rgp._to_json_or_none(session.call(rgp.ACCOUNT_INFO_TOOL, {}))
            )
            if not isinstance(account_json, dict):
                account_json = {}
            positions_json = rgp._unwrap_payload(
                rgp._to_json_or_none(session.call(rgp.POSITIONS_TOOL, {}))
            )
            if not isinstance(positions_json, list):
                positions_json = []
            existing_notional = 0.0
            for position in positions_json:
                if str(position.get("symbol", "")).upper() == key:
                    existing_notional = float(position.get("market_value", 0) or 0)
            equity = float(account_json.get("equity", 0) or 0)
            last_equity = float(account_json.get("last_equity", equity) or 0)
            account_cache[key] = AccountState(
                cash=float(account_json.get("cash", 0) or 0),
                buying_power=float(account_json.get("buying_power", 0) or 0),
                equity=equity,
                daily_pnl=equity - last_equity,
                open_position_count=len(positions_json),
                orders_placed_this_run=0,
                existing_position_notional=existing_notional,
            )
        return account_cache[key]

    watchlist = [str(s).upper() for s in cfg.get("watchlist", [])] or None
    idempotency_store = IdempotencyStore()
    from config import is_production_env

    memory_store = None if is_production_env() else MemoryStore()
    if dry_run:
        # Dry-run never touches the order path, so it also never touches the
        # durable order-intent/idempotency records (dev rehearsal tool only).
        persistence = None
        executor: Any = _DryRunExecutor()
    else:
        executor = _FinalOrderGate(
            _ProxyOrderExecutor(session),
            fetch_account=fetch_account,
            limits=limits,
            allowed_symbols=watchlist or [],
            prices=prices_seen,
            idempotency_store=idempotency_store,
            observability=observability or Observability(),
            run_id=run_id,
        )
    return DecisionLoop(
        provider=provider,
        fetch_bars=fetch_bars,
        fetch_price=fetch_price,
        fetch_account=fetch_account,
        executor=executor,
        limits=limits,
        watchlist=watchlist,
        memory_store=memory_store,
        observability=observability or Observability(),
        run_id=run_id,
        persistence=persistence,
    )


def _run_once_decision_loop(cfg: dict, dry_run: bool = False) -> int:
    """One DecisionLoop cycle in PAPER mode; every failure path fails closed."""
    if not paper_trading_enabled():
        msg = (
            "PAPER_TRADING is not enabled — refusing to run the DecisionLoop "
            "(this backend is paper-trading only; set PAPER_TRADING=true explicitly)."
        )
        print(msg, file=sys.stderr)
        journal.log_run_start("DecisionLoop run aborted: paper trading not enabled")
        journal.log_run_end(f"Run failed: {msg}")
        return 1

    watchlist = ", ".join(str(s).upper() for s in cfg.get("watchlist", []))
    journal.log_run_start(f"DecisionLoop paper run, watchlist={watchlist}")

    session = _RiskGuardProxySession()
    run_id = create_run_id()
    # C1: PostgreSQL is authoritative whenever a database is configured — wire
    # the fail-closed persistence adapter and mirror events into agent_events
    # so the production API can read real state without a shared filesystem.
    persistence = _build_persistence()
    if persistence is None and not dry_run:
        from config import is_production_env

        if is_production_env():
            # C1/C3: production never trades without PostgreSQL-backed persistence.
            msg = "production requires PostgreSQL persistence; refusing to run (fail closed)"
            print(f"Run failed (fail closed): {msg}", file=sys.stderr)
            journal.log_run_end(f"Run failed: {msg}")
            return 1
    if persistence is not None:
        from persistence import PgMirroredObservability

        observer = PgMirroredObservability()
        try:
            persistence.preflight()
        except Exception as exc:  # noqa: BLE001 — fail closed before ANY work
            observer.emit("run_failed", run_id=run_id, error=type(exc).__name__)
            print(f"Run failed (fail closed): {exc}", file=sys.stderr)
            journal.log_run_end(f"Run failed: {exc}")
            return 1
    else:
        observer = Observability()
    observer.emit("run_started", run_id=run_id, dry_run=dry_run)
    try:
        provider = build_llm_provider()  # fails closed before any subprocess spawns
        session.start()
        loop = _build_decision_loop(
            cfg,
            provider=provider,
            session=session,
            dry_run=dry_run,
            run_id=run_id,
            observability=observer,
            persistence=persistence,
        )
        result = loop.run()
    except Exception as exc:  # noqa: BLE001 — fail closed: nothing reached the executor
        msg = _redact(exc)
        observer.emit("run_failed", run_id=run_id)
        print(f"Run failed: {msg}", file=sys.stderr)
        journal.log_run_end(f"Run failed: {msg}")
        return 1
    finally:
        session.close()

    journal.log_reasoning(_redact(result.response or result.error or ""))
    for outcome in result.outcomes:
        journal.log_order_decision(
            outcome.symbol,
            outcome.action,
            outcome.requested_qty,
            outcome.risk.allowed,
            outcome.risk.reason,
        )

    executor_errors = [o for o in result.outcomes if o.executor_error]
    if result.success and not executor_errors:
        observer.emit("run_completed", run_id=run_id, dry_run=dry_run)
        if dry_run:
            for outcome in result.outcomes:
                if outcome.order is not None:
                    print(
                        f"DRY-RUN: would submit {outcome.order.symbol} {outcome.order.side} "
                        f"{outcome.order.qty} — not sent to the risk guard or Alpaca"
                    )
            journal.log_run_end(
                f"dry run completed ({result.orders_submitted} order(s) WOULD be submitted — "
                f"none sent, {len(result.outcomes)} validated decision(s))"
            )
        else:
            journal.log_run_end(
                f"completed ({result.orders_submitted} order(s) submitted via risk guard, "
                f"{len(result.outcomes)} validated decision(s))"
            )
        print(_redact(result.response or ""))
        return 0

    if not result.success:
        msg = _redact(result.error or "unknown DecisionLoop failure")
    else:
        msg = _redact("; ".join(f"{o.symbol}: {o.executor_error}" for o in executor_errors))
    print(f"Run failed (fail closed): {msg}", file=sys.stderr)
    observer.emit("run_failed", run_id=run_id)
    journal.log_run_end(f"Run failed: {msg}")
    return 1


def run_once(force: bool = False, dry_run: bool = False) -> int:
    cfg = load_config()
    # Heuristic only: --force may skip this weekday/RTH check. It NEVER bypasses
    # the authoritative Alpaca market-clock gate inside the risk-guard proxy.
    if cfg.get("schedule", {}).get("skip_if_market_closed", True) and not force and not market_likely_open(cfg):
        print("Market likely closed (US equities, weekday 9:30-16:00 ET) — skipping run.")
        return 0

    backend = os.environ.get(AGENT_BACKEND_ENV, "decision_loop").strip().lower()
    if backend == "decision_loop":
        return _run_once_decision_loop(cfg, dry_run=dry_run)
    if backend == "cline":
        if dry_run:
            # Fail closed: Cline drives its own MCP session, so a hard
            # zero-order guarantee is impossible on this backend.
            msg = (
                "--dry-run is not supported with AGENT_BACKEND=cline (Cline drives its own "
                "MCP session, so zero-order submission cannot be guaranteed) — refusing to run."
            )
            print(msg, file=sys.stderr)
            journal.log_run_start("Run aborted: --dry-run requested with the Cline backend")
            journal.log_run_end(f"Run failed: {msg}")
            return 1
        return _run_once_cline(cfg)
    print(
        f"Unknown {AGENT_BACKEND_ENV} {backend!r} — refusing to run (fail closed; "
        "expected 'decision_loop' or 'cline').",
        file=sys.stderr,
    )
    return 1


def _run_once_cline(cfg: dict) -> int:
    with open(PROMPT_PATH, encoding="utf-8") as f:
        system_prompt = f.read()

    watchlist = ", ".join(cfg.get("watchlist", []))
    task = (
        f"Run today's trading routine. Your watchlist is: {watchlist}. "
        "Follow your standard process: check account state, review each symbol, "
        "reason explicitly, then act only where justified."
    )

    journal.log_run_start(f"invoking agent via cline CLI, watchlist={watchlist}")

    try:
        output = invoke_cline(system_prompt, task)
    except FileNotFoundError as exc:
        # cline binary missing — log, stderr, exit 1, no traceback
        msg = str(exc)
        print(msg, file=sys.stderr)
        journal.log_run_end(f"Run failed: {msg}")
        return 1
    except Exception as exc:  # noqa: BLE001
        journal.log_run_end(f"Run failed: {exc}")
        print(f"Run failed: {exc}", file=sys.stderr)
        return 1

    reasoning = extract_reasoning(output)
    journal.log_reasoning(reasoning)

    watchlist = {str(s).upper() for s in cfg.get("watchlist", [])}
    try:
        decisions = extract_trade_decisions(output, expected_symbols=watchlist)
    except (ValueError, TypeError) as exc:
        # Malformed/absent decisions must never crash the run or reach Alpaca;
        # the Python risk guard stays the enforcement layer.
        print(f"Structured decisions invalid: {exc}", file=sys.stderr)
        journal.log_run_end(f"completed (structured decisions invalid: {exc})")
        print(reasoning)
        return 0

    journal.log_run_end(f"completed ({len(decisions)} structured decisions validated)")
    print(reasoning)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and run exactly one orchestrator cycle.

    --help and unknown options are handled entirely by argparse and exit
    before any configuration load, market-hours check, LLM call, or Alpaca
    access can happen.
    """
    parser = argparse.ArgumentParser(
        prog="orchestrator.py",
        description="Alpaca paper-trading orchestrator: runs one agent decision cycle.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run exactly one trading cycle (this is the default behavior)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="run one trading cycle even if the market is likely closed",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="run the full workflow but never submit an order (decision_loop backend only)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="print read-only observability health status and exit",
    )
    args = parser.parse_args(argv)
    if args.status:
        status = Observability.read_status(directory=observability_module.DEFAULT_OBSERVABILITY_DIR)
        if not status:
            status = Observability(directory=observability_module.DEFAULT_OBSERVABILITY_DIR).health
        print(json.dumps(status, sort_keys=True))
        return 0
    return run_once(force=args.force, dry_run=args.dry_run)


if __name__ == "__main__":
    # Windows consoles/redirects default to cp1252; agent reasoning is UTF-8
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))


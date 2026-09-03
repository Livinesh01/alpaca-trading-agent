"""Per-run markdown audit trail in journal/YYYY-MM-DD.md.

Each run appends reasoning, order decisions, and outcome so
"the agent reasoned about this before trading" stays a checkable claim.
"""

import os
from datetime import datetime, timezone

JOURNAL_DIR = os.path.join(os.path.dirname(__file__), "..", "journal")


def _ensure_dir():
    os.makedirs(JOURNAL_DIR, exist_ok=True)


def _today_path() -> str:
    _ensure_dir()
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(JOURNAL_DIR, f"{date_str}.md")


def log_run_start(account_summary: str) -> None:
    path = _today_path()
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n## Run at {ts}\n\n**Account:** {account_summary}\n\n")


def log_reasoning(text: str) -> None:
    path = _today_path()
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"**Agent reasoning:**\n\n{text}\n\n")


def log_order_decision(symbol: str, side: str, qty, allowed: bool, reason: str) -> None:
    path = _today_path()
    status = "✅ ALLOWED" if allowed else "🚫 BLOCKED"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"- **{status}** — {side.upper()} {qty} {symbol} — {reason}\n")


def log_order_state(
    client_order_id: str,
    symbol: str,
    side: str,
    qty,
    status: str,
    detail: str,
) -> None:
    path = _today_path()
    with open(path, "a", encoding="utf-8") as f:
        f.write(
            f"  - **ORDER STATE** — {status.upper()} — {side.upper()} {qty} {symbol} "
            f"(client_order_id={client_order_id}) — {detail}\n"
        )


def log_run_end(summary: str) -> None:
    path = _today_path()
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n**Run summary:** {summary}\n\n---\n")

"""Process-wide worker-lease health signal.

The worker's lease-renewal thread owns this guard. When a renewal fails, the
lease is considered LOST for the whole process until a renewal succeeds again:

* no new decision cycle may start,
* the final order gate refuses every submission (fail closed),
* if the lease is not recovered within the worker's bounded grace period, the
  worker stops safely and exits non-zero.

The guard is intentionally module-level: the renewal thread, the cycle loop and
the order path all run inside the worker process, and a lost lease must be
visible to all of them immediately. Outside the worker (CLI runs, API) the guard
is never marked lost, so behavior is unchanged.
"""

from __future__ import annotations

import threading

_lost = threading.Event()


def mark_lost() -> None:
    """Mark the worker lease as lost: trading must pause immediately."""
    _lost.set()


def mark_recovered() -> None:
    """Mark the worker lease as recovered: trading may resume."""
    _lost.clear()


def is_lost() -> bool:
    """True when the worker can no longer prove ownership of its lease."""
    return _lost.is_set()


def reset() -> None:
    """Clear the guard (tests only)."""
    _lost.clear()

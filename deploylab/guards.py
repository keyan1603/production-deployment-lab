"""Guards that act before a provider limit or a budget does: quota shedding with a priority reserve, and a daily spend cap.

Why shed early. A provider's hard limit fails every caller at once, in the middle of whatever they were doing, and the failures
set off retries, circuit breakers and failover to a weaker model. Shedding a little earlier, and in a chosen order, turns that into
a decision: background work stops first, the traffic that matters most keeps the last of the allowance, and the failure is one the
caller already knows how to handle (a ticket is held with a reason; it is not lost to a timeout).

    QuotaGuard   counts requests per model per provider-day, persisted across restarts. Each caller has a shed threshold
                 (the share of the day's allowance at which that caller is refused). Low-priority callers get a low threshold.
    SpendGuard   sums the dollars the gateway has recorded today, with the same persistence, and refuses non-exempt callers
                 once the daily cap is reached.

Both are in-process and per gateway instance. They are only correct with ONE gateway instance, like the rate limiter they sit
beside; scaling the gateway out needs the counters in a shared store (see the README, "capacity").
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path
from typing import Callable, Optional


def day_key(now: dt.datetime, reset_hour_utc: int) -> str:
    """The provider day a moment belongs to, when the provider's day starts at `reset_hour_utc`."""
    return (now - dt.timedelta(hours=reset_hour_utc)).date().isoformat()


class _Persisted:
    def __init__(self, path: Optional[Path], reset_hour_utc: int, clock: Callable[[], dt.datetime]) -> None:
        self.path, self.reset_hour_utc, self.clock = Path(path) if path else None, reset_hour_utc, clock
        self.lock = threading.Lock()
        self.day, self.data = day_key(self.clock(), reset_hour_utc), {}
        if self.path and self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                if saved.get("day") == self.day:
                    self.data = saved.get("data", {})
            except (OSError, ValueError):
                pass                                                   # an unreadable file starts the day at zero: never block on bookkeeping

    def _roll(self) -> None:
        today = day_key(self.clock(), self.reset_hour_utc)
        if today != self.day:
            self.day, self.data = today, {}

    def _save(self) -> None:
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps({"day": self.day, "data": self.data}), encoding="utf-8")
            except OSError:
                pass


class QuotaGuard(_Persisted):
    def __init__(self, limits: dict, shed_at: Optional[dict] = None, path: Optional[Path] = None, reset_hour_utc: int = 0,
                 clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)) -> None:
        super().__init__(path, reset_hour_utc, clock)
        self.limits, self.shed_at = dict(limits), dict(shed_at or {})

    def record(self, model: str, n: int = 1) -> None:
        with self.lock:
            self._roll()
            self.data[model] = self.data.get(model, 0) + n
            self._save()

    def used(self, model: str) -> int:
        with self.lock:
            self._roll()
            return self.data.get(model, 0)

    def ratio(self, model: str) -> float:
        limit = self.limits.get(model)
        return self.used(model) / limit if limit else 0.0

    def threshold(self, caller: str) -> float:
        return self.shed_at.get(caller, 1.0)

    def check(self, caller: str, models: list) -> Optional[str]:
        """None to proceed, or the reason this caller is refused: every model it could be served by is at or past its threshold."""
        limited = [m for m in models if m in self.limits]
        if not limited:
            return None
        limit_at = self.threshold(caller)
        if all(self.ratio(m) >= limit_at for m in limited):
            worst = max(limited, key=self.ratio)
            return f"{caller} is shed at {limit_at:.0%} of the daily allowance; {worst} is at {self.ratio(worst):.0%}"
        return None

    def snapshot(self) -> dict:
        return {m: {"used": self.used(m), "limit": lim, "ratio": round(self.ratio(m), 3)} for m, lim in self.limits.items()}


class SpendGuard(_Persisted):
    def __init__(self, daily_budget_usd: float, exempt: tuple = (), path: Optional[Path] = None, reset_hour_utc: int = 0,
                 clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)) -> None:
        super().__init__(path, reset_hour_utc, clock)
        self.budget, self.exempt = daily_budget_usd, set(exempt)

    def record(self, usd: float) -> None:
        with self.lock:
            self._roll()
            self.data["usd"] = self.data.get("usd", 0.0) + usd
            self._save()

    def spent(self) -> float:
        with self.lock:
            self._roll()
            return self.data.get("usd", 0.0)

    def ratio(self) -> float:
        return self.spent() / self.budget if self.budget else 0.0

    def check(self, caller: str) -> Optional[str]:
        if self.budget and caller not in self.exempt and self.spent() >= self.budget:
            return f"the daily budget of ${self.budget:g} is spent (${self.spent():.4f})"
        return None

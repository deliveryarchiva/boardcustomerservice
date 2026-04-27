"""Cache in-memory dello snapshot Customer Service Board.

Comportamento (PRD §4.1, Q&A §1.6 §7.35):
- TTL configurabile via POLL_INTERVAL_SECONDS (default 600 = 10 min).
- Cache lazy: la prima richiesta dopo restart attende il primo fetch.
- Concorrenza: se più utenti si collegano nella stessa finestra di scadenza,
  solo UN fetch è in corso; gli altri aspettano e ricevono lo stesso snapshot.
- Degrado 429: se il fetch fallisce per rate limit, conserva lo snapshot stale
  e segnala il banner di degrado fino al successo del fetch successivo.

Singola istanza Railway (PRD §5): la cache è in-memory non condivisa, voluto.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

log = logging.getLogger("csb.cache")

DEFAULT_TTL = int(os.getenv("POLL_INTERVAL_SECONDS", "600"))


@dataclass
class CacheState:
    snapshot: Optional[dict] = None
    fetched_at: float = 0.0
    degraded: bool = False
    degraded_since: float = 0.0
    next_retry_at: float = 0.0
    last_error: Optional[str] = None
    fetch_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SnapshotCache:
    """Wrapper attorno a CacheState. Espone un'unica API: get_or_fetch()."""

    def __init__(self, ttl_s: int = DEFAULT_TTL):
        self.ttl_s = ttl_s
        self.state = CacheState()

    def is_fresh(self) -> bool:
        s = self.state
        return (
            s.snapshot is not None
            and (time.time() - s.fetched_at) < self.ttl_s
        )

    def seconds_until_expiry(self) -> int:
        if self.state.snapshot is None:
            return 0
        return max(0, int(self.ttl_s - (time.time() - self.state.fetched_at)))

    async def get_or_fetch(
        self, fetch_fn: Callable[[], Awaitable[dict]]
    ) -> dict:
        """Restituisce lo snapshot, eseguendo un fetch se la cache è stale.

        Concorrenza: il primo a entrare nel lock fa il fetch; gli altri aspettano
        che termini e leggono il risultato dalla cache (Q&A §1.6).
        """
        if self.is_fresh():
            return self._decorate(self.state.snapshot)

        async with self.state.fetch_lock:
            # Doppio check: chi era in coda potrebbe trovare cache già aggiornata.
            if self.is_fresh():
                return self._decorate(self.state.snapshot)

            # In modalità degraded, rispetta next_retry_at per non martellare Atlassian.
            if (
                self.state.degraded
                and self.state.snapshot is not None
                and time.time() < self.state.next_retry_at
            ):
                log.info(
                    "Cache stale ma in backoff: servo snapshot stale fino a retry."
                )
                return self._decorate(self.state.snapshot)

            try:
                new_snapshot = await fetch_fn()
                self.state.snapshot = new_snapshot
                self.state.fetched_at = time.time()
                self.state.degraded = False
                self.state.degraded_since = 0.0
                self.state.next_retry_at = 0.0
                self.state.last_error = None
                log.info(
                    "Snapshot aggiornato (rows=%d/%d, KPI=%s)",
                    len(new_snapshot.get("rows") or []),
                    len(new_snapshot.get("rowsAll") or []),
                    new_snapshot.get("kpi"),
                )
            except Exception as e:
                log.error("Fetch snapshot fallito: %s", e)
                self.state.last_error = str(e)
                if self.state.snapshot is None:
                    # Mai avuto un fetch riuscito: rilancia, /api/snapshot risponderà 503.
                    raise
                # Avevo uno snapshot precedente: marca degrado e servi stale.
                self.state.degraded = True
                if not self.state.degraded_since:
                    self.state.degraded_since = time.time()
                self.state.next_retry_at = time.time() + 60  # retry tra 60s
                return self._decorate(self.state.snapshot)

            return self._decorate(self.state.snapshot)

    def _decorate(self, snapshot: dict) -> dict:
        """Aggiunge metadati di stato cache allo snapshot prima di servirlo."""
        out = dict(snapshot)
        out["fetchedAtMs"] = int(self.state.fetched_at * 1000)
        out["ttlSeconds"] = self.ttl_s
        out["nextRefreshSeconds"] = self.seconds_until_expiry()
        out["degraded"] = self.state.degraded
        out["degradedSinceMs"] = (
            int(self.state.degraded_since * 1000) if self.state.degraded else 0
        )
        out["lastError"] = self.state.last_error if self.state.degraded else None
        return out


# Istanza singleton riutilizzata da main.py
cache = SnapshotCache()

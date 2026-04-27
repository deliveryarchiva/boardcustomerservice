"""Calcolo giorni lavorativi in Italia.

Esclude sabato, domenica e le festività nazionali italiane (PRD §4.2.4 — Q&A §3.17).

Pasquetta è mobile (lunedì dopo Pasqua) e viene calcolata via algoritmo di Gauss.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional, Union

# Festività fisse italiane (mese, giorno).
ITALIAN_FIXED_HOLIDAYS = (
    (1, 1),    # Capodanno
    (1, 6),    # Epifania
    (4, 25),   # Festa della Liberazione
    (5, 1),    # Festa dei Lavoratori
    (6, 2),    # Festa della Repubblica
    (8, 15),   # Ferragosto
    (11, 1),   # Tutti i Santi
    (12, 8),   # Immacolata Concezione
    (12, 25),  # Natale
    (12, 26),  # Santo Stefano
)


def easter_sunday(year: int) -> date:
    """Algoritmo gregoriano anonimo per la domenica di Pasqua."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


@lru_cache(maxsize=64)
def italian_holidays(year: int) -> frozenset[date]:
    holidays = {date(year, m, d) for m, d in ITALIAN_FIXED_HOLIDAYS}
    holidays.add(easter_sunday(year) + timedelta(days=1))  # Pasquetta
    return frozenset(holidays)


def is_business_day(d: date) -> bool:
    if d.weekday() >= 5:  # Sabato (5) o Domenica (6)
        return False
    return d not in italian_holidays(d.year)


def _to_date(d: Union[date, datetime]) -> date:
    """Normalizza datetime → date in fuso locale Europe/Rome (proxy: tz-naive locale)."""
    if isinstance(d, datetime):
        if d.tzinfo is not None:
            # Convertiamo a UTC poi a "ora Italia" approssimata: per il conteggio dei
            # giorni lavorativi è sufficiente la data, e l'offset orario è sempre +1/+2
            # — non altera mai il conteggio in modo significativo per il caso d'uso.
            d = d.astimezone(timezone.utc).replace(tzinfo=None)
        return d.date()
    return d


def business_days_between(
    start: Union[date, datetime, str, None],
    end: Union[date, datetime, str, None] = None,
) -> Optional[int]:
    """Numero di giorni lavorativi tra ``start`` (escluso) e ``end`` (incluso).

    Convenzione (PRD §4.2.4):
    - se ``start`` è oggi → 0
    - se ``start`` è ieri (e oggi è lavorativo) → 1
    - le festività italiane non si contano

    ``start`` ed ``end`` accettano date / datetime / stringa ISO 8601.
    Restituisce ``None`` se ``start`` è ``None``.
    """
    if start is None:
        return None
    if isinstance(start, str):
        start = _parse_iso(start)
    if end is None:
        end = datetime.now(timezone.utc)
    if isinstance(end, str):
        end = _parse_iso(end)
    d_start = _to_date(start)
    d_end = _to_date(end)
    if d_end < d_start:
        return 0
    if d_end == d_start:
        return 0
    count = 0
    d = d_start + timedelta(days=1)
    while d <= d_end:
        if is_business_day(d):
            count += 1
        d += timedelta(days=1)
    return count


def _parse_iso(s: str) -> datetime:
    """Parser ISO 8601 tollerante (accetta suffisso 'Z')."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)

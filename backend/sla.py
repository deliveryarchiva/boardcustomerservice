"""Parser dati SLA Atlassian — PRD §4.2.2 colonna 12.

Atlassian risponde a ``GET /rest/servicedeskapi/request/{id}/sla`` con:

```
{
  "values": [
    {
      "name": "Time to first response",
      "ongoingCycle": {
        "remainingTime": {"millis": 12345, "friendly": "1h 23m"},
        "breached": false,
        "paused": false,
        "withinCalendarHours": true
      },
      "completedCycles": [...]
    },
    ...
  ]
}
```

Strategia:
1. Si privilegia il primo SLA con ``ongoingCycle`` non ``paused``.
2. Se nessun ciclo è ongoing, si cerca un ciclo completo "breached".
3. Se nessuno dei due, si segnala ``state="none"`` (label "— pausa" coerente
   con il mockup).

Fase 1: l'eventuale fallback su ``duedate`` (PRD §9 R2) verrà valutato solo se
la validazione su dataset reale lo rende necessario.
"""
from __future__ import annotations

from typing import Optional

# Soglia per passare da "ok" a "warn" (in millisecondi). 4h = 14400000ms.
SLA_WARN_THRESHOLD_MS = 4 * 60 * 60 * 1000


def _humanize_ms(ms: int) -> str:
    """Formatta '1g 23h 45m' a partire da millisecondi (positivi o negativi)."""
    if ms is None:
        return "—"
    sign = "-" if ms < 0 else ""
    ms = abs(int(ms))
    seconds = ms // 1000
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}g")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes:02d}m")
    return sign + " ".join(parts)


def parse_sla(payload: Optional[dict]) -> dict:
    """Restituisce il dict SLA pronto per la UI.

    Output:
        {
          "state": "ok" | "warn" | "breached" | "none",
          "label": "✓ 1g 03h 12m" | "⚠ Breached -2g 4h" | "— pausa",
          "remainingMs": int (positivo se rimane tempo, negativo se breached) | None
        }
    """
    if not payload:
        return _none_sla()

    values = payload.get("values") or []
    # 1) cerca un ciclo ongoing non in pausa
    for sla in values:
        cycle = sla.get("ongoingCycle") or {}
        if not cycle:
            continue
        if cycle.get("paused"):
            continue
        ms = ((cycle.get("remainingTime") or {}).get("millis"))
        breached = bool(cycle.get("breached"))
        if breached or (isinstance(ms, int) and ms < 0):
            return {
                "state": "breached",
                "label": f"⚠ Breached {_humanize_ms(ms or 0)}",
                "remainingMs": ms,
            }
        if isinstance(ms, int):
            state = "warn" if ms <= SLA_WARN_THRESHOLD_MS else "ok"
            symbol = "⏱" if state == "warn" else "✓"
            return {
                "state": state,
                "label": f"{symbol} {_humanize_ms(ms)}",
                "remainingMs": ms,
            }
    # 2) nessun ciclo ongoing: se esiste almeno un completedCycle breached, segnalalo
    for sla in values:
        for completed in (sla.get("completedCycles") or []):
            if completed.get("breached"):
                return {
                    "state": "breached",
                    "label": "⚠ Breached",
                    "remainingMs": None,
                }
    return _none_sla()


def _none_sla() -> dict:
    return {"state": "none", "label": "— pausa", "remainingMs": None}

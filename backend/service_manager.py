"""Sezione Service Manager — registry clienti + fetch ticket dedicato + stats.

Modello dati (JSON su volume Railway, allineato a `users.json`):

    data/service_manager/
      customers.json         registry: code, name, service_manager, helpdesk, backup
      customers/<CODE>/      docs/, docs.json, sal.json, appunti.json (M7.B)

Permessi:
  - admin: CRUD registry + accesso a tutti i clienti
  - user: lettura registry; scrittura solo sui clienti dove è SM/helpdesk/backup

Il fetch dei ticket per cliente non riusa lo snapshot urgent (è un sottoinsieme):
faccio una JQL dedicata per code con cache per-cliente TTL allineato.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .auth import DATA_DIR, load_users
from .business_days import business_days_between
from .jira_client import JiraClient, is_demo_mode
from .transform import (
    _read_custom_yes_no,
    issue_to_row,
    is_active_status,
    is_waiting_for_reporter,
    is_waiting_for_son,
)

log = logging.getLogger("csb.sm")

SM_DIR = DATA_DIR / "service_manager"
SM_DIR.mkdir(parents=True, exist_ok=True)
CUSTOMERS_FILE = SM_DIR / "customers.json"

JIRA_PROJECT_KEY_TC = os.getenv("JIRA_PROJECT_KEY_TC", "TC")
JIRA_DATE_FROM = os.getenv("JIRA_DATE_FROM", "2026/01/01")
FIELD_CUSTOMER_CODE = os.getenv("JIRA_FIELD_CUSTOMER_CODE", "")
TICKETS_TTL_S = int(os.getenv("POLL_INTERVAL_SECONDS", "600"))


# ============================================================
#  Registry — CRUD su customers.json
# ============================================================

def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write+replace con retry su Windows: il filesystem può ritardare il rilascio
    del lock dopo write_text quando le richieste sono ravvicinate, generando
    PermissionError [WinError 5]."""
    tmp = str(path) + ".tmp"
    Path(tmp).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    last_exc = None
    for delay in (0, 0.05, 0.1, 0.2, 0.4):
        if delay:
            time.sleep(delay)
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:
            last_exc = e
    raise last_exc


def load_customers() -> list[dict]:
    if not CUSTOMERS_FILE.exists():
        return []
    try:
        return json.loads(CUSTOMERS_FILE.read_text("utf-8"))
    except Exception as e:
        log.warning("customers.json corrotto, ricreo vuoto: %s", e)
        return []


def _save_customers(customers: list[dict]) -> None:
    _atomic_write_json(CUSTOMERS_FILE, customers)


def get_customer(code: str) -> Optional[dict]:
    code_norm = (code or "").strip()
    for c in load_customers():
        if c.get("code") == code_norm:
            return c
    return None


def _validate_username(username: str | None, allow_empty: bool = False) -> str:
    """Verifica che lo username (se valorizzato) corrisponda a un utente con
    ruolo applicativo `user` o `admin` (no ospiti). Restituisce stringa pulita."""
    if not username:
        if allow_empty:
            return ""
        raise ValueError("username obbligatorio")
    u = username.strip().lower()
    users = load_users()
    rec = users.get(u)
    if not rec:
        raise ValueError(f"utente '{u}' non censito")
    if rec.get("ruolo") not in ("admin", "user"):
        raise ValueError(f"utente '{u}' deve avere ruolo admin o user")
    return u


def create_customer(
    *,
    code: str,
    name: str,
    service_manager_username: str,
    helpdesk_username: str,
    backup_username: str | None,
    notes: str = "",
    created_by: str,
) -> dict:
    code_norm = (code or "").strip()
    if not code_norm:
        raise ValueError("Codice cliente obbligatorio")
    if not (name or "").strip():
        raise ValueError("Nome cliente obbligatorio")
    customers = load_customers()
    if any(c.get("code") == code_norm for c in customers):
        raise ValueError(f"Cliente {code_norm} già censito")
    record = {
        "code": code_norm,
        "name": name.strip(),
        "service_manager": _validate_username(service_manager_username),
        "helpdesk": _validate_username(helpdesk_username),
        "backup": _validate_username(backup_username, allow_empty=True),
        "notes": (notes or "").strip(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": created_by,
    }
    customers.append(record)
    _save_customers(customers)
    # Predispone le directory per documenti / sal / appunti (M7.B)
    (SM_DIR / "customers" / code_norm / "docs").mkdir(parents=True, exist_ok=True)
    return record


def update_customer(code: str, patch: dict) -> dict:
    customers = load_customers()
    for i, c in enumerate(customers):
        if c.get("code") == code:
            if "name" in patch and patch["name"]:
                c["name"] = patch["name"].strip()
            if "service_manager" in patch:
                c["service_manager"] = _validate_username(patch["service_manager"])
            if "helpdesk" in patch:
                c["helpdesk"] = _validate_username(patch["helpdesk"])
            if "backup" in patch:
                c["backup"] = _validate_username(patch.get("backup"), allow_empty=True)
            if "notes" in patch:
                c["notes"] = (patch["notes"] or "").strip()
            customers[i] = c
            _save_customers(customers)
            return c
    raise KeyError(f"Cliente {code} non trovato")


def delete_customer(code: str) -> bool:
    customers = load_customers()
    new = [c for c in customers if c.get("code") != code]
    if len(new) == len(customers):
        return False
    _save_customers(new)
    return True


def user_can_write_customer(user: dict, customer: dict) -> bool:
    if user.get("ruolo") == "admin":
        return True
    u = (user.get("username") or "").lower()
    return u in {customer.get("service_manager"), customer.get("helpdesk"), customer.get("backup")}


# ============================================================
#  Cache ticket per cliente
# ============================================================

class _CustomerTicketsCache:
    """Cache in-memory per i ticket del singolo cliente. Ogni code ha il proprio
    timestamp e lock — fetch concorrenti sullo stesso cliente sono serializzati,
    fetch su clienti diversi proseguono in parallelo."""

    def __init__(self, ttl_s: int = TICKETS_TTL_S):
        self.ttl_s = ttl_s
        self._entries: dict[str, dict] = {}

    def _entry(self, code: str) -> dict:
        if code not in self._entries:
            self._entries[code] = {
                "rows": None,
                "fetched_at": 0.0,
                "lock": asyncio.Lock(),
            }
        return self._entries[code]

    async def get_or_fetch(
        self, code: str, fetch_fn: Callable[[], Awaitable[list[dict]]]
    ) -> tuple[list[dict], int]:
        e = self._entry(code)
        now = time.time()
        if e["rows"] is not None and (now - e["fetched_at"]) < self.ttl_s:
            return e["rows"], int(self.ttl_s - (now - e["fetched_at"]))
        async with e["lock"]:
            now = time.time()
            if e["rows"] is not None and (now - e["fetched_at"]) < self.ttl_s:
                return e["rows"], int(self.ttl_s - (now - e["fetched_at"]))
            rows = await fetch_fn()
            e["rows"] = rows
            e["fetched_at"] = time.time()
            return rows, self.ttl_s

    def invalidate(self, code: str) -> None:
        if code in self._entries:
            self._entries[code]["rows"] = None
            self._entries[code]["fetched_at"] = 0.0


tickets_cache = _CustomerTicketsCache()


def _customfield_id_only(raw: str) -> str:
    """`customfield_12091` → `12091` (per uso in JQL `cf[12091]`)."""
    if not raw:
        return ""
    m = re.search(r"(\d+)", raw)
    return m.group(1) if m else ""


def _build_customer_jql(code: str) -> str:
    code_clean = code.replace('"', '\\"')
    cf_id = _customfield_id_only(FIELD_CUSTOMER_CODE)
    if cf_id:
        cond = f'cf[{cf_id}] = "{code_clean}"'
    else:
        # Fallback: nome friendly (rischia di non risolvere se il campo ha label diverso)
        cond = f'"Codice Cliente" = "{code_clean}"'
    return (
        f'project = {JIRA_PROJECT_KEY_TC} AND created >= "{JIRA_DATE_FROM}" '
        f'AND {cond} ORDER BY created DESC'
    )


async def _fetch_customer_tickets_real(client: JiraClient, code: str) -> list[dict]:
    from .snapshot import _fields_to_request  # evita ciclo a tempo di import

    fields = _fields_to_request()
    jql = _build_customer_jql(code)
    log.info("Fetch ticket cliente %s: %s", code, jql)
    issues = await client.search_issues(jql, fields=fields)
    return [issue_to_row(i) for i in issues]


def _fetch_customer_tickets_demo(code: str) -> list[dict]:
    from .demo_data import build_demo_snapshot

    snap = build_demo_snapshot()
    return [r for r in snap.get("rowsAll", []) if (r.get("customer") or {}).get("code") == code]


async def get_customer_tickets(client: JiraClient, code: str) -> tuple[list[dict], int]:
    """Restituisce (rows, secondi al prossimo refresh). Cache TTL allineato a snapshot."""
    if is_demo_mode():
        async def _fn():
            return _fetch_customer_tickets_demo(code)
        return await tickets_cache.get_or_fetch(code, _fn)

    async def _fn():
        return await _fetch_customer_tickets_real(client, code)
    return await tickets_cache.get_or_fetch(code, _fn)


# ============================================================
#  Statistiche
# ============================================================

def _month_key(iso_dt: str | None) -> str | None:
    if not iso_dt:
        return None
    try:
        return datetime.fromisoformat(iso_dt.replace("Z", "+00:00")).strftime("%Y-%m")
    except Exception:
        return None


def _calendar_days(iso_a: str | None, iso_b: str | None) -> Optional[float]:
    if not iso_a or not iso_b:
        return None
    try:
        a = datetime.fromisoformat(iso_a.replace("Z", "+00:00"))
        b = datetime.fromisoformat(iso_b.replace("Z", "+00:00"))
        return (b - a).total_seconds() / 86400.0
    except Exception:
        return None


def _bucket_age(days: float | None) -> str:
    if days is None:
        return "n/a"
    if days < 7:
        return "<7gg"
    if days < 30:
        return "7-30gg"
    if days < 90:
        return "30-90gg"
    return ">90gg"


def compute_stats(rows: list[dict]) -> dict:
    """Calcola statistiche aggregate per la vista cliente.

    Tutto è derivato dai campi già in `issue_to_row`. Le metriche che richiedono
    changelog (reopen rate, transizioni reparti) o custom field non ancora
    censiti (servizio, ore fatturabili) sono escluse — vedi `gaps` nel payload.
    """
    total = len(rows)
    closed = [r for r in rows if not r.get("isActive")]
    active = [r for r in rows if r.get("isActive")]

    # Per mese (apertura / chiusura)
    by_month: dict[str, dict[str, int]] = {}
    for r in rows:
        m = _month_key(r.get("createdAt"))
        if m:
            by_month.setdefault(m, {"opened": 0, "closed": 0})["opened"] += 1
    for r in closed:
        m = _month_key(r.get("resolutionDate"))
        if m:
            by_month.setdefault(m, {"opened": 0, "closed": 0})["closed"] += 1
    months_sorted = sorted(by_month.keys())
    monthly = [{"month": m, **by_month[m]} for m in months_sorted]

    # Tempo medio risoluzione (gg calendario), su chiusi
    durations = []
    for r in closed:
        d = _calendar_days(r.get("createdAt"), r.get("resolutionDate"))
        if d is not None and d >= 0:
            durations.append(d)
    avg_resolution_days = round(sum(durations) / len(durations), 1) if durations else None

    # Tempo medio gg s/risc. (lavorativi) su attivi
    waits = [r["daysWithoutResponse"] for r in active if r.get("daysWithoutResponse") is not None]
    avg_wait_days = round(sum(waits) / len(waits), 1) if waits else None

    # SLA respect rate (sui ticket con SLA valutabile, attivi)
    sla_states = {"ok": 0, "warn": 0, "breached": 0, "none": 0}
    for r in active:
        s = (r.get("sla") or {}).get("state") or "none"
        sla_states[s] = sla_states.get(s, 0) + 1
    measurable = sla_states["ok"] + sla_states["warn"] + sla_states["breached"]
    sla_respect_pct = (
        round(100 * (sla_states["ok"] + sla_states["warn"]) / measurable, 1)
        if measurable else None
    )

    # Distribuzione per tipologia (issueType)
    by_type: dict[str, int] = {}
    for r in rows:
        t = (r.get("issueType") or "Altro").strip() or "Altro"
        by_type[t] = by_type.get(t, 0) + 1

    # Distribuzione per priority
    by_priority: dict[str, int] = {}
    for r in rows:
        p = (r.get("priority") or "—").strip() or "—"
        by_priority[p] = by_priority.get(p, 0) + 1

    # Backlog age (solo attivi)
    backlog: dict[str, int] = {"<7gg": 0, "7-30gg": 0, "30-90gg": 0, ">90gg": 0, "n/a": 0}
    now_iso = datetime.now(timezone.utc).isoformat()
    for r in active:
        bucket = _bucket_age(_calendar_days(r.get("createdAt"), now_iso))
        backlog[bucket] = backlog.get(bucket, 0) + 1

    # Ticket in stallo (≥8gg lavorativi senza riscontro, severity red)
    stalled = sum(1 for r in active if r.get("severity") == "red")

    return {
        "total": total,
        "active": len(active),
        "closed": len(closed),
        "stalled": stalled,
        "avgResolutionDays": avg_resolution_days,
        "avgWaitDays": avg_wait_days,
        "slaRespectPct": sla_respect_pct,
        "slaStates": sla_states,
        "monthly": monthly,
        "byType": by_type,
        "byPriority": by_priority,
        "backlog": backlog,
        # Voci richieste in roadmap ma non derivabili dal solo campo standard:
        "gaps": [
            "Distribuzione per servizio: richiede custom field 'Servizio' (da censire in env).",
            "Ore fatturabili per mese: richiede custom field 'Tempo fatturabile' (da censire in env).",
            "Tempo di stallo per reparto: richiede analisi changelog (M7.C).",
            "Reopen rate: richiede analisi changelog stato (M7.C).",
        ],
    }


# ============================================================
#  Assets per cliente — Documenti / SAL / Appunti  (M7.B)
# ============================================================

# Whitelist mime types accettati per upload documenti.
DOC_ALLOWED_MIME = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # docx
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # xlsx
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # pptx
    "application/vnd.ms-outlook",  # .msg (alcuni browser)
    "application/octet-stream",  # fallback per .msg / file generici
    "image/png",
    "image/jpeg",
    "text/plain",
}
DOC_ALLOWED_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".msg", ".png", ".jpg", ".jpeg", ".txt"}
DOC_MAX_BYTES = 10 * 1024 * 1024  # 10 MB

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_filename(name: str) -> str:
    name = (name or "").strip().replace("/", "_").replace("\\", "_")
    safe = _SAFE_NAME_RE.sub("_", name)[:120]
    return safe or "file"


def _customer_dir(code: str) -> Path:
    p = SM_DIR / "customers" / code
    (p / "docs").mkdir(parents=True, exist_ok=True)
    return p


def _read_json_list(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        log.warning("%s corrotto, ricreo vuoto: %s", path, e)
        return []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


# ----- Documenti -----

def list_docs(code: str) -> list[dict]:
    return _read_json_list(_customer_dir(code) / "docs.json")


def add_doc(code: str, *, filename: str, mime: str, content: bytes, uploader: str) -> dict:
    if len(content) > DOC_MAX_BYTES:
        raise ValueError(f"File oltre il limite di {DOC_MAX_BYTES // (1024*1024)} MB")
    ext = Path(filename).suffix.lower()
    if mime not in DOC_ALLOWED_MIME and ext not in DOC_ALLOWED_EXT:
        raise ValueError(f"Tipo file non consentito: {mime or ext or 'sconosciuto'}")
    cust_dir = _customer_dir(code)
    doc_id = _new_id()
    safe_name = _sanitize_filename(filename)
    stored_name = f"{doc_id}__{safe_name}"
    (cust_dir / "docs" / stored_name).write_bytes(content)
    record = {
        "id": doc_id,
        "filename": filename,
        "storedName": stored_name,
        "mime": mime or "application/octet-stream",
        "size": len(content),
        "uploaded_at": _now(),
        "uploaded_by": uploader,
    }
    docs = list_docs(code)
    docs.append(record)
    _atomic_write_json(cust_dir / "docs.json", docs)
    return record


def get_doc_path(code: str, doc_id: str) -> tuple[Path, dict]:
    for d in list_docs(code):
        if d.get("id") == doc_id:
            path = _customer_dir(code) / "docs" / d["storedName"]
            if not path.exists():
                raise FileNotFoundError(f"File fisico mancante per doc {doc_id}")
            return path, d
    raise KeyError(f"Documento {doc_id} non trovato")


def delete_doc(code: str, doc_id: str) -> bool:
    cust_dir = _customer_dir(code)
    docs = list_docs(code)
    target = next((d for d in docs if d.get("id") == doc_id), None)
    if not target:
        return False
    path = cust_dir / "docs" / target["storedName"]
    if path.exists():
        try:
            path.unlink()
        except OSError as e:
            log.warning("Impossibile rimuovere file %s: %s", path, e)
    _atomic_write_json(cust_dir / "docs.json", [d for d in docs if d.get("id") != doc_id])
    return True


# ----- SAL (Stato Avanzamento Lavori) -----

def list_sal(code: str) -> list[dict]:
    items = _read_json_list(_customer_dir(code) / "sal.json")
    items.sort(key=lambda s: s.get("date", ""), reverse=True)
    return items


def add_sal(
    code: str,
    *,
    date: str,
    oggetto: str,
    partecipanti: list[str],
    minute: str,
    next_steps: str,
    author: str,
) -> dict:
    if not (date or "").strip():
        raise ValueError("Data SAL obbligatoria")
    if not (oggetto or "").strip():
        raise ValueError("Oggetto SAL obbligatorio")
    record = {
        "id": _new_id(),
        "date": date.strip(),
        "oggetto": oggetto.strip(),
        "partecipanti": [p.strip() for p in partecipanti if (p or "").strip()],
        "minute": minute or "",
        "next_steps": next_steps or "",
        "created_at": _now(),
        "created_by": author,
    }
    items = _read_json_list(_customer_dir(code) / "sal.json")
    items.append(record)
    _atomic_write_json(_customer_dir(code) / "sal.json", items)
    return record


def update_sal(code: str, sal_id: str, patch: dict) -> dict:
    items = _read_json_list(_customer_dir(code) / "sal.json")
    for i, s in enumerate(items):
        if s.get("id") == sal_id:
            for k in ("date", "oggetto", "minute", "next_steps"):
                if k in patch:
                    s[k] = (patch[k] or "").strip() if k in ("date", "oggetto") else (patch[k] or "")
            if "partecipanti" in patch and isinstance(patch["partecipanti"], list):
                s["partecipanti"] = [p.strip() for p in patch["partecipanti"] if (p or "").strip()]
            s["updated_at"] = _now()
            items[i] = s
            _atomic_write_json(_customer_dir(code) / "sal.json", items)
            return s
    raise KeyError(f"SAL {sal_id} non trovato")


def delete_sal(code: str, sal_id: str) -> bool:
    items = _read_json_list(_customer_dir(code) / "sal.json")
    new = [s for s in items if s.get("id") != sal_id]
    if len(new) == len(items):
        return False
    _atomic_write_json(_customer_dir(code) / "sal.json", new)
    return True


# ----- Appunti -----

def list_appunti(code: str) -> list[dict]:
    items = _read_json_list(_customer_dir(code) / "appunti.json")
    items.sort(key=lambda a: a.get("created_at", ""), reverse=True)
    return items


def add_appunto(code: str, *, text: str, author: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("Testo appunto obbligatorio")
    record = {
        "id": _new_id(),
        "text": text,
        "created_at": _now(),
        "created_by": author,
    }
    items = _read_json_list(_customer_dir(code) / "appunti.json")
    items.append(record)
    _atomic_write_json(_customer_dir(code) / "appunti.json", items)
    return record


def update_appunto(code: str, appunto_id: str, *, text: str, user: dict) -> dict:
    items = _read_json_list(_customer_dir(code) / "appunti.json")
    for i, a in enumerate(items):
        if a.get("id") == appunto_id:
            if user.get("ruolo") != "admin" and a.get("created_by") != user.get("username"):
                raise PermissionError("Puoi modificare solo i tuoi appunti (admin escluso)")
            text_clean = (text or "").strip()
            if not text_clean:
                raise ValueError("Testo appunto obbligatorio")
            a["text"] = text_clean
            a["updated_at"] = _now()
            items[i] = a
            _atomic_write_json(_customer_dir(code) / "appunti.json", items)
            return a
    raise KeyError(f"Appunto {appunto_id} non trovato")


def delete_appunto(code: str, appunto_id: str, user: dict) -> bool:
    items = _read_json_list(_customer_dir(code) / "appunti.json")
    target = next((a for a in items if a.get("id") == appunto_id), None)
    if not target:
        return False
    if user.get("ruolo") != "admin" and target.get("created_by") != user.get("username"):
        raise PermissionError("Puoi eliminare solo i tuoi appunti (admin escluso)")
    _atomic_write_json(
        _customer_dir(code) / "appunti.json",
        [a for a in items if a.get("id") != appunto_id],
    )
    return True

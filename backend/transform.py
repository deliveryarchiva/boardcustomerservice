"""Trasformazioni issue Jira → row UI e calcolo KPI.

Implementa (M2 + M3):
- normalizzazione status (case-insensitive EN+IT) — PRD §4.2.1, Q&A §2.10
- flag condizioni Highest / In Escalation / Spoccato
- mapping campi base
- KPI 1-4
- severity riga (0-2 verde / 3-7 arancione / ≥8 rosso) — PRD §4.2.4

In M4 verrà aggiunto il render delle tabelle con filtri/ordinamento.
"""
from __future__ import annotations

import os
from typing import Any, Optional

JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "").rstrip("/")
FIELD_TICKET_SPOCCATO = os.getenv("JIRA_FIELD_TICKET_SPOCCATO", "")
FIELD_IN_ESCALATION = os.getenv("JIRA_FIELD_IN_ESCALATION", "")
FIELD_CUSTOMER_NAME = os.getenv("JIRA_FIELD_CUSTOMER_NAME", "")
FIELD_CUSTOMER_CODE = os.getenv("JIRA_FIELD_CUSTOMER_CODE", "")

# Status normalizzati (lowercase) → famiglia per badge UI
# PRD §4.2.1 + Q&A §2.10: confronto case-insensitive su EN + IT.
# Stati Archiva osservati su istanza reale 2026-04-28: "Aperto", "Work in progress",
# "Waiting for reporter", "Waiting for son", "Replied from reporter",
# "Replied from son", "Sleeping", "Assegnata", "In attesa di risposta dal cliente".
STATUS_FAMILIES: dict[str, str] = {
    # Waiting for reporter / In attesa cliente → wfr
    "waiting for reporter": "wfr",
    "waiting for customer": "wfr",
    "in attesa cliente": "wfr",
    "in attesa del cliente": "wfr",
    "attesa cliente": "wfr",
    "in attesa di risposta dal cliente": "wfr",
    "in attesa risposta cliente": "wfr",
    "sleeping": "wfr",  # stato dormiente Archiva, concettualmente in attesa
    # Waiting for son / In attesa del figlio → wfson
    "waiting for son": "wfson",
    "waiting for child": "wfson",
    "in attesa del figlio": "wfson",
    "in attesa figlio": "wfson",
    # In progress / lavorazione attiva
    "in progress": "progress",
    "work in progress": "progress",
    "in lavorazione": "progress",
    "in corso": "progress",
    "assegnata": "progress",
    "assegnato": "progress",
    "replied from reporter": "progress",
    "replied from son": "progress",
    # Open / To Do
    "open": "open",
    "to do": "open",
    "aperto": "open",
    "nuovo": "open",
    # Resolved / Done
    "done": "resolved",
    "resolved": "resolved",
    "closed": "resolved",
    "completato": "resolved",
    "risolto": "resolved",
    "chiuso": "resolved",
}

# Fallback dalla statusCategory standard Atlassian (new/indeterminate/done) per
# stati di workflow custom non mappati esplicitamente.
_CATEGORY_FALLBACK = {
    "done": "resolved",
    "indeterminate": "progress",
    "new": "open",
}


def status_family(status_name: str, status_category_key: Optional[str] = None) -> str:
    """Famiglia di stato per il badge (progress/wfr/wfson/open/resolved).

    Cerca prima nella mappa esplicita per nome (case-insensitive); se non trovato
    e ``status_category_key`` è valorizzato (``new``/``indeterminate``/``done``),
    deriva il colore da quello — copertura per workflow custom.
    """
    if not status_name:
        return "open"
    fam = STATUS_FAMILIES.get(status_name.strip().lower())
    if fam:
        return fam
    if status_category_key:
        return _CATEGORY_FALLBACK.get(status_category_key.lower(), "open")
    return "open"


def is_waiting_for_reporter(status_name: str) -> bool:
    return status_family(status_name) == "wfr"


def is_waiting_for_son(status_name: str) -> bool:
    return status_family(status_name) == "wfson"


def is_active_status(status_category_key: str) -> bool:
    """statusCategory ≠ Done. Atlassian usa key: 'new' | 'indeterminate' | 'done'."""
    return (status_category_key or "").lower() != "done"


_TRUTHY_VALUES = {"yes", "true", "si", "sì", "1"}


def _read_custom_yes_no(fields: dict, custom_id: str) -> bool:
    """Legge un custom field Yes/No senza assumere se è option, boolean o multicheckbox.

    Atlassian può restituire:
      - ``true`` / ``false`` (checkbox)
      - ``"Yes"`` / ``"No"`` (stringa)
      - ``{"value": "Yes"}`` (option singola)
      - ``[{"value": "Yes"}, {"value": "Altro"}]`` (multicheckboxes — verificato
        sull'istanza Archiva: ``customfield_11787`` SPOCCATO e ``customfield_12384``
        IN ESCALATION sono entrambi ``multicheckboxes``)
    Per il caso multicheckbox restituisce True se UN qualunque elemento ha
    ``value`` truthy (l'utente ha "checkato" Yes anche se ha selezionato altro).
    """
    if not custom_id:
        return False
    raw = fields.get(custom_id)
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in _TRUTHY_VALUES
    if isinstance(raw, dict):
        v = raw.get("value")
        return isinstance(v, str) and v.strip().lower() in _TRUTHY_VALUES
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                v = item.get("value")
                if isinstance(v, str) and v.strip().lower() in _TRUTHY_VALUES:
                    return True
            elif isinstance(item, str) and item.strip().lower() in _TRUTHY_VALUES:
                return True
        return False
    return False


def _initials(full_name: str) -> str:
    if not full_name:
        return "?"
    parts = [p for p in full_name.split() if p]
    return ("".join(p[0] for p in parts[:2]) or "?").upper()


def _build_url(key: str) -> str:
    return f"{JIRA_BASE_URL}/browse/{key}" if JIRA_BASE_URL and key else "#"


def issue_to_row(issue: dict) -> dict:
    """Mappa una issue Jira nel dict riga UI (versione M2 — sarà arricchita in M3/M4)."""
    fields = issue.get("fields") or {}
    key = issue.get("key", "")
    status = fields.get("status") or {}
    status_name = status.get("name", "")
    status_category = (status.get("statusCategory") or {}).get("key", "")
    priority_name = (fields.get("priority") or {}).get("name", "") or ""
    assignee = fields.get("assignee") or None
    assignee_name = (assignee or {}).get("displayName") if assignee else None
    return {
        "key": key,
        "issueId": issue.get("id"),
        "project": (fields.get("project") or {}).get("key") or key.split("-")[0],
        "summary": fields.get("summary") or "",
        "status": status_name,
        "statusFamily": status_family(status_name),
        "statusCategory": status_category,
        "isActive": is_active_status(status_category),
        "isHighest": priority_name.strip().lower() == "highest",
        "isEscalation": _read_custom_yes_no(fields, FIELD_IN_ESCALATION),
        "isSpoccato": _read_custom_yes_no(fields, FIELD_TICKET_SPOCCATO),
        "assignee": (
            {
                "displayName": assignee_name,
                "initials": _initials(assignee_name or ""),
            }
            if assignee_name
            else None
        ),
        "customer": _extract_customer(fields),
        "createdAt": fields.get("created"),
        "resolutionDate": fields.get("resolutiondate"),
        "url": _build_url(key),
        # Campi M3 — popolati in snapshot.py durante l'orchestrazione.
        "lastResponseToClient": None,
        "lastResponseAuthor": None,
        "daysWithoutResponse": None,
        "daysFromFallback": False,
        "severity": "green",
        "leaf": None,            # dict da LeafInfo.to_dict() oppure None
        "leafStallDays": None,   # gg lavorativi dall'ultimo commento del leaf
        "sla": {"state": "none", "label": "—", "remainingMs": None},
    }


def _extract_customer(fields: dict) -> Optional[dict]:
    """Estrae cliente dai custom field configurati:
      - ``JIRA_FIELD_CUSTOMER_NAME`` (option, default Archiva: ``customfield_12159``)
      - ``JIRA_FIELD_CUSTOMER_CODE`` (textfield, default Archiva: ``customfield_12091``)

    Fallback su ``reporter.displayName`` se il name non è valorizzato (utile per
    ticket vecchi dove il campo non era ancora compilato).
    """
    name = _read_option_value(fields, FIELD_CUSTOMER_NAME)
    if not name:
        reporter = fields.get("reporter") or {}
        name = reporter.get("displayName") or ""
    code = _read_option_value(fields, FIELD_CUSTOMER_CODE)
    if not name and not code:
        return None
    return {"name": name, "code": code}


def _read_option_value(fields: dict, custom_id: str) -> str:
    """Legge un custom field di tipo 'select' (option) → restituisce la stringa value.

    Atlassian può restituire ``{"value": "Nome", "id": "..."}`` oppure ``None``.
    """
    if not custom_id:
        return ""
    raw = fields.get(custom_id)
    if isinstance(raw, dict):
        v = raw.get("value")
        if isinstance(v, str):
            return v.strip()
    if isinstance(raw, str):
        return raw.strip()
    return ""


def severity_class(days_without_response: Optional[int]) -> str:
    """Mappa giorni → classe colore riga (PRD §4.2.4)."""
    if days_without_response is None:
        return "green"
    if days_without_response <= 2:
        return "green"
    if days_without_response <= 7:
        return "orange"
    return "red"


def is_orphan_hdx(issue: dict, parent_keys_in_dataset: set[str]) -> bool:
    """Un HDX è orfano se non ha un Parent valido verso un TC del dataset (PRD §4.1)."""
    fields = issue.get("fields") or {}
    project_key = (fields.get("project") or {}).get("key")
    if project_key != "HDX":
        return False
    parent = fields.get("parent")
    if isinstance(parent, dict) and parent.get("key"):
        return parent["key"] not in parent_keys_in_dataset
    # Fallback: cerca tra issuelinks un link parent verso un TC
    for link in fields.get("issuelinks") or []:
        for side in ("inwardIssue", "outwardIssue"):
            target = link.get(side) or {}
            if target.get("key", "").startswith("TC-"):
                return target["key"] not in parent_keys_in_dataset
    return True


# ============================================================
#  KPI
# ============================================================

def compute_kpi(rows_urgent_tc_all_period: list[dict]) -> dict:
    """Calcola i 4 KPI sul **pool urgent** (TC con condizione OR Highest /
    Spoccato / In Escalation). PRD §4.2.1, rivisto 2026-04-28: la board ha come
    focus i soli ticket urgenti, quindi tutti i KPI si riferiscono a quel pool.

    - KPI 1 — TC urgenti aperti (urgent + attivi)
    - KPI 2 — TC urgenti totali nel periodo (urgent, attivi + chiusi) — denominatore
    - KPI 3 — TC urgenti aperti in Waiting for reporter
    - KPI 4 — TC urgenti aperti in Waiting for son
    """
    urgent_active = [r for r in rows_urgent_tc_all_period if r["isActive"]]
    return {
        "tcUrgentiAperti": len(urgent_active),
        "tcUrgentiPeriodo": len(rows_urgent_tc_all_period),
        "tcWaitingForReporter": sum(1 for r in urgent_active if is_waiting_for_reporter(r["status"])),
        "tcWaitingForSon": sum(1 for r in urgent_active if is_waiting_for_son(r["status"])),
    }

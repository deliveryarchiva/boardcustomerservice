"""Calcolo "ultima risposta verso il cliente" — PRD §4.2.4, Q&A §3.13–§3.15.

Regole:
1. Solo commenti pubblici (``jsdPublic = true``).
2. L'autore NON è il reporter del ticket (i commenti del reporter sono "lato cliente",
   anche quando il reporter è un account interno Archiva — Q&A §3.13).
3. L'autore NON è in blocklist bot/automation (Q&A §3.14).
4. Si confronta sempre ``comment.created`` (mai ``updated``, Q&A §3.15) per evitare
   che un edit di un vecchio commento azzeri ingiustamente il calcolo.
5. Se nessun commento valido esiste → fallback su ``issue.updated`` con flag dedicato.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger("csb.comments")


def _bot_blocklist() -> set[str]:
    """Set di accountId / displayName configurato via env (csv)."""
    raw = os.getenv("JIRA_BOT_BLOCKLIST", "")
    return {s.strip() for s in raw.split(",") if s.strip()}


def _author_id(comment: dict) -> str:
    return ((comment.get("author") or {}).get("accountId") or "").strip()


def _author_name(comment: dict) -> str:
    return ((comment.get("author") or {}).get("displayName") or "").strip()


def find_last_operator_response(
    comments: list[dict],
    reporter_account_id: str,
    bot_blocklist: Optional[set[str]] = None,
) -> Optional[dict]:
    """Trova l'ultimo commento operatore valido tra ``comments``.

    Restituisce il dict commento Jira originale (o None se nessuno valido).
    Atlassian ordina di solito per ``created`` ascendente — qui non assumiamo
    nulla e iteriamo trovando il massimo per ``created``.
    """
    if not comments:
        return None
    blocklist = bot_blocklist if bot_blocklist is not None else _bot_blocklist()
    candidates = []
    for c in comments:
        if not c.get("jsdPublic", False):
            continue
        author_id = _author_id(c)
        author_name = _author_name(c)
        if not author_id and not author_name:
            continue
        if reporter_account_id and author_id == reporter_account_id:
            continue
        if author_id in blocklist or author_name in blocklist:
            continue
        if not c.get("created"):
            continue
        candidates.append(c)
    if not candidates:
        return None
    return max(candidates, key=lambda c: c["created"])


def find_last_human_comment(
    comments: list[dict],
    bot_blocklist: Optional[set[str]] = None,
) -> Optional[dict]:
    """Ultimo commento di un autore *umano* (= non in blocklist bot/automation).

    Differisce da ``find_last_operator_response`` perché:
    - NON esclude il reporter (qualunque autore va bene)
    - NON richiede ``jsdPublic=true`` (anche commenti interni contano come "vita"
      sull'issue, utile per misurare lo stallo del leaf)

    Usata da ``_stall_days_for_leaf`` (PRD §4.2.5 / Q&A §4.24: *"giorni
    dall'ultimo commento del leaf"*).
    """
    if not comments:
        return None
    blocklist = bot_blocklist if bot_blocklist is not None else _bot_blocklist()
    candidates = []
    for c in comments:
        author_id = _author_id(c)
        author_name = _author_name(c)
        if not author_id and not author_name:
            continue
        if author_id in blocklist or author_name in blocklist:
            continue
        if not c.get("created"):
            continue
        candidates.append(c)
    if not candidates:
        return None
    return max(candidates, key=lambda c: c["created"])


def extract_response_info(
    comments: list[dict], issue: dict
) -> dict:
    """Costruisce un dict pronto da iniettare nella row UI.

    Output:
        {
            "lastResponseToClient": "2026-04-16T...",  # o None
            "lastResponseAuthor":   "Marco Bianchi",   # o None
            "fromFallback":         False              # True se fallback su updated
        }
    """
    fields = issue.get("fields") or {}
    reporter_id = (fields.get("reporter") or {}).get("accountId") or ""
    last = find_last_operator_response(comments, reporter_id)
    if last is not None:
        return {
            "lastResponseToClient": last.get("created"),
            "lastResponseAuthor": _author_name(last) or None,
            "fromFallback": False,
        }
    return {
        "lastResponseToClient": fields.get("updated"),
        "lastResponseAuthor": None,
        "fromFallback": True,
    }

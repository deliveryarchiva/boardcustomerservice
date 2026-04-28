"""Orchestratore: costruisce lo snapshot da Jira e popola la cache.

Flow (PRD §4.1, §4.2.4, §4.2.5):
  1. Fetch dataset urgent (TC + HDX con OR) e dataset all-TC.
  2. Filtra HDX orfani.
  3. Per ogni TC del dataset urgent:
     a. fetch commenti → calcolo "ultima risposta operatore" + giorni lavorativi senza riscontro
     b. fetch SLA → badge state/label
     c. se in Waiting for son: risolve la catena → 1+ leaf (eventualmente in fallback)
        Per ogni leaf: fetch commenti del leaf → calcolo "Ngg fermo".
  4. Materializza le righe: 1 riga per TC senza leaf, N righe per TC con N leaf.
  5. Calcola KPI sul dataset all-TC.

Il fetch parallelo di commenti/SLA è limitato per non saturare il rate limit
(asyncio.Semaphore con concorrenza configurabile).
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from . import demo_data
from .business_days import business_days_between
from .comments import extract_response_info
from .jira_client import JiraClient, is_demo_mode
from .leaf import LeafInfo, build_parent_index, resolve_for_tc
from .sla import parse_sla
from .transform import (
    compute_kpi,
    is_orphan_hdx,
    is_waiting_for_son,
    issue_to_row,
    severity_class,
)

log = logging.getLogger("csb.snapshot")

JIRA_PROJECT_KEY_TC = os.getenv("JIRA_PROJECT_KEY_TC", "TC")
JIRA_PROJECT_KEY_HDX = os.getenv("JIRA_PROJECT_KEY_HDX", "HDX")
JIRA_DATE_FROM = os.getenv("JIRA_DATE_FROM", "2026/01/01")
FIELD_TICKET_SPOCCATO = os.getenv("JIRA_FIELD_TICKET_SPOCCATO", "")
FIELD_IN_ESCALATION = os.getenv("JIRA_FIELD_IN_ESCALATION", "")
FIELD_CUSTOMER_NAME = os.getenv("JIRA_FIELD_CUSTOMER_NAME", "")
ENRICH_CONCURRENCY = int(os.getenv("JIRA_ENRICH_CONCURRENCY", "5"))

BASE_FIELDS = [
    "summary",
    "status",
    "priority",
    "assignee",
    "reporter",
    "created",
    "updated",
    "resolutiondate",
    "project",
    "parent",
    "issuelinks",
]


def _build_or_jql() -> str:
    or_clauses = ['priority = Highest']
    if FIELD_TICKET_SPOCCATO:
        or_clauses.append('"TICKET SPOCCATO" = Yes')
    if FIELD_IN_ESCALATION:
        or_clauses.append('"IN ESCALATION" = Yes')
    or_part = " OR ".join(or_clauses)
    return (
        f"(project = {JIRA_PROJECT_KEY_TC} OR project = {JIRA_PROJECT_KEY_HDX}) "
        f'AND created >= "{JIRA_DATE_FROM}" '
        f"AND ({or_part}) "
        f"ORDER BY created DESC"
    )


def _build_all_tc_jql() -> str:
    return (
        f"project = {JIRA_PROJECT_KEY_TC} "
        f'AND created >= "{JIRA_DATE_FROM}" '
        f"ORDER BY created DESC"
    )


def _fields_to_request() -> list[str]:
    fields = list(BASE_FIELDS)
    for cf in (FIELD_TICKET_SPOCCATO, FIELD_IN_ESCALATION, FIELD_CUSTOMER_NAME):
        if cf:
            fields.append(cf)
    return fields


# ============================================================
#  Enrichment
# ============================================================

async def _enrich_response_and_sla(
    issue: dict, client: JiraClient, sem: asyncio.Semaphore
) -> dict:
    """Per una singola issue, fetch commenti+SLA → dict di patch da applicare alla row."""
    key = issue.get("key", "")
    async with sem:
        comments_task = asyncio.create_task(client.get_comments(key))
        sla_task = asyncio.create_task(client.get_sla(key))
        try:
            comments = await comments_task
        except Exception as e:
            log.warning("Commenti fallback per %s: %s", key, e)
            comments = []
        try:
            sla_payload = await sla_task
        except Exception as e:
            log.warning("SLA fallback per %s: %s", key, e)
            sla_payload = None

    response = extract_response_info(comments, issue)
    days = business_days_between(response["lastResponseToClient"])
    sla = parse_sla(sla_payload)
    return {
        "lastResponseToClient": response["lastResponseToClient"],
        "lastResponseAuthor": response["lastResponseAuthor"],
        "daysWithoutResponse": days,
        "daysFromFallback": response["fromFallback"],
        "severity": severity_class(days),
        "sla": sla,
    }


async def _stall_days_for_leaf(
    leaf_info: LeafInfo, client: JiraClient, sem: asyncio.Semaphore
) -> Optional[int]:
    """Calcola gg lavorativi dall'ultimo commento del leaf (Q&A §4.24)."""
    if leaf_info.leaf_issue is None:
        return None
    async with sem:
        try:
            comments = await client.get_comments(leaf_info.leaf_key)
        except Exception as e:
            log.warning("Commenti leaf fallback per %s: %s", leaf_info.leaf_key, e)
            comments = []
    response = extract_response_info(comments, leaf_info.leaf_issue)
    return business_days_between(response["lastResponseToClient"])


# ============================================================
#  Materialization
# ============================================================

def _materialize_rows(
    tc_row: dict, leaves: list[LeafInfo], leaf_stall_days: dict[str, Optional[int]]
) -> list[dict]:
    """Produce 1 riga (TC senza leaf) o N righe (una per leaf)."""
    if not leaves:
        return [tc_row]
    out = []
    for leaf in leaves:
        row = dict(tc_row)
        row["leaf"] = leaf.to_dict()
        row["leafStallDays"] = leaf_stall_days.get(leaf.leaf_key)
        out.append(row)
    return out


# ============================================================
#  Entrypoint reale
# ============================================================

async def fetch_snapshot(client: JiraClient) -> dict:
    fields = _fields_to_request()
    or_jql = _build_or_jql()
    all_tc_jql = _build_all_tc_jql()

    log.info("JQL urgent: %s", or_jql)
    log.info("JQL all-TC: %s", all_tc_jql)

    urgent_issues, all_tc_issues = await asyncio.gather(
        client.search_issues(or_jql, fields=fields),
        client.search_issues(all_tc_jql, fields=fields),
    )

    # Esclusione HDX orfani.
    tc_keys_in_urgent = {
        i.get("key")
        for i in urgent_issues
        if (i.get("fields") or {}).get("project", {}).get("key") == "TC"
    }
    filtered_urgent = [
        i for i in urgent_issues if not is_orphan_hdx(i, tc_keys_in_urgent)
    ]

    # Indice parent → children (M3.5).
    parent_index = build_parent_index(filtered_urgent)

    # Solo i TC del dataset urgent ATTIVI vanno mostrati nella tabella "Vista TC Attivi";
    # gli HDX sono usati per il leaf, e i TC chiusi solo nel tab "Tutti i TC" (PRD §4.2.2).
    urgent_tcs = [
        i for i in filtered_urgent
        if (i.get("fields") or {}).get("project", {}).get("key") == "TC"
        and ((i.get("fields") or {}).get("status") or {}).get("statusCategory", {}).get("key") != "done"
    ]

    sem = asyncio.Semaphore(ENRICH_CONCURRENCY)

    # 1) arricchimento commenti+SLA per tutti i TC urgent.
    enrich_tasks = [_enrich_response_and_sla(tc, client, sem) for tc in urgent_tcs]
    enrich_results = await asyncio.gather(*enrich_tasks, return_exceptions=False)

    # 2) risoluzione catena per ogni TC in waiting-for-son.
    leaf_tasks = []
    for tc in urgent_tcs:
        status_name = ((tc.get("fields") or {}).get("status") or {}).get("name", "")
        if is_waiting_for_son(status_name):
            leaf_tasks.append(resolve_for_tc(tc, parent_index, client))
        else:
            leaf_tasks.append(asyncio.sleep(0, result=[]))
    leaf_results: list[list[LeafInfo]] = await asyncio.gather(*leaf_tasks)

    # 3) gg fermo per tutti i leaf (deduplicato per chiave).
    unique_leaves: dict[str, LeafInfo] = {}
    for leaves in leaf_results:
        for li in leaves:
            unique_leaves.setdefault(li.leaf_key, li)
    stall_results = await asyncio.gather(
        *[_stall_days_for_leaf(li, client, sem) for li in unique_leaves.values()]
    )
    leaf_stall_days = dict(zip(unique_leaves.keys(), stall_results))

    # 4) materializzazione delle righe.
    rows: list[dict] = []
    for tc, patch, leaves in zip(urgent_tcs, enrich_results, leaf_results):
        base_row = issue_to_row(tc)
        base_row.update(patch)
        rows.extend(_materialize_rows(base_row, leaves, leaf_stall_days))

    # Tab "Tutti i TC": righe non arricchite (M2 baseline; M4 deciderà se arricchire anche queste).
    rows_all = [issue_to_row(i) for i in all_tc_issues]
    kpi = compute_kpi([issue_to_row(i) for i in filtered_urgent], rows_all)

    return {
        "demo": False,
        "rows": rows,
        "rowsAll": rows_all,
        "kpi": kpi,
    }


async def build_snapshot(client: JiraClient) -> dict:
    if is_demo_mode():
        log.info("Modalità DEMO (JIRA_API_TOKEN non valorizzato).")
        return demo_data.build_demo_snapshot()
    return await fetch_snapshot(client)

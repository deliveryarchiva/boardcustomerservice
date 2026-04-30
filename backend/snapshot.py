"""Orchestratore: costruisce lo snapshot da Jira e popola la cache.

Flow (PRD §4.1, §4.2.4, §4.2.5 — rivisto 2026-04-28):
  1. Un unico fetch del dataset urgent (TC + HDX con OR Highest/Spoccato/Escalation).
     L'intera board (KPI + entrambi i tab) è ristretta a questo pool — quindi
     non serve più la JQL all-TC del periodo (risparmio ~7000 issue per fetch).
  2. Filtra HDX orfani.
  3. Per ogni TC del dataset urgent ATTIVI:
     a. fetch commenti → calcolo "ultima risposta operatore" + giorni lavorativi
     b. fetch SLA → badge state/label
     c. se in Waiting for son: risolve la catena → 1+ leaf
  4. Materializza le righe attive: 1 riga per TC senza leaf, N righe per TC con N leaf.
  5. rowsAll (tab "Tutti i TC") = tutti i TC del dataset urgent (attivi + chiusi),
     senza enrichment commenti/SLA (pesante e non necessario per i TC chiusi).
  6. Calcola KPI sul pool urgent.

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
from .comments import extract_response_info, find_last_human_comment
from .jira_client import JiraClient, is_demo_mode
from .leaf import LeafInfo, build_parent_index, resolve_for_tc
from .sla import parse_sla
from .transform import (
    compute_kpi,
    is_active_status,
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
FIELD_CUSTOMER_CODE = os.getenv("JIRA_FIELD_CUSTOMER_CODE", "")
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
    # I custom field 'TICKET SPOCCATO' e 'IN ESCALATION' sono multicheckbox con
    # valori in italiano: l'opzione si chiama "SI" (verificato 2026-04-28 su
    # un campione reale: customfield_11787 e customfield_12384 → value="SI").
    # Usare "= Yes" produrrebbe 0 match.
    or_clauses = ['priority = Highest']
    if FIELD_TICKET_SPOCCATO:
        or_clauses.append('"TICKET SPOCCATO" = SI')
    if FIELD_IN_ESCALATION:
        or_clauses.append('"IN ESCALATION" = SI')
    or_part = " OR ".join(or_clauses)
    return (
        f"(project = {JIRA_PROJECT_KEY_TC} OR project = {JIRA_PROJECT_KEY_HDX}) "
        f'AND created >= "{JIRA_DATE_FROM}" '
        f"AND ({or_part}) "
        f"ORDER BY created DESC"
    )


def _chunks(lst: list, size: int):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


async def _expand_chain_with_fetch(
    client: JiraClient,
    fields: list[str],
    issues: list[dict],
    parent_index: dict[str, list[dict]],
    max_depth: int = 10,
) -> tuple[list[dict], dict[str, list[dict]]]:
    """Fetch supplementare iterativo per popolare i figli mancanti dei ticket
    in Waiting for son attivi (PRD §4.2.5).

    Caso d'uso: TC-X (WFS, Highest) ha figlio HDX-Y (WFS, NON Highest) che ha
    figlio HDX-Z (sleeping). HDX-Y non rientra nella JQL urgent → parent_index
    incompleto → mostreremmo HDX-Y come leaf invece di HDX-Z.

    Nota implementativa: su JSM Cloud Archiva la relazione TC↔HDX è espressa
    via ``issuelinks`` di tipo "Parent" (outward "Padre DI" → figlio), NON via
    il campo nativo ``fields.parent``. La JQL ``parent = X`` ritorna sempre 0
    risultati. Per ricavare i figli leggiamo direttamente ``issuelinks`` del
    nodo padre, poi fetchiamo le issue figlie via ``issuekey in (...)``.
    """
    from .leaf import build_parent_index as _rebuild

    seen_keys = {i.get("key") for i in issues}
    for depth in range(max_depth):
        # 1) Identifica i ticket WFS attivi senza figli attivi noti
        needs_expand: list[dict] = []
        for i in issues:
            f = i.get("fields") or {}
            status = f.get("status") or {}
            status_name = status.get("name", "")
            status_cat = (status.get("statusCategory") or {}).get("key", "")
            if not is_active_status(status_cat) or not is_waiting_for_son(status_name):
                continue
            children = parent_index.get(i.get("key", ""), [])
            active_children = [
                c for c in children
                if is_active_status(((c.get("fields") or {}).get("status") or {}).get("statusCategory", {}).get("key", ""))
            ]
            if not active_children:
                needs_expand.append(i)
        if not needs_expand:
            return issues, parent_index

        # 2) Estrai i KEY dei figli dagli issuelinks (link Parent outward)
        children_keys_to_fetch: set[str] = set()
        for parent_issue in needs_expand:
            for link in (parent_issue.get("fields") or {}).get("issuelinks") or []:
                if (link.get("type") or {}).get("name", "").lower() != "parent":
                    continue
                ow = link.get("outwardIssue") or {}
                child_key = ow.get("key")
                if child_key and child_key not in seen_keys:
                    children_keys_to_fetch.add(child_key)
        if not children_keys_to_fetch:
            return issues, parent_index

        log.info(
            "Fetch supplementare children depth=%d: %d parent WFS, %d figli da fetchare",
            depth, len(needs_expand), len(children_keys_to_fetch),
        )

        # 3) Batch fetch dei figli con issuekey in (...)
        new_children: list[dict] = []
        for chunk in _chunks(list(children_keys_to_fetch), 50):
            jql = f"issuekey in ({', '.join(chunk)})"
            result = await client.search_issues(jql, fields=fields)
            for c in result:
                if c.get("key") not in seen_keys:
                    seen_keys.add(c.get("key"))
                    new_children.append(c)
        if not new_children:
            return issues, parent_index

        issues = issues + new_children
        parent_index = _rebuild(issues)
    return issues, parent_index


def _fields_to_request() -> list[str]:
    fields = list(BASE_FIELDS)
    for cf in (FIELD_TICKET_SPOCCATO, FIELD_IN_ESCALATION, FIELD_CUSTOMER_NAME, FIELD_CUSTOMER_CODE):
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
    """Calcola gg lavorativi dall'ultimo commento del leaf (Q&A §4.24).

    Usa ``find_last_human_comment`` (qualunque autore non-bot) — NON l'ultimo
    commento operatore. Per il leaf vogliamo misurare lo stallo "umano" indipen-
    dentemente dal ruolo: anche un commento del reporter conta come "vita". Se
    non c'è alcun commento umano, fallback a ``issue.created`` (NON ``updated``,
    che viene mosso da Automation for Jira / Time to SLA e falserebbe il dato).
    """
    if leaf_info.leaf_issue is None:
        return None
    async with sem:
        try:
            comments = await client.get_comments(leaf_info.leaf_key)
        except Exception as e:
            log.warning("Commenti leaf fallback per %s: %s", leaf_info.leaf_key, e)
            comments = []
    last = find_last_human_comment(comments)
    if last is not None:
        return business_days_between(last.get("created"))
    # Nessun commento umano — usa created del leaf (stabile, immune ad automation)
    fields = leaf_info.leaf_issue.get("fields") or {}
    return business_days_between(fields.get("created"))


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
    log.info("JQL urgent: %s", or_jql)

    urgent_issues = await client.search_issues(or_jql, fields=fields)

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

    # Espande la catena con fetch supplementare per i figli fuori dal dataset urgent
    # (PRD §4.2.5): l'effettivo leaf può essere un HDX non Highest/SPOC/ESC.
    filtered_urgent, parent_index = await _expand_chain_with_fetch(
        client, fields, filtered_urgent, parent_index
    )

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

    # Tab "Tutti i TC": tutti i TC del dataset urgent (attivi + chiusi), no enrichment.
    urgent_tc_all_period = [
        i for i in filtered_urgent
        if (i.get("fields") or {}).get("project", {}).get("key") == "TC"
    ]
    rows_all = [issue_to_row(i) for i in urgent_tc_all_period]
    kpi = compute_kpi(rows_all)

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

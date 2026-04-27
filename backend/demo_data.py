"""Fixtures per modalità demo (quando JIRA_API_TOKEN non è valorizzato).

Le righe ricalcano fedelmente il mockup `mockup/index.html`. I valori dei KPI
sono derivati dalle righe; i giorni senza riscontro / leaf / SLA sono pre-bakati
per permettere il test visivo della UI prima del collegamento reale a Jira.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _iso(s: str) -> str:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).isoformat()


def _row(
    *,
    key,
    summary,
    status,
    family,
    active=True,
    highest=False,
    escalation=False,
    spoccato=False,
    assignee=None,
    customer_name,
    customer_code="",
    created,
    last_response=None,
    last_response_author=None,
    days,
    days_from_fallback=False,
    severity,
    leaf=None,
    leaf_stall_days=None,
    sla,
):
    return {
        "key": key,
        "issueId": key.split("-")[1],
        "project": key.split("-")[0],
        "summary": summary,
        "status": status,
        "statusFamily": family,
        "statusCategory": "indeterminate" if active else "done",
        "isActive": active,
        "isHighest": highest,
        "isEscalation": escalation,
        "isSpoccato": spoccato,
        "assignee": assignee,
        "customer": {"name": customer_name, "code": customer_code},
        "createdAt": _iso(created),
        "resolutionDate": None,
        "url": f"#{key}",
        "lastResponseToClient": _iso(last_response) if last_response else None,
        "lastResponseAuthor": last_response_author,
        "daysWithoutResponse": days,
        "daysFromFallback": days_from_fallback,
        "severity": severity,
        "leaf": leaf,
        "leafStallDays": leaf_stall_days,
        "sla": sla,
    }


def _ag(initials, name):
    return {"displayName": name, "initials": initials}


def _leaf(*, direct_child, leaf_key, stall_assignee, chain_too_deep=False, attesa_cliente=False):
    return {
        "directChildKey": direct_child,
        "leafKey": leaf_key,
        "leafStatus": "Waiting for son" if direct_child != leaf_key else "In Progress",
        "leafAssignee": stall_assignee,
        "chainTooDeep": chain_too_deep,
        "attesaClienteHelpdesk": attesa_cliente,
    }


def _sla(state, label, ms=None):
    return {"state": state, "label": label, "remainingMs": ms}


# Le righe replicano il mockup, in ordine:
DEMO_ROWS = [
    # Riga 1 — TC-1042 rosso, escalation totale, leaf catena 2 livelli
    _row(
        key="TC-1042",
        summary="Errore esportazione fatture passive — blocco chiusura mese contabile, urgente per scadenza 30/04",
        status="Waiting for son", family="wfson",
        highest=True, escalation=True, spoccato=True,
        assignee=_ag("GN", "Giulia Nogara"),
        customer_name="Rossi S.p.A.", customer_code="CL-00231",
        created="2026-04-14",
        last_response="2026-04-16", last_response_author="Marco Bianchi",
        days=8, severity="red",
        leaf=_leaf(direct_child="HDX-3318", leaf_key="HDX-3401", stall_assignee="L. Verdi (Sviluppo)"),
        leaf_stall_days=7,
        sla=_sla("breached", "⚠ Breached -2g 4h"),
    ),
    # Riga 2 — TC-1037 rosso, fallback su updated (no commenti)
    _row(
        key="TC-1037",
        summary="Anomalia conservazione sostitutiva lotti Q1 2026 — necessario intervento prima dell'audit",
        status="In Progress", family="progress",
        highest=True, spoccato=True,
        assignee=_ag("PR", "Paolo Russo"),
        customer_name="Banca Veneta Holding", customer_code="CL-00088",
        created="2026-04-10",
        last_response=None, last_response_author=None,
        days=9, days_from_fallback=True, severity="red",
        sla=_sla("breached", "⚠ Breached -5g 18h"),
    ),
    # Riga 3 — TC-1029 arancione, catena troppo profonda
    _row(
        key="TC-1029",
        summary="Workflow approvazione DDT non si chiude su step finale — riproducibile su 3 utenti",
        status="Waiting for son", family="wfson",
        highest=True, escalation=True,
        assignee=_ag("CP", "Chiara Pettenuzzo"),
        customer_name="Mediterranea Logistics", customer_code="CL-00412",
        created="2026-04-06",
        last_response="2026-04-20", last_response_author="Chiara Pettenuzzo",
        days=5, severity="orange",
        leaf=_leaf(direct_child="HDX-3267", leaf_key="HDX-3267", stall_assignee=None, chain_too_deep=True),
        leaf_stall_days=None,
        sla=_sla("warn", "⏱ 12h 04m", ms=12*3600*1000),
    ),
    # Riga 4 — TC-1024 arancione, primo dei due figli (HDX-3301)
    _row(
        key="TC-1024",
        summary="Migrazione storico documenti pre-2024 verso nuovo storage — perimetro multi-modulo",
        status="Waiting for son", family="wfson",
        highest=True,
        assignee=_ag("SG", "Stefano Gazzani"),
        customer_name="Fabbrica Veneta SRL", customer_code="CL-00305",
        created="2026-04-02",
        last_response="2026-04-21", last_response_author="Stefano Gazzani",
        days=4, severity="orange",
        leaf=_leaf(direct_child="HDX-3301", leaf_key="HDX-3301", stall_assignee="M. Carraro (Storage)"),
        leaf_stall_days=4,
        sla=_sla("warn", "⏱ 1g 03h", ms=27*3600*1000),
    ),
    # Riga 4 bis — TC-1024 secondo figlio (HDX-3302)
    _row(
        key="TC-1024",
        summary="Migrazione storico documenti pre-2024 verso nuovo storage — perimetro multi-modulo",
        status="Waiting for son", family="wfson",
        highest=True,
        assignee=_ag("SG", "Stefano Gazzani"),
        customer_name="Fabbrica Veneta SRL", customer_code="CL-00305",
        created="2026-04-02",
        last_response="2026-04-17", last_response_author="Stefano Gazzani",
        days=6, severity="orange",
        leaf=_leaf(direct_child="HDX-3302", leaf_key="HDX-3302", stall_assignee="A. Lugli (Migration)"),
        leaf_stall_days=6,
        sla=_sla("warn", "⏱ 4h 12m", ms=4*3600*1000),
    ),
    # Riga 5 — TC-1019 verde, in attesa cliente
    _row(
        key="TC-1019",
        summary="Richiesta integrazione SDI per nuova sede — attesa codice destinatario",
        status="Waiting for reporter", family="wfr",
        spoccato=True,
        assignee=_ag("GN", "Giulia Nogara"),
        customer_name="Gruppo Brescia Industrie", customer_code="CL-00501",
        created="2026-04-22",
        last_response="2026-04-24", last_response_author="Giulia Nogara",
        days=1, severity="green",
        sla=_sla("ok", "✓ 18h 42m", ms=18*3600*1000+42*60*1000),
    ),
    # Riga 6 — TC-1015 verde, semplice, non assegnato
    _row(
        key="TC-1015",
        summary="Configurazione utente amministratore aggiuntivo per portale collaboratori",
        status="In Progress", family="progress",
        highest=True,
        assignee=None,
        customer_name="Studio Legale Conti & Partners", customer_code="CL-00177",
        created="2026-04-26",
        last_response="2026-04-26", last_response_author="Marco Bianchi",
        days=1, severity="green",
        sla=_sla("ok", "✓ 1g 22h", ms=46*3600*1000),
    ),
    # Riga 7 — TC-1011 arancione, attesa cliente al figlio diretto
    _row(
        key="TC-1011",
        summary="Errore intermittente API webhook ordini — log mostrano timeout su endpoint partner",
        status="Waiting for son", family="wfson",
        highest=True,
        assignee=_ag("PR", "Paolo Russo"),
        customer_name="CoopAlimentari Nord-Est", customer_code="CL-00622",
        created="2026-04-18",
        last_response="2026-04-22", last_response_author="Paolo Russo",
        days=3, severity="orange",
        leaf=_leaf(direct_child="HDX-3290", leaf_key="HDX-3290", stall_assignee=None, attesa_cliente=True),
        leaf_stall_days=None,
        sla=_sla("none", "— pausa"),
    ),
    # Riga 8 — TC-1008 verde, solo SPOCCATO (non Highest)
    _row(
        key="TC-1008",
        summary="Anomalia su report mensile fatturazione elettronica — differenza €1,2k vs Jira contabile",
        status="Open", family="open",
        spoccato=True,
        assignee=_ag("CP", "Chiara Pettenuzzo"),
        customer_name="PharmaItalia Distribuzione", customer_code="CL-00094",
        created="2026-04-25",
        last_response="2026-04-27", last_response_author="Chiara Pettenuzzo",
        days=0, severity="green",
        sla=_sla("ok", "✓ 3g 12h", ms=84*3600*1000),
    ),
]


def build_demo_snapshot() -> dict:
    """Costruisce uno snapshot completo per la modalità demo.

    KPI calcolati (tutti TC, perché in demo non abbiamo HDX standalone):
    - KPI 1: 8 (TC unici nel dataset urgent: tutti attivi e con almeno una condizione)
    - KPI 2: 7 (TC unici totali attivi — TC-1024 è duplicato per leaf, va contato 1)
    - KPI 3: 1 (TC-1019 in WFR)
    - KPI 4: 4 (TC-1042, TC-1029, TC-1024, TC-1011 in WFS)
    """
    # Deduplica per key prima di contare i KPI (TC-1024 duplicato per 2 leaf).
    unique_tc_keys = set()
    unique_rows: list[dict] = []
    for r in DEMO_ROWS:
        if r["key"] in unique_tc_keys:
            continue
        unique_tc_keys.add(r["key"])
        unique_rows.append(r)

    return {
        "demo": True,
        "rows": DEMO_ROWS,             # con duplicazione per leaf (UI table)
        "rowsAll": unique_rows,        # senza duplicati (Tab "Tutti i TC")
        "kpi": {
            # KPI 1: TC unici del dataset urgent attivi → 8
            "tcUrgentiAperti": 8,
            # KPI 2: tutti i TC del periodo attivi (in demo coincide; in prod sarà ≥ KPI 1)
            "tcInCorso": 8,
            # Stima rappresentativa di TC totali nel periodo (anche chiusi); usato come "su X totali"
            "tcTotaliPeriodo": 25,
            # KPI 3: TC-1019 in WFR
            "tcWaitingForReporter": 1,
            # KPI 4: TC-1042, TC-1029, TC-1024, TC-1011 in WFS
            "tcWaitingForSon": 4,
        },
    }

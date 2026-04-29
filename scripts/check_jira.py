"""Onboarding Jira — verifica connessione e individua i parametri da popolare in env.

Esegue in sequenza tutti i controlli che servono per andare in produzione:

  1. Auth: chiamata `/myself` per verificare email + token
  2. Custom field: cerca per nome 'TICKET SPOCCATO' e 'IN ESCALATION' → stampa l'ID
  3. Link types: cerca il Parent/Child standard JSM
  4. JQL base: esegue la query con OR conditional e mostra il count
  5. SLA: prende il primo TC dal risultato e tenta `/servicedeskapi/request/{id}/sla`

Variabili d'ambiente lette (vedi `.env.example`):
  JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY_TC, JIRA_PROJECT_KEY_HDX, JIRA_DATE_FROM

Uso:
  $ python -m scripts.check_jira

Output: stampa "OK"/"KO" per ogni step + i valori da copiare in `.env`.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Permette esecuzione sia con `python -m scripts.check_jira` che `python scripts/check_jira.py`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Forziamo UTF-8 sullo stdout/stderr per gestire i caratteri unicode (frecce, ecc.)
# anche su console Windows con encoding di default cp1252.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from backend.jira_client import JiraClient, JiraError, is_demo_mode  # noqa: E402


C_OK = "\033[32m"
C_KO = "\033[31m"
C_HI = "\033[36m"
C_DIM = "\033[2m"
C_END = "\033[0m"


def line(label: str, ok: bool, msg: str = "") -> None:
    flag = f"{C_OK}OK{C_END}" if ok else f"{C_KO}KO{C_END}"
    print(f"  [{flag}] {label}{(': ' + msg) if msg else ''}")


def header(title: str) -> None:
    print(f"\n{C_HI}=== {title} ==={C_END}")


async def step_auth(client: JiraClient) -> dict | None:
    header("1. Auth (chiamata /myself)")
    try:
        me = await client._request("GET", "/rest/api/3/myself")  # type: ignore[attr-defined]
    except JiraError as e:
        line("/myself", False, str(e))
        return None
    line(
        "/myself",
        True,
        f"loggato come {me.get('displayName')!r} ({me.get('emailAddress')})",
    )
    return me


async def step_custom_fields(client: JiraClient) -> None:
    header("2. Custom field (TICKET SPOCCATO, IN ESCALATION)")
    try:
        fields = await client._request("GET", "/rest/api/3/field")  # type: ignore[attr-defined]
    except JiraError as e:
        line("GET /rest/api/3/field", False, str(e))
        return
    targets = {"ticket spoccato": None, "in escalation": None}
    fuzzy = {"spoccato": [], "escalation": []}
    for f in fields:
        name = (f.get("name") or "").strip()
        low = name.lower()
        for k in targets:
            if low == k:
                targets[k] = f
        if "spoccato" in low and targets["ticket spoccato"] is None:
            fuzzy["spoccato"].append(f)
        if "escalation" in low and targets["in escalation"] is None:
            fuzzy["escalation"].append(f)

    for label, key in [
        ("TICKET SPOCCATO  → JIRA_FIELD_TICKET_SPOCCATO", "ticket spoccato"),
        ("IN ESCALATION    → JIRA_FIELD_IN_ESCALATION  ", "in escalation"),
    ]:
        f = targets[key]
        if f:
            schema = f.get("schema") or {}
            line(label, True, f"id={f['id']}  type={schema.get('type','?')}  custom={schema.get('custom','?')}")
        else:
            short = key.split()[0]
            candidates = fuzzy[short if short != "ticket" else "spoccato"]
            if candidates:
                names = ", ".join(c.get("name", "?") for c in candidates[:3])
                line(label, False, f"non trovato esattamente. Candidati simili: {names}")
            else:
                line(label, False, "non trovato. Verificare nome esatto sul board admin Jira.")


async def step_link_types(client: JiraClient) -> None:
    header("3. Link types (Parent/Child standard JSM)")
    try:
        data = await client._request("GET", "/rest/api/3/issueLinkType")  # type: ignore[attr-defined]
    except JiraError as e:
        line("GET /rest/api/3/issueLinkType", False, str(e))
        return
    types = data.get("issueLinkTypes") or []
    parent_like = []
    for t in types:
        n = (t.get("name") or "").lower()
        if any(w in n for w in ("parent", "child")):
            parent_like.append(t)
    if not parent_like:
        line("link type Parent/Child", False, f"nessuno trovato. Disponibili: {[t.get('name') for t in types]}")
        return
    for t in parent_like:
        line(
            "candidato",
            True,
            f"id={t.get('id')}  name={t.get('name')!r}  inward={t.get('inward')!r}  outward={t.get('outward')!r}",
        )
    print(f"  {C_DIM}Nota: in JSM Cloud il Parent standard è di solito un campo nativo (`fields.parent`),{C_END}")
    print(f"  {C_DIM}non un link type. Se la lista qui non ha 'Parent/Child', va bene — il backend usa già `parent`.{C_END}")


async def step_jql(client: JiraClient) -> list[dict]:
    header("4. JQL base (dataset urgent)")
    project_tc = os.getenv("JIRA_PROJECT_KEY_TC", "TC")
    project_hdx = os.getenv("JIRA_PROJECT_KEY_HDX", "HDX")
    date_from = os.getenv("JIRA_DATE_FROM", "2026/01/01")
    spoccato = os.getenv("JIRA_FIELD_TICKET_SPOCCATO", "")
    escalation = os.getenv("JIRA_FIELD_IN_ESCALATION", "")

    or_clauses = ["priority = Highest"]
    if spoccato:
        or_clauses.append('"TICKET SPOCCATO" = SI')
    if escalation:
        or_clauses.append('"IN ESCALATION" = SI')

    jql = (
        f"(project = {project_tc} OR project = {project_hdx}) "
        f'AND created >= "{date_from}" '
        f"AND ({' OR '.join(or_clauses)}) "
        "ORDER BY created DESC"
    )
    print(f"  JQL: {C_DIM}{jql}{C_END}")
    try:
        issues = await client.search_issues(jql, fields=["summary", "status", "project"], page_size=20)
    except JiraError as e:
        line("search_issues", False, str(e))
        return []
    line(
        "search_issues",
        True,
        f"{len(issues)} issue trovate (mostra prime 5)",
    )
    for i in issues[:5]:
        f = i.get("fields") or {}
        st = (f.get("status") or {}).get("name", "?")
        s = (f.get("summary") or "")[:60]
        print(f"     - {i.get('key'):<12} {st:<20} {s}")
    return issues


async def step_sla(client: JiraClient, issues: list[dict]) -> None:
    header("5. SLA (campione)")
    if not issues:
        line("nessuna issue da testare", False, "")
        return
    sample = next(
        (i for i in issues if (i.get("fields") or {}).get("project", {}).get("key") == "TC"),
        issues[0],
    )
    key = sample.get("key")
    print(f"  Test su {key}")
    try:
        data = await client.get_sla(key)
    except JiraError as e:
        line(f"GET .../request/{key}/sla", False, str(e))
        return
    if data is None:
        line(f"GET .../request/{key}/sla", False, "404 / SLA non disponibile per questa request type")
        return
    values = data.get("values") or []
    line(f"GET .../request/{key}/sla", True, f"{len(values)} cicli SLA esposti")
    for v in values[:3]:
        ongoing = v.get("ongoingCycle") or {}
        rt = (ongoing.get("remainingTime") or {}).get("friendly") if ongoing else None
        breached = ongoing.get("breached") if ongoing else None
        print(f"     - {v.get('name'):<30}  remaining={rt}  breached={breached}")


async def main() -> int:
    print(f"{C_HI}Customer Service Board — onboarding Jira{C_END}\n")
    if is_demo_mode():
        print(f"{C_KO}JIRA_BASE_URL/JIRA_EMAIL/JIRA_API_TOKEN non valorizzati — siamo in DEMO mode.{C_END}")
        print("Compila prima il file .env, poi rilancia.")
        return 1
    print(f"  base url: {os.getenv('JIRA_BASE_URL')}")
    print(f"  email:    {os.getenv('JIRA_EMAIL')}")
    client = JiraClient()
    try:
        me = await step_auth(client)
        if me is None:
            return 2
        await step_custom_fields(client)
        await step_link_types(client)
        issues = await step_jql(client)
        await step_sla(client, issues)
    finally:
        await client.close()
    print(f"\n{C_OK}Done.{C_END} Copia gli ID custom field nel tuo .env e rilancia.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

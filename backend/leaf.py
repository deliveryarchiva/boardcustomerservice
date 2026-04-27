"""Risoluzione catena Waiting for son → leaf ticket — PRD §4.2.5.

Idea generale:
- Un TC in stato `Waiting for son` ha (almeno) un HDX figlio non completato.
- L'attesa reale può essere più in profondità (HDX → HDX → ...). Si scende fino al
  primo nodo che NON è in `Waiting for son` (o fino a `max_depth`, default 10).

Regole rilevanti:
- **Figli multipli a livello TC**: se il TC ha N figli waiting, la board mostra N righe
  (una per ogni figlio). La duplicazione è gestita in `snapshot.py`, qui restituiamo
  una lista di leaf info per TC (Q&A §4.20).
- **Figlio diretto in Waiting for reporter**: stop a livello 2 con etichetta
  "Attesa Cliente / Helpdesk" (PRD §4.2.5, Q&A §4.22). Non si scende oltre.
- **Catena troppo profonda** (> max_depth): badge dedicato e fallback al ticket
  di partenza.
- **Leaf fuori dataset principale**: chiamata supplementare a
  ``GET /rest/api/3/issue/{leafKey}`` (PRD §4.2.5).

In M3 implementato come pure async function. Nota: la verifica del nome/ID esatto
del link type Parent/Child JSM è ancora in "Punti aperti" — qui usiamo il campo
``parent`` e ``issuelinks`` (con type "Parent" inward/outward) come euristica.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .jira_client import JiraClient
from .transform import is_active_status, is_waiting_for_reporter, is_waiting_for_son

log = logging.getLogger("csb.leaf")

MAX_DEPTH = 10


@dataclass
class LeafInfo:
    """Dati pronti da iniettare nella riga della tabella per un singolo leaf."""

    direct_child_key: str
    leaf_key: str
    leaf_status: str
    leaf_assignee: Optional[str]
    chain_too_deep: bool = False
    attesa_cliente_helpdesk: bool = False  # figlio diretto in Waiting for reporter
    leaf_issue: Optional[dict] = None  # issue Jira completa del leaf (per calcolo gg fermo)

    def to_dict(self) -> dict:
        return {
            "directChildKey": self.direct_child_key,
            "leafKey": self.leaf_key,
            "leafStatus": self.leaf_status,
            "leafAssignee": self.leaf_assignee,
            "chainTooDeep": self.chain_too_deep,
            "attesaClienteHelpdesk": self.attesa_cliente_helpdesk,
        }


# ============================================================
#  Helper: estrazione children
# ============================================================

def _children_keys_from_issue(issue: dict) -> list[str]:
    """Estrae i KEY dei figli di una issue dai field ``parent``/``issuelinks``.

    Nota: per trovare i figli di X servirebbe normalmente una JQL `parent = X`,
    NON i campi della issue X. Questa funzione è il *contrario*: leggere su X
    quale è il SUO parent (per indicizzare). I figli si raccolgono quindi
    invertendo l'indice (vedi build_parent_index).
    """
    fields = issue.get("fields") or {}
    parents: list[str] = []
    parent = fields.get("parent")
    if isinstance(parent, dict) and parent.get("key"):
        parents.append(parent["key"])
    for link in fields.get("issuelinks") or []:
        for side in ("inwardIssue", "outwardIssue"):
            target = link.get(side) or {}
            if (link.get("type") or {}).get("name", "").lower() in {"parent", "blocks", "is parent of"}:
                if target.get("key"):
                    parents.append(target["key"])
    return parents


def build_parent_index(all_issues: list[dict]) -> dict[str, list[dict]]:
    """Costruisce un indice ``parent_key → [child_issue, ...]`` dal dataset.

    Per ogni issue figlio, leggiamo il campo ``parent`` (campo standard JSM) e
    aggiungiamo l'issue alla lista del relativo padre. Le issue senza parent
    vengono ignorate.
    """
    index: dict[str, list[dict]] = {}
    for issue in all_issues:
        parent_keys = _children_keys_from_issue(issue)
        for pk in parent_keys:
            index.setdefault(pk, []).append(issue)
    return index


def _is_waiting_status(status_name: str) -> bool:
    """Stato 'waiting…' generico (incluso WFR e WFS)."""
    return is_waiting_for_reporter(status_name) or is_waiting_for_son(status_name)


def _filter_waiting_active(children: list[dict]) -> list[dict]:
    """Solo figli ancora attivi e in stato waiting (qualunque waiting…)."""
    out = []
    for c in children:
        fields = c.get("fields") or {}
        status = fields.get("status") or {}
        status_name = status.get("name", "")
        status_cat = (status.get("statusCategory") or {}).get("key", "")
        if is_active_status(status_cat) and _is_waiting_status(status_name):
            out.append(c)
    return out


# ============================================================
#  Risoluzione catena
# ============================================================

async def _fetch_issue_safe(client: JiraClient, key: str) -> Optional[dict]:
    try:
        return await client.get_issue(key)
    except Exception as e:
        log.warning("Fetch supplementare fallito per %s: %s", key, e)
        return None


async def _resolve_one_chain(
    direct_child: dict,
    parent_index: dict[str, list[dict]],
    client: JiraClient,
    additional_cache: dict[str, dict],
    *,
    depth: int = 1,
    max_depth: int = MAX_DEPTH,
) -> LeafInfo:
    """Discende ricorsivamente da un figlio diretto fino al leaf.

    Restituisce sempre una `LeafInfo` (anche in caso di limite profondità o WFR).
    """
    direct_child_key = direct_child.get("key", "")
    current = direct_child
    visited: set[str] = {direct_child_key}

    while True:
        fields = current.get("fields") or {}
        status_name = (fields.get("status") or {}).get("name", "")

        # Caso speciale: figlio diretto in WFR — la palla è al cliente, fermo a livello 2.
        if depth == 1 and is_waiting_for_reporter(status_name):
            return LeafInfo(
                direct_child_key=direct_child_key,
                leaf_key=direct_child_key,
                leaf_status=status_name,
                leaf_assignee=_assignee_name(current),
                attesa_cliente_helpdesk=True,
                leaf_issue=current,
            )

        # Se NON è waiting-for-son, current è il leaf.
        if not is_waiting_for_son(status_name):
            return LeafInfo(
                direct_child_key=direct_child_key,
                leaf_key=current.get("key", ""),
                leaf_status=status_name,
                leaf_assignee=_assignee_name(current),
                leaf_issue=current,
            )

        # current è waiting-for-son → cerca il prossimo nodo waiting tra i figli.
        if depth >= max_depth:
            return LeafInfo(
                direct_child_key=direct_child_key,
                leaf_key=direct_child_key,  # fallback al ticket di partenza
                leaf_status=status_name,
                leaf_assignee=_assignee_name(direct_child),
                chain_too_deep=True,
                leaf_issue=direct_child,
            )

        children = parent_index.get(current.get("key", ""), [])
        waiting_children = _filter_waiting_active(children)

        if not waiting_children:
            # Nessun figlio waiting dentro il dataset: prova fetch supplementare?
            # Per ora, se il dataset locale non li ha, consideriamo current il leaf.
            return LeafInfo(
                direct_child_key=direct_child_key,
                leaf_key=current.get("key", ""),
                leaf_status=status_name,
                leaf_assignee=_assignee_name(current),
                leaf_issue=current,
            )

        # Profondità ≥2: prendiamo il primo (PRD non duplica oltre il livello 1).
        next_node = waiting_children[0]
        next_key = next_node.get("key", "")

        # Loop guard
        if next_key in visited:
            log.warning("Catena ciclica rilevata su %s, stop al nodo corrente", next_key)
            return LeafInfo(
                direct_child_key=direct_child_key,
                leaf_key=current.get("key", ""),
                leaf_status=status_name,
                leaf_assignee=_assignee_name(current),
                leaf_issue=current,
            )
        visited.add(next_key)

        # Se il prossimo nodo è in cache come "shallow" (mancano fields), arricchisci.
        if not (next_node.get("fields") or {}).get("status"):
            full = additional_cache.get(next_key) or await _fetch_issue_safe(client, next_key)
            if full is not None:
                additional_cache[next_key] = full
                next_node = full

        current = next_node
        depth += 1


async def resolve_for_tc(
    tc_issue: dict,
    parent_index: dict[str, list[dict]],
    client: JiraClient,
    *,
    max_depth: int = MAX_DEPTH,
) -> list[LeafInfo]:
    """Risolve i leaf per un singolo TC. Lista vuota se non si applica.

    - Se il TC NON è in waiting-for-son → []
    - Altrimenti restituisce una LeafInfo per ogni figlio in stato waiting (Q&A §4.20).
    """
    fields = tc_issue.get("fields") or {}
    status_name = (fields.get("status") or {}).get("name", "")
    if not is_waiting_for_son(status_name):
        return []

    children = parent_index.get(tc_issue.get("key", ""), [])
    waiting_children = _filter_waiting_active(children)
    if not waiting_children:
        return []

    additional_cache: dict[str, dict] = {}
    leaves = []
    for child in waiting_children:
        info = await _resolve_one_chain(
            child, parent_index, client, additional_cache, max_depth=max_depth
        )
        leaves.append(info)
    return leaves


def _assignee_name(issue: dict) -> Optional[str]:
    fields = issue.get("fields") or {}
    assignee = fields.get("assignee") or {}
    return assignee.get("displayName") if assignee else None

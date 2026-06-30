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

import os

from .jira_client import JiraClient
from .transform import (
    _build_url,
    is_active_status,
    is_waiting_for_reporter,
    is_waiting_for_son,
    status_family,
)

_FIELD_REPARTO = os.getenv("JIRA_FIELD_REPARTO", "")

log = logging.getLogger("csb.leaf")

MAX_DEPTH = 10


@dataclass
class LeafInfo:
    """Dati pronti da iniettare nella riga della tabella per un singolo leaf."""

    direct_child_key: str
    leaf_key: str
    leaf_status: str
    leaf_assignee: Optional[str]
    leaf_reparto: Optional[str] = None
    chain_too_deep: bool = False
    attesa_cliente_helpdesk: bool = False  # figlio diretto in Waiting for reporter
    leaf_issue: Optional[dict] = None  # issue Jira completa del leaf (per calcolo gg fermo)

    def to_dict(self) -> dict:
        cat_key = ""
        if self.leaf_issue:
            cat_key = (
                ((self.leaf_issue.get("fields") or {}).get("status") or {})
                .get("statusCategory", {})
                .get("key", "")
            )
        return {
            "directChildKey": self.direct_child_key,
            "directChildUrl": _build_url(self.direct_child_key),
            "leafKey": self.leaf_key,
            "leafUrl": _build_url(self.leaf_key),
            "leafStatus": self.leaf_status,
            "leafStatusFamily": status_family(self.leaf_status, cat_key),
            "leafAssignee": self.leaf_assignee,
            "leafReparto": self.leaf_reparto,
            "chainTooDeep": self.chain_too_deep,
            "attesaClienteHelpdesk": self.attesa_cliente_helpdesk,
        }


# ============================================================
#  Helper: estrazione children
# ============================================================

def _children_keys_from_issue(issue: dict) -> list[str]:
    """Estrae i KEY dei PARENT di una issue (non dei figli — il nome storico
    è fuorviante).

    L'invertimento avviene in ``build_parent_index``: per ogni issue I, leggiamo
    chi è il SUO parent → l'indice diventa ``parent_key → [child_issue,...]``.

    Su Jira Service Management Cloud (verificato 2026-04-28) la relazione
    TC ↔ HDX è espressa via ``issuelinks`` di tipo "Parent":
        - ``inward  = "Figlio DI"`` → ``inwardIssue``  è il PADRE dell'issue
        - ``outward = "Padre DI"``  → ``outwardIssue`` è il FIGLIO dell'issue

    Per ricavare i PARENT dell'issue corrente leggiamo SOLO ``inwardIssue``;
    leggere anche outward causerebbe un'inversione sistematica della relazione
    (TC-X verrebbe contato come child di HDX-Y invece che parent).

    Manteniamo come prima fonte ``fields.parent`` per i progetti che usano il
    campo nativo (es. epic→story).
    """
    fields = issue.get("fields") or {}
    parents: list[str] = []
    parent = fields.get("parent")
    if isinstance(parent, dict) and parent.get("key"):
        parents.append(parent["key"])
    for link in fields.get("issuelinks") or []:
        if (link.get("type") or {}).get("name", "").lower() != "parent":
            continue
        target = link.get("inwardIssue") or {}
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


def _filter_active_children(children: list[dict]) -> list[dict]:
    """Solo figli ancora attivi (statusCategory ≠ Done) — qualunque stato.

    Il "leaf" della filiera è infatti il primo figlio NON in WFS, che può essere
    perfettamente in lavorazione ("Aperto", "In Progress", ecc.). Filtrare solo
    i waiting nasconde il leaf vero quando il TC è in WFS ma il figlio è già
    in carico a uno sviluppatore.
    """
    out = []
    for c in children:
        fields = c.get("fields") or {}
        status_cat = ((fields.get("status") or {}).get("statusCategory") or {}).get("key", "")
        if is_active_status(status_cat):
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
                leaf_reparto=_reparto_value(current),
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
                leaf_reparto=_reparto_value(current),
                leaf_issue=current,
            )

        # current è waiting-for-son → cerca il prossimo nodo waiting tra i figli.
        if depth >= max_depth:
            return LeafInfo(
                direct_child_key=direct_child_key,
                leaf_key=direct_child_key,  # fallback al ticket di partenza
                leaf_status=status_name,
                leaf_assignee=_assignee_name(direct_child),
                leaf_reparto=_reparto_value(direct_child),
                chain_too_deep=True,
                leaf_issue=direct_child,
            )

        children = parent_index.get(current.get("key", ""), [])
        active_children = _filter_active_children(children)

        if not active_children:
            # Nessun figlio attivo dentro il dataset: current è il leaf.
            return LeafInfo(
                direct_child_key=direct_child_key,
                leaf_key=current.get("key", ""),
                leaf_status=status_name,
                leaf_assignee=_assignee_name(current),
                leaf_reparto=_reparto_value(current),
                leaf_issue=current,
            )

        # Profondità ≥2: prendiamo il primo figlio attivo (PRD non duplica oltre L1).
        # Privilegiamo i figli in stato waiting (catena di stallo); se non ce ne sono,
        # il primo figlio attivo va comunque bene perché lì la catena termina.
        waiting_children = [
            c for c in active_children
            if _is_waiting_status(((c.get("fields") or {}).get("status") or {}).get("name", ""))
        ]
        next_node = waiting_children[0] if waiting_children else active_children[0]
        next_key = next_node.get("key", "")

        # Loop guard
        if next_key in visited:
            log.warning("Catena ciclica rilevata su %s, stop al nodo corrente", next_key)
            return LeafInfo(
                direct_child_key=direct_child_key,
                leaf_key=current.get("key", ""),
                leaf_status=status_name,
                leaf_assignee=_assignee_name(current),
                leaf_reparto=_reparto_value(current),
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

    - Se il TC non ha figli attivi → []
    - Se ha ≥2 figli ATTIVI in stato waiting → una LeafInfo per ogni figlio waiting
      (Q&A §4.20: duplicazione a livello TC).
    - Altrimenti → una sola LeafInfo, partendo dal primo figlio attivo.

    Nota: la risoluzione viene eseguita anche per TC che NON sono in
    Waiting for son (es. "Replied from reporter") perché la catena figli
    aperta è informazione utile nella sezione TC Sollecitati.
    """
    children = parent_index.get(tc_issue.get("key", ""), [])
    active_children = _filter_active_children(children)
    if not active_children:
        return []

    waiting_children = [
        c for c in active_children
        if _is_waiting_status(((c.get("fields") or {}).get("status") or {}).get("name", ""))
    ]
    targets = waiting_children if len(waiting_children) >= 2 else active_children[:1]

    additional_cache: dict[str, dict] = {}
    leaves = []
    for child in targets:
        info = await _resolve_one_chain(
            child, parent_index, client, additional_cache, max_depth=max_depth
        )
        leaves.append(info)
    return leaves


def _assignee_name(issue: dict) -> Optional[str]:
    fields = issue.get("fields") or {}
    assignee = fields.get("assignee") or {}
    return assignee.get("displayName") if assignee else None


def _reparto_value(issue: dict) -> Optional[str]:
    if not _FIELD_REPARTO:
        return None
    fields = issue.get("fields") or {}
    val = fields.get(_FIELD_REPARTO)
    if isinstance(val, dict):
        return val.get("value") or None
    return str(val) if val else None

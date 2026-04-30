"""Storage e logica per la sezione "TC Sollecitati".

Lista curata manualmente di TC che richiedono pressione esplicita verso uno
sviluppatore o una funzione interna. I ticket vengono aggiunti/modificati/rimossi
solo dagli operatori della board (admin/user). I dati sono persistenti sul volume
Railway in ``data/solleciti.json``.

Modello dati (per ogni TC):

    {
        "key": "TC-107711",
        "destinatario": {"nome": "...", "reparto": "..."},
        "richiedente":  {"nome": "...", "reparto": "..."},
        "ultimaDataSollecito": "2026-04-26T10:15:00+02:00",
        "richiestaEvasione":  "2026-05-05" | null | "data_richiesta",
        "createdAt":   "...",
        "createdBy":   "<username>",
        "history": [
            {
                "ts": "2026-04-22T10:00:00+02:00",
                "type": "create" | "sollecito" | "cambio_destinatario"
                       | "cambio_richiedente" | "cambio_evasione",
                "user": "<username>",
                # type-specific fields:
                #   create:           destinatario, richiedente, evasione
                #   sollecito:        destinatario (snapshot al momento)
                #   cambio_*:         from, to
            },
            ...
        ]
    }

Il numero di solleciti = ``count(history where type in {create, sollecito})``.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DATA_DIR = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "data"))
SOLLECITI_FILE = DATA_DIR / "solleciti.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

VALID_EVENT_TYPES = {
    "create",
    "sollecito",
    "cambio_destinatario",
    "cambio_richiedente",
    "cambio_evasione",
}

VALID_EVASIONE_SPECIAL = {"data_richiesta"}

KEY_RE = re.compile(r"^[A-Z]+-\d+$")


# ============================================================
#  Storage
# ============================================================

def load_solleciti() -> dict:
    """Mappa ``key → record``. Dict vuoto se file inesistente o corrotto."""
    try:
        if SOLLECITI_FILE.exists():
            return json.loads(SOLLECITI_FILE.read_text("utf-8"))
    except Exception:
        pass
    return {}


def _atomic_write(payload: dict) -> None:
    tmp = str(SOLLECITI_FILE) + ".tmp"
    Path(tmp).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(tmp, SOLLECITI_FILE)


# ============================================================
#  Helpers
# ============================================================

def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _normalize_evasione(value) -> Optional[str]:
    """Restituisce uno dei tre valori validi: una data ISO, ``"data_richiesta"`` o ``None``."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        if v in VALID_EVASIONE_SPECIAL:
            return v
        # Validazione data ISO YYYY-MM-DD
        try:
            datetime.fromisoformat(v).date()
            return v[:10]  # accetta sia "YYYY-MM-DD" che "YYYY-MM-DDTHH:MM:SS"
        except ValueError:
            pass
    raise ValueError(
        f"evasione deve essere una data ISO YYYY-MM-DD, vuoto, oppure 'data_richiesta'"
    )


def _normalize_persona(value) -> dict:
    """Verifica che il dict {nome, reparto} sia valorizzato."""
    if not isinstance(value, dict):
        raise ValueError("persona deve essere un oggetto {nome, reparto}")
    nome = (value.get("nome") or "").strip()
    reparto = (value.get("reparto") or "").strip()
    if not nome:
        raise ValueError("persona.nome obbligatorio")
    return {"nome": nome, "reparto": reparto}


def _validate_key(key: str) -> str:
    if not isinstance(key, str) or not KEY_RE.match(key):
        raise ValueError(f"key non valida: '{key}' (atteso pattern PROJECT-NNN)")
    return key


def _count_solleciti(record: dict) -> int:
    return sum(
        1 for ev in (record.get("history") or [])
        if ev.get("type") in {"create", "sollecito"}
    )


def _decorate(record: dict) -> dict:
    """Aggiunge campi calcolati (es. ``numSolleciti``) prima di servire al client."""
    out = dict(record)
    out["numSolleciti"] = _count_solleciti(record)
    return out


def list_all() -> list[dict]:
    return [_decorate(r) for r in load_solleciti().values()]


def get(key: str) -> Optional[dict]:
    rec = load_solleciti().get(key)
    return _decorate(rec) if rec else None


# ============================================================
#  Mutazioni
# ============================================================

def create(
    *,
    key: str,
    destinatario: dict,
    richiedente: dict,
    evasione,
    data_sollecito: Optional[str],
    user: str,
) -> dict:
    """Crea un nuovo record con un primo evento ``create`` (o ``create`` retroattivo
    se ``data_sollecito`` è una data passata).
    """
    key = _validate_key(key)
    dest = _normalize_persona(destinatario)
    rich = _normalize_persona(richiedente)
    evas = _normalize_evasione(evasione)
    ts_create = _resolve_ts(data_sollecito)

    db = load_solleciti()
    if key in db:
        raise ValueError(f"{key} è già nella lista solleciti")
    record = {
        "key": key,
        "destinatario": dest,
        "richiedente": rich,
        "ultimaDataSollecito": ts_create,
        "richiestaEvasione": evas,
        "createdAt": ts_create,
        "createdBy": user,
        "history": [
            {
                "ts": ts_create,
                "type": "create",
                "user": user,
                "destinatario": dest,
                "richiedente": rich,
                "evasione": evas,
            }
        ],
    }
    db[key] = record
    _atomic_write(db)
    return _decorate(record)


def add_sollecito(
    key: str, *, data_sollecito: Optional[str] = None, user: str
) -> dict:
    """+1 sollecito: aggiunge un evento ``sollecito`` alla history.

    Se ``data_sollecito`` è ``None``, usa oggi (uso pulsante ``+`` veloce).
    Aggiorna ``ultimaDataSollecito``.
    """
    key = _validate_key(key)
    db = load_solleciti()
    record = db.get(key)
    if not record:
        raise KeyError(f"{key} non è nella lista solleciti")
    ts = _resolve_ts(data_sollecito)
    record["history"].append({
        "ts": ts,
        "type": "sollecito",
        "user": user,
        "destinatario": dict(record["destinatario"]),
    })
    record["ultimaDataSollecito"] = ts
    db[key] = record
    _atomic_write(db)
    return _decorate(record)


def patch(
    key: str,
    *,
    destinatario=None,
    richiedente=None,
    evasione=...,  # sentinel: ... = non toccare; None | "data_richiesta" | "YYYY-MM-DD" = imposta
    user: str,
) -> dict:
    """Modifica selettiva. Per ogni campo cambiato genera un evento history
    dedicato (``cambio_destinatario`` / ``cambio_richiedente`` / ``cambio_evasione``).
    """
    key = _validate_key(key)
    db = load_solleciti()
    record = db.get(key)
    if not record:
        raise KeyError(f"{key} non è nella lista solleciti")

    now = _now_iso()
    if destinatario is not None:
        new_dest = _normalize_persona(destinatario)
        if new_dest != record["destinatario"]:
            record["history"].append({
                "ts": now,
                "type": "cambio_destinatario",
                "user": user,
                "from": dict(record["destinatario"]),
                "to": new_dest,
            })
            record["destinatario"] = new_dest
    if richiedente is not None:
        new_rich = _normalize_persona(richiedente)
        if new_rich != record["richiedente"]:
            record["history"].append({
                "ts": now,
                "type": "cambio_richiedente",
                "user": user,
                "from": dict(record["richiedente"]),
                "to": new_rich,
            })
            record["richiedente"] = new_rich
    if evasione is not ...:
        new_evas = _normalize_evasione(evasione)
        if new_evas != record.get("richiestaEvasione"):
            record["history"].append({
                "ts": now,
                "type": "cambio_evasione",
                "user": user,
                "from": record.get("richiestaEvasione"),
                "to": new_evas,
            })
            record["richiestaEvasione"] = new_evas
    db[key] = record
    _atomic_write(db)
    return _decorate(record)


def delete(key: str) -> bool:
    key = _validate_key(key)
    db = load_solleciti()
    if key not in db:
        return False
    db.pop(key, None)
    _atomic_write(db)
    return True


# ============================================================
#  Helpers timestamp
# ============================================================

def _resolve_ts(data_sollecito: Optional[str]) -> str:
    """Se ``data_sollecito`` è una stringa ``YYYY-MM-DD`` la converte in ISO datetime
    a inizio giornata (UTC); altrimenti usa adesso."""
    if not data_sollecito:
        return _now_iso()
    s = data_sollecito.strip()
    if not s:
        return _now_iso()
    try:
        d = datetime.fromisoformat(s)
        # Se solo data (senza ora), ancora a 00:00 UTC
        if len(s) == 10:
            d = d.replace(tzinfo=timezone.utc)
        elif d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone().isoformat(timespec="seconds")
    except ValueError:
        raise ValueError(f"data_sollecito non valida: '{data_sollecito}' (atteso YYYY-MM-DD)")

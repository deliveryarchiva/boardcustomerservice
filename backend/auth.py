"""Standard auth Archiva: sha256, sessioni UUID in-memory, users.json su volume Railway.

Ruoli applicativi (PRD §4.3):
- admin     → gestione utenti + accesso completo dashboard
- user      → accesso completo dashboard in sola lettura
- ospite    → vista ridotta (solo 4 KPI aggregati, no dettaglio ticket nominativo)
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Optional

from fastapi import Depends, Header, HTTPException

# Su Windows Path.rename() fallisce se la destinazione esiste; os.replace() è atomic e cross-platform.

DATA_DIR = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "data"))
USERS_FILE = DATA_DIR / "users.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

VALID_ROLES = {"admin", "user", "ospite"}

sessions: dict[str, dict] = {}


def hash_password(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()


def verify_password(plain: str, hashed: str) -> bool:
    return hash_password(plain) == hashed


def load_users() -> dict:
    try:
        if USERS_FILE.exists():
            return json.loads(USERS_FILE.read_text("utf-8"))
    except Exception:
        pass
    return {}


def _atomic_write(users: dict) -> None:
    tmp = str(USERS_FILE) + ".tmp"
    Path(tmp).write_text(
        json.dumps(users, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(tmp, USERS_FILE)


def save_user(user: dict) -> None:
    users = load_users()
    users[user["username"].lower()] = user
    _atomic_write(users)


def delete_user(username: str) -> None:
    users = load_users()
    users.pop(username.lower(), None)
    _atomic_write(users)


def seed_users() -> None:
    """Seed iniziale utenti standard Archiva. Idempotente *additive*: aggiunge
    solo i seed mancanti, non sovrascrive utenti esistenti (così le password
    cambiate dagli utenti restano valide al riavvio).
    """
    pwd_default = hash_password(os.getenv("DEFAULT_PASSWORD", "archiva2026"))
    seed: list[dict] = [
        {
            "nome": "Marco Pastore",
            "username": "marco.pastore",
            "ruolo": "admin",
            "role": "Head of Project Delivery",
            "password": pwd_default,
        },
        {
            "nome": "Paolo Gandini",
            "username": "paolo.gandini",
            "ruolo": "admin",
            "role": "Delivery & Customer Service Director",
            "password": pwd_default,
        },
        {
            "nome": "Chiara Pettenuzzo",
            "username": "chiara.pettenuzzo",
            "ruolo": "admin",
            "role": "Service Delivery Manager",
            "password": pwd_default,
        },
        {
            "nome": "Michael Seren",
            "username": "michael.seren",
            "ruolo": "admin",
            "role": "",
            "password": hash_password("D3fault!"),
        },
    ]
    existing = load_users()
    for u in seed:
        if u["username"].lower() not in existing:
            save_user(u)


def create_session(user: dict) -> str:
    token = str(uuid.uuid4())
    sessions[token] = {
        "username": user["username"],
        "nome": user["nome"],
        "ruolo": user["ruolo"],
        "role": user.get("role", ""),
    }
    return token


def destroy_session(token: str) -> None:
    sessions.pop(token, None)


def get_current_user(authorization: Optional[str] = Header(default=None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Non autenticato")
    token = authorization.removeprefix("Bearer ").strip()
    user = sessions.get(token)
    if not user:
        raise HTTPException(status_code=401, detail="Sessione scaduta")
    return user


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("ruolo") != "admin":
        raise HTTPException(
            status_code=403, detail="Accesso riservato agli amministratori"
        )
    return user


def require_full_access(user: dict = Depends(get_current_user)) -> dict:
    """admin + user vedono tutto; ospite vede solo KPI aggregati."""
    if user.get("ruolo") not in {"admin", "user"}:
        raise HTTPException(
            status_code=403, detail="Accesso riservato agli utenti autenticati"
        )
    return user

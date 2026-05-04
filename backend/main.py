"""Customer Service Board — entrypoint FastAPI.

Fasi implementate:
- M1: auth standard Archiva, gestione utenti, template login/admin/index.

In arrivo (M2+): integrazione Jira read-only, polling 10 min, KPI, tabelle.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

# load_dotenv() viene chiamato in backend/__init__.py PRIMA di qualunque import
# che legga env var a tempo di import (jira_client, transform, snapshot).

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth import (
    VALID_ROLES,
    create_session,
    delete_user,
    destroy_session,
    get_current_user,
    hash_password,
    load_organigramma,
    load_users,
    require_admin,
    require_full_access,
    save_user,
    seed_users,
    verify_password,
)
from . import solleciti as solleciti_store
from .cache import cache
from .jira_client import JiraClient, is_demo_mode
from .snapshot import build_snapshot

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Singleton client Jira riutilizzato per tutto il ciclo di vita dell'app.
jira_client = JiraClient()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    seed_users()
    yield
    # Shutdown
    await jira_client.close()


app = FastAPI(title="Customer Service Board", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ============================================================
#  Pagine HTML
# ============================================================

@app.get("/login")
def page_login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/")
def page_index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/admin")
def page_admin():
    # La gestione utenze è ora una sezione integrata nella board principale.
    # Manteniamo la route per compatibilità con vecchi bookmark.
    return RedirectResponse(url="/#utenze", status_code=301)


# ============================================================
#  Auth API
# ============================================================

@app.post("/api/auth/login")
async def api_login(request: Request):
    body = await request.json()
    username = (body.get("username") or "").strip().lower()
    password = body.get("password") or ""
    if not username or not password:
        raise HTTPException(400, "Username e password obbligatori")
    users = load_users()
    user = users.get(username)
    if not user or not verify_password(password, user["password"]):
        raise HTTPException(401, "Credenziali non valide")
    token = create_session(user)
    return {
        "ok": True,
        "token": token,
        "user": {
            "username": user["username"],
            "nome": user["nome"],
            "ruolo": user["ruolo"],
            "role": user.get("role", ""),
        },
    }


@app.post("/api/auth/logout")
async def api_logout(request: Request):
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        destroy_session(auth.removeprefix("Bearer ").strip())
    return {"ok": True}


@app.get("/api/auth/me")
def api_me(user: dict = Depends(get_current_user)):
    return {"ok": True, "user": user}


@app.post("/api/auth/change-password")
async def api_change_password(
    request: Request, user: dict = Depends(get_current_user)
):
    body = await request.json()
    current = body.get("currentPassword") or ""
    new = body.get("newPassword") or ""
    if len(new) < 8:
        raise HTTPException(400, "La nuova password deve avere almeno 8 caratteri")
    users = load_users()
    db_user = users.get(user["username"].lower())
    if not db_user or not verify_password(current, db_user["password"]):
        raise HTTPException(401, "Password attuale non corretta")
    db_user["password"] = hash_password(new)
    save_user(db_user)
    return {"ok": True}


# ============================================================
#  Admin API — gestione utenti (solo ruolo admin)
# ============================================================

def _public_user(u: dict) -> dict:
    return {
        "username": u["username"],
        "nome": u["nome"],
        "ruolo": u["ruolo"],
        "role": u.get("role", ""),
    }


@app.get("/api/admin/users")
def api_list_users(_: dict = Depends(require_admin)):
    users = load_users()
    return {"ok": True, "users": [_public_user(u) for u in users.values()]}


@app.post("/api/admin/users")
async def api_create_user(request: Request, _: dict = Depends(require_admin)):
    body = await request.json()
    username = (body.get("username") or "").strip().lower()
    nome = (body.get("nome") or "").strip()
    ruolo = (body.get("ruolo") or "user").strip().lower()
    role = (body.get("role") or "").strip()
    password = body.get("password") or ""
    if not username or not nome or not password:
        raise HTTPException(400, "username, nome e password sono obbligatori")
    if ruolo not in VALID_ROLES:
        raise HTTPException(400, f"Ruolo non valido. Validi: {sorted(VALID_ROLES)}")
    users = load_users()
    if username in users:
        raise HTTPException(409, "Utente già esistente")
    user = {
        "username": username,
        "nome": nome,
        "ruolo": ruolo,
        "role": role,
        "password": hash_password(password),
    }
    save_user(user)
    return {"ok": True, "user": _public_user(user)}


@app.put("/api/admin/users/{username}")
async def api_update_user(
    username: str, request: Request, _: dict = Depends(require_admin)
):
    body = await request.json()
    users = load_users()
    user = users.get(username.lower())
    if not user:
        raise HTTPException(404, "Utente non trovato")
    if "nome" in body and body["nome"]:
        user["nome"] = body["nome"].strip()
    if "ruolo" in body and body["ruolo"]:
        new_role = body["ruolo"].strip().lower()
        if new_role not in VALID_ROLES:
            raise HTTPException(
                400, f"Ruolo non valido. Validi: {sorted(VALID_ROLES)}"
            )
        user["ruolo"] = new_role
    if "role" in body:
        user["role"] = (body["role"] or "").strip()
    if "password" in body and body["password"]:
        if len(body["password"]) < 8:
            raise HTTPException(
                400, "La nuova password deve avere almeno 8 caratteri"
            )
        user["password"] = hash_password(body["password"])
    save_user(user)
    return {"ok": True, "user": _public_user(user)}


@app.delete("/api/admin/users/{username}")
def api_delete_user(username: str, current: dict = Depends(require_admin)):
    if username.lower() == current["username"].lower():
        raise HTTPException(400, "Non puoi eliminare il tuo stesso utente")
    users = load_users()
    if username.lower() not in users:
        raise HTTPException(404, "Utente non trovato")
    delete_user(username)
    return {"ok": True}


# ============================================================
#  Health
# ============================================================

@app.get("/api/health")
def health():
    return {"ok": True, "version": app.version, "demo": is_demo_mode()}


@app.get("/api/organigramma")
def api_organigramma(_: dict = Depends(get_current_user)):
    """Lista pubblica dell'organigramma Archiva — usata dai dropdown del modale
    "Aggiungi sollecito" (destinatario, richiedente). Auth-protetta per evitare
    scraping anonimo, ma accessibile a tutti i ruoli loggati."""
    return {"ok": True, "organigramma": load_organigramma()}


# ============================================================
#  Solleciti — lettura aperta a admin/user, scrittura idem (no ospite)
# ============================================================

@app.get("/api/solleciti")
def api_list_solleciti(_: dict = Depends(require_full_access)):
    return {"ok": True, "solleciti": solleciti_store.list_all()}


@app.post("/api/solleciti")
async def api_create_sollecito(
    request: Request, user: dict = Depends(require_full_access)
):
    body = await request.json()
    try:
        record = solleciti_store.create(
            key=(body.get("key") or "").strip().upper(),
            destinatario=body.get("destinatario") or {},
            richiedente=body.get("richiedente") or {},
            evasione=body.get("evasione"),
            data_sollecito=body.get("dataSollecito"),
            user=user["username"],
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "sollecito": record}


@app.post("/api/solleciti/{key}/sollecito")
async def api_add_sollecito(
    key: str, request: Request, user: dict = Depends(require_full_access)
):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    try:
        record = solleciti_store.add_sollecito(
            key.upper(),
            data_sollecito=body.get("dataSollecito") if body else None,
            user=user["username"],
        )
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "sollecito": record}


@app.patch("/api/solleciti/{key}")
async def api_patch_sollecito(
    key: str, request: Request, user: dict = Depends(require_full_access)
):
    body = await request.json()
    kwargs = {"user": user["username"]}
    if "destinatario" in body:
        kwargs["destinatario"] = body["destinatario"]
    if "richiedente" in body:
        kwargs["richiedente"] = body["richiedente"]
    if "evasione" in body:
        kwargs["evasione"] = body["evasione"]
    try:
        record = solleciti_store.patch(key.upper(), **kwargs)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "sollecito": record}


@app.delete("/api/solleciti/{key}")
def api_delete_sollecito(key: str, _: dict = Depends(require_full_access)):
    ok = solleciti_store.delete(key.upper())
    if not ok:
        raise HTTPException(404, f"{key} non trovato")
    return {"ok": True}


# ============================================================
#  Snapshot — KPI + righe TC/HDX dalla cache (PRD §4.1, §4.2.1)
# ============================================================

async def _fetch_for_cache() -> dict:
    return await build_snapshot(jira_client)


@app.get("/api/snapshot")
async def api_snapshot(user: dict = Depends(get_current_user)):
    snapshot = await cache.get_or_fetch(_fetch_for_cache)
    if user.get("ruolo") == "ospite":
        # Vista ospite (PRD §4.3, Q&A §6.29): solo KPI aggregati, no dettaglio.
        return {
            "ok": True,
            "snapshot": {
                **{
                    k: v
                    for k, v in snapshot.items()
                    if k not in ("rows", "rowsAll")
                },
                "rows": [],
                "rowsAll": [],
            },
        }
    return {"ok": True, "snapshot": snapshot}


# ============================================================
#  Error handler uniforme JSON per le API
# ============================================================

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=exc.status_code, content={"ok": False, "detail": exc.detail}
        )
    raise exc

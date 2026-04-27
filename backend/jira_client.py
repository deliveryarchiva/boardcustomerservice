"""Client read-only Jira Service Management.

Comportamento (PRD §4.1):
- Solo GET (l'app non scrive mai su Jira).
- Paginazione obbligatoria su /search (startAt/maxResults).
- Backoff esponenziale su 429 (Atlassian rate limit) + log.
- Auth Basic con email + API token.

Quando JIRA_API_TOKEN non è valorizzato il client va in modalità "demo" (vedi
demo_data.py) — utile per sviluppo UI prima dell'accesso reale a JSM.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from typing import Any, Optional

import httpx

log = logging.getLogger("csb.jira")

JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "").rstrip("/")
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")

# Pagina max consigliata da Atlassian per /search; oltre torna 400.
DEFAULT_PAGE_SIZE = 100
MAX_PAGES = 100  # safety guard: max 100*100 = 10_000 issue per query


def is_demo_mode() -> bool:
    return not (JIRA_BASE_URL and JIRA_EMAIL and JIRA_API_TOKEN)


def _basic_auth_header() -> str:
    raw = f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


class JiraRateLimited(Exception):
    """Atlassian 429 dopo tutti i retry — il chiamante decide se servire stale cache."""


class JiraError(Exception):
    pass


class JiraClient:
    """HTTP client minimale, asincrono. Una sola istanza riusabile per app."""

    def __init__(self, timeout_s: float = 30.0):
        self.timeout = timeout_s
        self._client: Optional[httpx.AsyncClient] = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=JIRA_BASE_URL,
                timeout=self.timeout,
                headers={
                    "Authorization": _basic_auth_header(),
                    "Accept": "application/json",
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(
        self, method: str, path: str, *, params: Optional[dict] = None
    ) -> dict:
        """GET con backoff esponenziale su 429. Max 5 tentativi (30s, 60s, 120s, 240s)."""
        client = await self._ensure_client()
        delays = [30, 60, 120, 240]
        last_exc: Optional[Exception] = None
        for attempt in range(len(delays) + 1):
            try:
                r = await client.request(method, path, params=params)
            except httpx.HTTPError as e:
                last_exc = e
                log.warning("Jira HTTP error on %s %s: %s", method, path, e)
                if attempt < len(delays):
                    await asyncio.sleep(delays[attempt])
                    continue
                raise JiraError(f"Network error verso Jira: {e}") from e
            if r.status_code == 429:
                if attempt < len(delays):
                    delay = delays[attempt]
                    log.warning(
                        "Jira 429 (rate limit) — backoff %ds (tentativo %d/%d)",
                        delay,
                        attempt + 1,
                        len(delays) + 1,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise JiraRateLimited("Atlassian rate limit dopo retry")
            if r.status_code in (401, 403):
                raise JiraError(
                    f"Auth Jira fallita ({r.status_code}). Verifica JIRA_EMAIL e JIRA_API_TOKEN."
                )
            if r.status_code >= 400:
                raise JiraError(f"Jira {method} {path} → {r.status_code}: {r.text[:300]}")
            return r.json()
        if last_exc:
            raise JiraError(str(last_exc))
        raise JiraError("Esaurito numero di tentativi senza risposta valida")

    # ------------------------------------------------------------
    #  Endpoint usati
    # ------------------------------------------------------------

    async def search_issues(
        self,
        jql: str,
        fields: list[str],
        expand: Optional[list[str]] = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> list[dict]:
        """Ritorna TUTTE le issue paginando con la nuova API ``/rest/api/3/search/jql``.

        Atlassian ha deprecato e rimosso ``/rest/api/3/search`` (CHANGE-2046,
        ottobre 2024). La nuova API è cursor-based: niente più ``startAt`` e
        niente più ``total``; si itera passando ``nextPageToken`` finché non
        arriva ``isLast=true`` o un risultato vuoto.
        """
        all_issues: list[dict] = []
        next_page_token: Optional[str] = None
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {
                "jql": jql,
                "maxResults": page_size,
                "fields": ",".join(fields),
            }
            if expand:
                params["expand"] = ",".join(expand)
            if next_page_token:
                params["nextPageToken"] = next_page_token
            data = await self._request("GET", "/rest/api/3/search/jql", params=params)
            issues = data.get("issues") or []
            all_issues.extend(issues)
            next_page_token = data.get("nextPageToken")
            if data.get("isLast", False) or not next_page_token or not issues:
                break
        else:
            log.warning(
                "search_issues: raggiunto MAX_PAGES (%d) per JQL: %s", MAX_PAGES, jql
            )
        return all_issues

    async def get_issue(
        self, key: str, *, expand: Optional[list[str]] = None
    ) -> dict:
        params: dict[str, Any] = {}
        if expand:
            params["expand"] = ",".join(expand)
        return await self._request(
            "GET", f"/rest/api/3/issue/{key}", params=params or None
        )

    async def get_comments(self, key: str) -> list[dict]:
        """Restituisce TUTTI i commenti paginando."""
        all_comments: list[dict] = []
        start_at = 0
        for _ in range(MAX_PAGES):
            data = await self._request(
                "GET",
                f"/rest/api/3/issue/{key}/comment",
                params={"startAt": start_at, "maxResults": 100},
            )
            comments = data.get("comments") or []
            all_comments.extend(comments)
            total = data.get("total", 0)
            start_at += len(comments)
            if not comments or start_at >= total:
                break
        return all_comments

    async def get_sla(self, issue_id_or_key: str) -> Optional[dict]:
        """Wrapper su /servicedeskapi/request/{id}/sla. Ritorna None se 404."""
        try:
            return await self._request(
                "GET", f"/rest/servicedeskapi/request/{issue_id_or_key}/sla"
            )
        except JiraError as e:
            log.debug("SLA non disponibile per %s: %s", issue_id_or_key, e)
            return None

# ToDoList — Customer Service Board

> Stato avanzamento implementazione, allineato a [`specs/prd.md`](specs/prd.md), [`specs/QeA.md`](specs/QeA.md) e [`mockup/index.html`](mockup/index.html).
>
> **Legenda stato**: `[ ]` da fare · `[~]` in corso · `[x]` fatto · `[!]` bloccato / da chiarire
>
> **Ultimo aggiornamento**: 2026-04-27 (Onboarding Jira reale completato 100%. Token aggiornato a Michael Seren con permessi Service Desk Agent → SLA attivi. Pipeline reale OK in 39s: 20 TC urgenti / 7020 TC nel periodo / 9 SLA breached visibili. **PRONTI AL DEPLOY RAILWAY.**)

---

## M0 — Setup repository e fondamenta progetto

- [x] Decisione stack definitiva: **FastAPI (Python 3.11) + Jinja2 + JS vanilla** (allineato pattern Archiva auth standard, no React/Vite) — PRD §6
- [x] Scaffolding repo: `backend/{auth.py, main.py, templates/, static/}`, `data/`, `requirements.txt`, `.env.example`, `README.md`
- [x] Configurazione `.gitignore` (Python, secrets, `data/`)
- [x] `Procfile` + `railway.json` (singola istanza, no scaling — PRD §5)
- [x] `runtime.txt` + `.python-version` per pin Python 3.11 (evita drift con 3.14 locale)
- [x] `.env.example` con tutte le variabili: `JIRA_*`, `JIRA_FIELD_TICKET_SPOCCATO`, `JIRA_FIELD_IN_ESCALATION`, `DEFAULT_PASSWORD`, `RAILWAY_VOLUME_MOUNT_PATH`, `POLL_INTERVAL_SECONDS`, blocklist bot, domini interni
- [x] `scripts/check_jira.py` — onboarding Jira: auth + custom field ID + link types + JQL test + SLA test (5 controlli in un comando)
- [x] Migrazione `on_event` deprecato → `lifespan` (FastAPI 0.115)
- [ ] Pipeline deploy Railway funzionante su branch principale (da fare al primo push — vedi runbook in README)

---

## M1 — Setup + Auth (stima 2 gg)

> Riuso integrale standard auth Archiva (DB locale, no SSO) — PRD §4.3

- [x] Import pattern auth standard Archiva (sha256 + sessioni UUID in-memory + `users.json` su volume Railway) — vedi `backend/auth.py`
- [x] Storage utenti: `data/users.json` (campi `username`, `nome`, `ruolo`, `role`, `password` sha256) — PRD §7
- [x] Endpoint login / logout / me / change-password (`backend/main.py`)
- [x] Sessione via header `Authorization: Bearer <uuid>` + token in `sessionStorage` (pattern Archiva — no cookie). N.B. lo standard Archiva usa Bearer token, NON cookie httpOnly come ipotizzato in PRD §5: ho mantenuto il pattern standard.
- [x] Admin panel standard Archiva per gestione utenti (`/admin` + `templates/admin.html` + endpoint `/api/admin/users`)
- [x] Ruoli applicativi: `admin`, `user`, `ospite` — validazione lato API, vista ospite via classe `body.role-ospite` lato UI
- [x] Pagina di login brandizzata Archiva (`templates/login.html`)
- [x] Seed utenti iniziali: Marco Pastore, Paolo Gandini, Chiara Pettenuzzo (admin)
- [x] Smoke test passati: login OK, login KO 401, /me 401 senza token, admin 403 a non-admin, change-password + re-login, validazione ruolo invalido, auto-eliminazione bloccata, creazione ospite OK
- [ ] Skeleton app deployato su Railway con login funzionante (da fare al primo push)

---

## M2 — Integrazione JSM read-only TC + HDX (stima 4 gg)

### 2.1 Client Jira

- [x] Client REST `JiraClient` (httpx async) con Basic auth (email + API token) — `backend/jira_client.py`
- [x] `search_issues(jql, fields)` con paginazione automatica (max 50 pagine × 100 = 5000 issue) — PRD §4.1
- [x] `get_issue(key, expand)` con expand opzionale (changelog in M3)
- [x] `get_comments(key)` paginato per il calcolo "ultima risposta cliente" (M3)
- [x] `get_sla(id)` su `/rest/servicedeskapi/request/{id}/sla` (M3 lo userà nel rendering)
- [x] Gestione 429 con backoff esponenziale (30/60/120/240s, max 5 tentativi) + log + 401/403 con messaggio leggibile

### 2.2 Custom field mapping

- [x] Lettura via env `JIRA_FIELD_TICKET_SPOCCATO`, `JIRA_FIELD_IN_ESCALATION` (vuoti → ignorati nella JQL)
- [x] Parser `_read_custom_yes_no` tollerante: gestisce `bool`, `"Yes"/"No"`, `{"value": "Yes"}`, list-of-options (Q&A §1.4 — il tipo reale va verificato sull'istanza)
- [ ] **Da fare in onboarding**: verificare ID + tipo effettivo dei due custom field e popolare le env (vedi "Punti aperti")
- [ ] **Da fare in onboarding**: verificare nome/ID esatto del link type Parent/Child JSM (M3.5 lo userà)

### 2.3 JQL e dataset

- [x] JQL urgent: `(project = TC OR project = HDX) AND created >= "{JIRA_DATE_FROM}" AND (priority = Highest OR "TICKET SPOCCATO" = Yes OR "IN ESCALATION" = Yes) ORDER BY created DESC` — `backend/snapshot.py:_build_or_jql`
- [x] JQL all-TC: `project = TC AND created >= "{JIRA_DATE_FROM}" ORDER BY created DESC` — necessario per KPI 2/3/4 e per tab "Tutti i TC"
- [x] Esclusione HDX orfani via `is_orphan_hdx()` (controlla `parent` field + fallback su `issuelinks`) — Q&A §1.1
- [x] Inclusione TC chiusi nel dataset all-TC; KPI 2/3/4 filtrano `isActive` — Q&A §1.5

### 2.4 Cache e polling

- [x] `SnapshotCache` in-memory con TTL configurabile via `POLL_INTERVAL_SECONDS` (default 600 = 10 min) — `backend/cache.py`
- [x] **Approccio lazy** (allineato a "primo utente attende il primo poll"): no scheduler attivo, fetch innescato dalla prima richiesta dopo TTL scaduto. Coerente con Q&A §7.35 e con la nota PRD "no fetch eager"
- [x] Concorrenza: `asyncio.Lock` con doppio check post-acquisizione → 8 fetch paralleli triggerano 1 solo fetch reale (verificato in smoke test)
- [x] Degrado 429: se il fetch fallisce ma esiste cache stale, viene servita con flag `degraded=true` e `next_retry_at` per evitare hammering
- [x] Endpoint `/api/snapshot` (auth-protetto) restituisce stesso snapshot per KPI e (in arrivo M4) tabella — PRD §4.2.1
- [x] Filtro vista ospite lato backend: rimuove `rows`/`rowsAll`, espone solo KPI aggregati — PRD §4.3

### 2.5 KPI base (4 KPI)

- [x] KPI 1: TC urgenti aperti — `(Highest OR Spoccato OR In Escalation) AND statusCategory ≠ Done` calcolato sul dataset urgent
- [x] KPI 2: TC in corso — tutti i TC del periodo con `isActive=true` (dataset all-TC)
- [x] KPI 3: TC Waiting for reporter — match case-insensitive EN+IT (mappa `STATUS_FAMILIES`) — `backend/transform.py`
- [x] KPI 4: TC Waiting for son — analogo per WFS / "In attesa del figlio"
- [x] In più: `tcTotaliPeriodo` (denominatore "su X totali" mostrato in UI)

### 2.6 UI di base

- [x] Header con logo Archiva, titolo app, chip utente loggato + dropdown (cambio password / pannello admin / logout)
- [x] Timestamp ultimo aggiornamento sempre visibile in header (formato `dd/mm/yyyy hh:mm:ss`) e nel footer — PRD §1
- [x] Countdown live "prossimo poll tra Xm Ys" basato su `nextRefreshSeconds` dal backend; al raggiungimento di 0 trigger automatico di `loadSnapshot()`
- [x] Banner di degrado: visibile durante `degraded=true` ("Dato non aggiornato da Xm Ys") + variante "Modalità DEMO" quando `JIRA_API_TOKEN` non è configurato
- [x] Render placeholder della tabella (key, cliente, summary, stato, condizioni, assegnatario, data) — sostituito in M4 con render completo (giorni s/risc., leaf, SLA)
- [x] Vista ospite via `body.role-ospite` → nasconde tabella e toolbar, mostra solo KPI

### Smoke test M2 superati
- [x] `/api/snapshot` 401 senza token, 200 con token
- [x] Admin riceve dati completi con `demo:true` e KPI corretti (8/8/1/4 dal mockup)
- [x] Ospite riceve solo KPI (`rows=[]`, `rowsAll=[]`)
- [x] Concorrenza: 8 fetch paralleli → 1 solo fetchedAtMs (lock funzionante)
- [x] TTL 600s, `nextRefreshSeconds` decresce correttamente

---

## M3 — Logica "Giorni senza Riscontro" + leaf ticket (stima 3 gg)

### 3.1 Calcolo ultima risposta cliente — PRD §4.2.4

- [x] Recupero commenti via `client.get_comments(key)` paginato per TC e per ogni leaf — `backend/comments.py`
- [x] `find_last_operator_response()`: filtra `jsdPublic=true`, esclude reporter, esclude blocklist (env `JIRA_BOT_BLOCKLIST`)
- [x] Reporter interno Archiva: già coperto dal filtro "author != reporter" — Q&A §3.13
- [x] `lastResponseToClient` = `comment.created` (mai `updated`) — Q&A §3.15
- [x] `lastResponseAuthor` = `comment.author.displayName`
- [x] Fetch parallelo commenti/SLA con `asyncio.Semaphore(JIRA_ENRICH_CONCURRENCY=5)` per non saturare rate limit

### 3.2 Calcolo giorni lavorativi

- [x] `business_days_between()` in `backend/business_days.py` — esclude sab/dom + festività italiane
- [x] Festività fisse italiane (10 voci) + Pasquetta calcolata via algoritmo gregoriano anonimo
- [x] Test passati: Pasqua 2026 = 5 aprile ✓, 25/04 sab+festa ✓, 06/04 Pasquetta ✓, conteggio multi-giorno con festa intermedia ✓
- [x] Convenzione: `start` esclusivo, `end` inclusivo (oggi rispetto a oggi → 0 gg)

### 3.3 Fallback su `updated`

- [x] Se nessun commento operatore valido → calcolo da `issue.updated` (Q&A §3.18)
- [x] Flag `daysFromFallback=true` propagato fino alla UI
- [x] Icona ⚠ con tooltip nel `days-pill` — `renderRow()` in `index.html`

### 3.4 Colorazione riga

- [x] `severity_class(days)` in `transform.py`: 0–2 verde / 3–7 arancione / ≥8 rosso
- [x] Classe `row-{green|orange|red}` applicata al `<tr>` → bordo sinistro colorato (CSS già nel template M2)
- [x] Verifica nel browser: 9 righe con severity attesa (3 green / 4 orange / 2 red)

### 3.5 Risoluzione catena Waiting for son → leaf — PRD §4.2.5

- [x] `build_parent_index(issues)` invertito sui campi `parent`/`issuelinks` — `backend/leaf.py`
- [x] `_resolve_one_chain()` ricorsivo iterativo: discende fino a primo nodo non WFS
- [x] Figli multipli a livello TC: snapshot.py duplica la row TC per ogni HDX waiting (Q&A §4.20) — verificato: TC-1024 produce 2 righe (HDX-3301, HDX-3302)
- [x] Profondità max 10 con `chain_too_deep` flag + badge "catena troppo profonda" (Q&A §4.21) — verificato su TC-1029
- [x] Loop guard con `visited` set anti-cicli
- [x] Caso WFR a livello 2: flag `attesa_cliente_helpdesk` + label "📨 Attesa Cliente / Helpdesk", no discesa oltre (Q&A §4.22) — verificato su TC-1011
- [x] Chiamata supplementare `client.get_issue()` se nodo intermedio non ha campi caricati
- [x] "Ngg fermo" sul leaf = `business_days_between(last_operator_response_to_leaf, now)` (Q&A §4.24) — verificato: HDX-3401=7gg, HDX-3301=4gg, HDX-3302=6gg

### 3.6 Colonna SLA

- [x] Parser `parse_sla(payload)` in `backend/sla.py` per `/rest/servicedeskapi/request/{id}/sla`
- [x] Stati: `ok` (>4h), `warn` (≤4h ma positivo), `breached` (negativo o flag), `none` (paused/no SLA)
- [x] Label con simboli `✓` / `⏱` / `⚠ Breached` + tempo umano (es. `1g 03h 12m`)
- [x] Render badge `.sla.{ok|warn|breached|none}` nella colonna 12
- [ ] Validazione su dataset reale (fallback su `duedate` se SLA non esposto su qualche request type) — vedi "Punti aperti"

### Smoke test M3 superati
- [x] business_days unit test: 6/6 casi
- [x] Snapshot demo: KPI 8/8/25/1/4 corretti, 9 rows con duplicato TC-1024, severity counter 3/4/2
- [x] Browser test (Claude Preview): login → dashboard → 9 righe renderizzate con tutti i casi (catena, attesa cliente, profondità, fallback ⚠, 4 stati SLA)
- [x] Vista ospite: 0 rows, KPI presenti

---

## M4 — Tabelle, filtri, ricerca, ordinamento (stima 2 gg)

### 4.1 Tab "Vista TC Attivi" — PRD §4.2.2

- [x] 12 colonne dinamiche da `COLUMNS_ACTIVE` in `index.html` — header generato da `renderTable()`
- [x] Cella Key con deep link `target="_blank"` e icona `↗`
- [x] Cella Cliente: nome + codice cliente
- [x] Cella Summary: clamp 2 righe con ellipsis + tooltip full-text
- [x] Badge Stato per famiglia (`s-progress` / `s-wfr` / `s-wfson` / `s-open` / `s-resolved`)
- [x] Badge Condizioni H/E/S con stato `dim` se non attivo
- [x] Cella Assegnatario con avatar iniziali; gestione "Non assegnato"
- [x] Cella Ticket Figlio Attivo con leaf, stallo, badge "catena troppo profonda" (M3)

### 4.2 Tab "Tutti i TC" — PRD §4.2.3

- [x] `COLUMNS_ALL`: stesse colonne escluse `Cliente` e `Ticket Figlio Attivo`
- [x] Aggiunta colonna `Data Chiusura` (mappata su `resolutionDate`)
- [x] Aggiunta colonna `Tempo Ris. (gg)` calcolato come `(resolutionDate − createdAt) / 1 giorno` (calendario, non lavorativi — è una metrica di durata, non di stallo)
- [x] Switch tab via click su `.tab[data-tab]`, counter aggiornati su entrambi
- [x] Smoke browser: tab "Tutti i TC" mostra 8 righe (dedup), header senza Cliente/Leaf, con Data Chiusura/Tempo Ris.

### 4.3 Filtri condizione (OR) — PRD §4.2.6

- [x] 4 pill `Tutti / Highest / In Escalation / Spoccato` mutuamente esclusive (`.filter-pill[data-cond]`)
- [x] Stato `active` sulla pill selezionata, switch su click
- [x] Smoke browser: click su "Highest" → 7 righe (TC-1042/1037/1029/1024×2/1015/1011), TC-1019 e TC-1008 esclusi
- [x] Logica OR (no combinazione AND in fase 1) — Q&A §5.25

### 4.4 Toggle "Nascondi WFR"

- [x] Toggle UI con switch animato — `.toggle input + .switch`
- [x] Filtra SOLO la tabella (filtro su `r.statusFamily === 'wfr'`), NON i KPI — Q&A §5.26
- [x] Smoke browser: con toggle ON → 8 righe (TC-1019 nascosto), KPI 3 invariato a 1

### 4.5 Ricerca client-side

- [x] Campo ricerca full-text con debounce 100ms
- [x] Filtro case-insensitive su key, summary, status, assegnatario.displayName, customer.name+code, lastResponseAuthor
- [x] Esecuzione interamente lato client su `currentSnapshot` (no chiamate aggiuntive) — Q&A §5.27
- [x] Smoke browser: query "migrazione" → 2 righe TC-1024 (entrambe per leaf), query "CL-00177" → 1 riga TC-1015

### 4.6 Ordinamento

- [x] Header `<th data-sort>` cliccabili con indicatore: `⇅` (idle) / `▲` (asc) / `▼` (desc)
- [x] Default DESC su colonne data/numeriche (`created`, `days`, `lastResp`, `resolution`); ASC sulle altre
- [x] Click ripetuto inverte direzione
- [x] `applySort()` con accessor per chiave; null in fondo indipendentemente dalla direzione
- [x] Ordinamento omogeneo su "Giorni s/Risc.": fallback partecipa allo stesso ordinamento, distinto solo dall'icona ⚠ (Q&A §5.28) — verificato: ordine DESC `9⚠/8/6/5/4/3/1/1/0`, ASC `0/1/1/3/...`

### 4.7 Aggiornamento UI

- [x] Auto-refresh ogni 60s in foreground (la cache backend cambia solo a TTL=600s, ma il countdown si aggiorna fluido)
- [x] Countdown "prossimo poll tra…" basato su `nextRefreshSeconds` dal backend
- [x] Timestamp "ultimo aggiornamento" sempre visibile in header e footer

### Smoke test M4 superati
- [x] Filtro Highest: 7/9 righe; reset Tutti: 9/9
- [x] Toggle WFR: 9 → 8 righe, KPI invariati
- [x] Ricerca "migrazione": 2 righe; "CL-00177": 1 riga; reset: 9
- [x] Sort giorni DESC poi ASC, indicatori ▼/▲ cambiano correttamente
- [x] Switch tab: layout colonne cambia (12 attive vs 12 tutti, set diverso), 9 → 8 righe (dedup)
- [x] Console pulita: nessun errore

---

## M5 — Ruoli amministratore / utente / ospite (stima 2 gg)

- [x] `admin`: accesso completo + admin panel `/admin` — implementato in M1
- [x] `user`: accesso completo dashboard in sola lettura — implementato in M1; NO link admin nel chip utente; redirect automatico su `/admin` → `/`; API admin restituisce 403
- [x] `ospite`: vista ridotta solo 4 KPI aggregati, NO dettaglio ticket nominativo — implementato in M1+M2; verificato lato backend (`rows=[]`, `rowsAll=[]`) e UI (`body.role-ospite` nasconde board/toolbar/tabs)
- [x] Segregazione lato UI accettata come limite (bypassabile DevTools) — Q&A §6.29; in più filtro lato backend per non inviare PII over-the-wire
- [x] Vista ospite mostra solo i 4 KPI aggregati (KPI 1+2+3+4); flag SPOCCATO/IN ESCALATION sono aggregati nel KPI 1 — Q&A §6.32 ✓
- [x] **Layout kiosk** ottimizzato: `body.role-ospite` ingrandisce i KPI a 88px font, padding 32/28/28, border-left 8px, box-shadow accentuato → leggibile da monitor a distanza
- [x] **Sessione persistente** opt-in via checkbox "Mantieni accesso (kiosk mode)" su login → token in `localStorage` invece di `sessionStorage`. Deroga documentata al pattern Archiva auth-standard (che impone solo sessionStorage); attivata per il caso d'uso esplicito kiosk

### Smoke test M5 — 3 ruoli verificati nel browser
- [x] Admin (`marco.pastore`): vede tutto, accede a `/admin`, sessionStorage
- [x] User (`giulia.nogara` ruolo=user): vede dashboard, NO admin link, `/admin` → redirect `/`, API admin → 403, sessionStorage
- [x] Ospite (`kiosk.sala` ruolo=ospite, kiosk ON): board nascosta, KPI giganti (88px), token in localStorage (sopravvive a chiusura tab), `body.role-ospite` applicata, KPI corretti 8/8/1/4

---

## M6 — Alerting (opzionale fase 2)

- [ ] Notifiche Teams su soglia "Giorni senza Riscontro" superata
- [ ] Notifiche email su ticket Highest che supera SLA
- [ ] Configurazione soglie per ruolo
- [ ] (in fase 1: solo evidenziazione visiva — già coperta da M3.4)

---

## Trasversali — Branding, sicurezza, qualità

### Branding

- [ ] Palette ufficiale Archiva applicata (variabili CSS `--archiva-*` da mockup) — PRD §4.4
- [ ] Logo Archiva nell'header
- [ ] Coerenza visiva con Dashboard Ricavi (riferimento)

### Sicurezza & GDPR

- [ ] HTTPS obbligatorio
- [ ] Cookie sessione `httpOnly` + `secure` (no CSRF token: app non scrive su Jira) — PRD §5, Q&A §8.38
- [ ] Nessun secret nel repository (verificato con scan pre-commit)
- [ ] Log applicativi privi di PII di dominio: NO `issue.key`, `summary`, `assignee`, reporter — PRD §5, Q&A §8.37
- [ ] Solo email + password_hash + role persistiti — PRD §5

### Compatibilità

- [ ] Test su Chrome, Edge, Firefox (ultime 2 major) — PRD §5
- [ ] No supporto mobile in fase 1 (accettato)

### Performance

- [ ] Caricamento iniziale <2s su dato in cache
- [ ] Aggiornamento dashboard <1s sul dato già in cache

### i18n

- [ ] UI in italiano
- [ ] Status name Jira lasciati in lingua nativa workflow (es. `Waiting for reporter`) — PRD §5, Q&A §9.44

---

## Criteri di accettazione (PRD §11)

- [ ] Login funzionante con standard auth Archiva, ruoli gestiti dall'admin panel standard
- [ ] Dashboard mostra TC + HDX nel periodo che soddisfano la condizione OR; HDX orfani esclusi; finestra `created >= 01/01/2026`
- [ ] Colonne tabella e KPI rispecchiano §4.2.1 / §4.2.2 / §4.2.3
- [ ] KPI 1 conta solo TC attivi che soddisfano OR; KPI 3/4 case-insensitive EN+IT
- [ ] "Giorni senza Riscontro" calcolati in giorni lavorativi, validato su campione reale; fallback `updated` segnalato in UI
- [ ] Risoluzione catena WFS con figli multipli, badge "catena troppo profonda" oltre 10 livelli, "Ngg fermo" su ultimo commento del leaf
- [ ] Colorazione riga 0–2 verde / 3–7 arancione / ≥8 rosso (giorni lavorativi)
- [ ] Filtri OR, ricerca client-side, toggle "Nascondi WFR" (solo tabella) funzionanti
- [ ] Aggiornamento ogni 10 minuti verificato; timestamp sempre visibile; banner backoff 429
- [ ] Vista ospite: solo 4 KPI aggregati, no dettaglio nominativo, kiosk mode previo login
- [ ] Deep link su ticket apre Jira in nuova tab
- [ ] Nessuna chiamata di scrittura verso JSM nei log; log privi di PII di dominio
- [ ] Singola istanza Railway, no scaling orizzontale, primo poll lazy post-restart
- [ ] Custom field `TICKET SPOCCATO` e `IN ESCALATION` mappati correttamente via env
- [ ] Deploy su Railway con dominio Archiva
- [ ] Nessun secret nel repository

---

## Punti aperti / da verificare in implementazione

> Validati con `scripts/check_jira` su istanza reale Archiva (2026-04-27, account Marco Pastore).

- [x] **Custom field SPOCCATO/ESCALATION** — `customfield_11787` e `customfield_12384`, tipo `multicheckboxes` (parser aggiornato per scorrere la lista cercando "Yes"). Censiti in `.env.example`.
- [x] **Link type Parent/Child** — esiste link type `Parent` (id=10302, "Padre DI"/"Figlio DI") MA il backend usa il campo nativo `fields.parent` di JSM Cloud, che è la via standard. OK.
- [x] **SLA** — risolto cambiando token: con `michael.seren@archivagroup.it` (Service Desk Agent) i 5 cicli SLA standard sono esposti correttamente. Distribuzione reale snapshot 2026-04-27: 9 breached / 1 ok / 10 none (TC non Service Request). Badge label es: `✓ 11h 52m`, `⚠ Breached -5h 21m`.
- [!] **Bot blocklist** — `JIRA_BOT_BLOCKLIST` ancora vuoto. Da popolare osservando le prime righe con `lastResponseAuthor` sospetti (Automation, system, bot user). Rifresh cache automatica al cambio.
- [x] **Reporter interno Archiva** — coperto dalla regola "author != reporter" sui commenti; raffinazione opzionale via `ARCHIVA_INTERNAL_DOMAINS=archivagroup.com,archiva.it` già censita nel `.env`.

### Scoperte tecniche dal primo onboarding reale (2026-04-27)

- **API Jira `/rest/api/3/search` rimossa** (CHANGE-2046, ott 2024). Migrato a `/rest/api/3/search/jql` con paginazione cursor-based via `nextPageToken` (niente più `total` né `startAt`).
- **TC sono di tipo `[System] Incident`**, non Customer Request — l'endpoint `/rest/servicedeskapi/request/{id}/sla` ritorna 403/404 anche con permessi corretti. Gli SLA effettivi vivono nei `customfield_*` di tipo `sd-sla-field` (vedi punto sopra).
- **Bug fix critici applicati durante l'onboarding**:
  - `urgent_tcs` ora filtra anche per `isActive` (prima includeva i TC chiusi nel tab "Vista TC Attivi")
  - `MAX_PAGES` da 50 a 100 (TC totali nel periodo: 7017, prima cappato a 5000)
  - `load_dotenv()` spostato in `backend/__init__.py` per garantire l'ordine di caricamento prima dell'import di `jira_client.py` (altrimenti l'app finiva sempre in DEMO mode)

### Numeri reali a inizio fase 1 (snapshot 2026-04-27)

| Metrica | Valore |
|---|---|
| Tempo primo fetch | ~40s |
| TC urgenti aperti (KPI 1) | 20 |
| TC in corso (KPI 2) | 644 |
| TC totali periodo (sub) | 7017 |
| TC Waiting for reporter | 307 |
| TC Waiting for son | 207 |
| Rows con leaf risolto | 7 |
| Rows con fallback `updated` | 3 |
| Distribuzione severity | green=9, orange=5, red=6 |
| Distribuzione SLA | breached=9, ok=1, none=10 |

## Runbook deploy in produzione

Vedi sezione **"Deploy in produzione (Railway)"** in [README.md](README.md). Step:

1. **Onboarding Jira** — `python -m scripts.check_jira` con `.env` locale popolato → copia gli ID custom field stampati
2. **Setup Railway** — connetti repo, crea Volume su `/data`, popola env vars (lista in README)
3. **Deploy** — push del branch principale; Railway rileva `Procfile`/`railway.json`/`runtime.txt`
4. **Post-deploy** — `GET /api/health`, primo login con `DEFAULT_PASSWORD`, **cambio immediato delle 3 password seed**
5. **Onboarding utenti** — invitare agenti `user` e creare account `kiosk.sala` ruolo `ospite` per il monitor di sala

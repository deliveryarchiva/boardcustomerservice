# Customer Service Board

Web app interna Archiva Group per il monitoraggio dei ticket ad alta priorità del progetto Customer Care su Jira Service Management.

## Documentazione

- [PRD](specs/prd.md) — requisiti funzionali e non funzionali
- [Q&A](specs/QeA.md) — chiarimenti sulle scelte progettuali
- [Mockup](mockup/index.html) — riferimento visivo
- [ToDoList](ToDoList.md) — stato avanzamento

## Stack

- **Backend**: FastAPI (Python 3.11) — coerente con pattern Archiva
- **Frontend**: Jinja2 templates + JS vanilla (palette Archiva, no React/Vite)
- **Auth**: standard Archiva (sha256, sessioni UUID, `users.json` su volume)
- **Deploy**: Railway (singola istanza)
- **Storage**: file JSON su volume Railway per utenti; cache in-memory per ticket Jira

## Avvio locale

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/Mac
pip install -r requirements.txt
copy .env.example .env          # poi compilare i valori
uvicorn backend.main:app --reload --port 8000
```

Apri http://localhost:8000 → login con uno degli utenti seed (vedi `.env.example` per la password di default).

> **Modalità DEMO**: se `JIRA_API_TOKEN` è vuoto, l'app gira con dati finti (vedi `backend/demo_data.py`). Utile per sviluppo UI senza accesso a Jira.

---

## Deploy in produzione (Railway)

### Step 1 — Onboarding Jira (verifica parametri)

Prima del deploy, verifica che le credenziali Jira funzionino e identifica gli ID dei custom field. Configura `.env` localmente e lancia:

```bash
python -m scripts.check_jira
```

Lo script esegue 5 controlli:

1. **Auth** — chiama `/myself` e conferma email + token
2. **Custom field** — cerca `TICKET SPOCCATO` e `IN ESCALATION` per nome → stampa l'ID `customfield_*`
3. **Link types** — verifica il Parent/Child (in JSM Cloud è di solito il campo nativo `parent`, non un link type)
4. **JQL base** — esegue la query con OR conditional e mostra le prime 5 issue
5. **SLA** — testa l'endpoint SLA su una issue campione

Copia gli ID stampati nei due env `JIRA_FIELD_TICKET_SPOCCATO` e `JIRA_FIELD_IN_ESCALATION`.

### Step 2 — Setup Railway

1. Crea progetto Railway → connettilo al repo Git
2. **Volume**: `+ New → Volume → Mount path: /data` (necessario per la persistenza di `users.json`)
3. **Variabili d'ambiente** (Settings → Variables):

   ```
   # Auth
   DEFAULT_PASSWORD=<password-iniziale>      # cambiare dopo primo login
   RAILWAY_VOLUME_MOUNT_PATH=/data           # impostato automaticamente dal volume

   # Jira
   JIRA_BASE_URL=https://archivagroup.atlassian.net
   JIRA_EMAIL=service-account@archivagroup.com
   JIRA_API_TOKEN=<api-token-atlassian>
   JIRA_PROJECT_KEY_TC=TC
   JIRA_PROJECT_KEY_HDX=HDX
   JIRA_FIELD_TICKET_SPOCCATO=customfield_XXXXX
   JIRA_FIELD_IN_ESCALATION=customfield_YYYYY
   JIRA_DATE_FROM=2026/01/01

   # Polling + concorrenza
   POLL_INTERVAL_SECONDS=600
   JIRA_ENRICH_CONCURRENCY=5

   # Filtri operatore (csv)
   JIRA_BOT_BLOCKLIST=
   ARCHIVA_INTERNAL_DOMAINS=archivagroup.com,archiva.it
   ```

4. **Deploy**: Railway rileva `Procfile` + `railway.json` e lancia `uvicorn backend.main:app`. La versione Python è fissata da `runtime.txt` (`python-3.11`).

5. **Dominio**: aggiungi un dominio Archiva tramite Railway → Settings → Networking.

### Step 3 — Verifiche post-deploy

- `https://<tuo-dominio>/api/health` → `{"ok": true, "version": "0.1.0", "demo": false}`
- Login con uno dei 3 utenti seed (Marco / Paolo / Chiara) — password = `DEFAULT_PASSWORD`
- **Cambia immediatamente le password** seed dal pannello `/admin` o tramite la voce "🔑 Cambia password"
- Verifica che il primo poll completi (timestamp aggiornato in header)
- Se appare il banner di degrado: controlla i log Railway per dettagli (probabili: 401/403 auth, JQL errato, custom field non riconosciuti)

### Step 4 — Onboarding utenti

Da `/admin`:
- Invita gli agenti Customer Care con ruolo `user` (sola lettura)
- Crea un account `ospite` per il monitor di sala (es. `kiosk.sala`); al login attiva la checkbox "Mantieni accesso (kiosk mode)" sul tablet/PC del monitor
- Le 3 admin di default (Marco, Paolo, Chiara) sono già seed

---

## Architettura

```
backend/
├── auth.py            # standard Archiva: sha256, UUID sessions, users.json
├── main.py            # FastAPI app, routes, lifespan
├── jira_client.py     # httpx client read-only, paginazione, backoff 429
├── transform.py       # issue→row, KPI, severity, normalizzazione status
├── snapshot.py        # orchestratore: fetch + commenti + leaf + SLA
├── cache.py           # SnapshotCache lazy (TTL=10min, lock concorrenza)
├── comments.py        # ultima risposta operatore (esclusione reporter+bot)
├── leaf.py            # risoluzione catena Waiting for son → leaf
├── sla.py             # parser SLA Atlassian
├── business_days.py   # giorni lavorativi italiani (Pasquetta calcolata)
├── demo_data.py       # fixtures per modalità demo
├── templates/         # login.html, index.html, admin.html (Jinja2)
└── static/            # css/archiva.css, js/auth.js
data/                  # users.json (volume Railway in prod)
specs/                 # PRD + Q&A
mockup/                # mockup riferimento visivo
scripts/
└── check_jira.py      # verifica onboarding Jira
```

## Variabili d'ambiente

Vedi [`.env.example`](.env.example) per la lista completa con descrizioni.

## Limiti noti (fase 1 accettati)

- Singola istanza Railway: cache in-memory non condivisa, no scaling orizzontale (PRD §5)
- Vista ospite segregata lato UI (bypassabile da DevTools — Q&A §6.29). Defense-in-depth lato API: `rows`/`rowsAll` filtrati a `[]` per `ruolo=ospite`
- Alerting Teams/email è fase 2 (M6) — fase 1 ha solo evidenziazione visiva
- Sessioni in-memory: si perdono al restart Railway (utenti devono rifare login)

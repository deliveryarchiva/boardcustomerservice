# PRD — Customer Service Board

**Applicazione web di monitoraggio ticket ad alta priorità — Progetto Customer Care Archiva Group (Jira Service Management)**

---

## 1. Obiettivo

Realizzare una **web app interna Archiva Group** che si interfacci con Jira Service Management (JSM) per fornire al team Customer Care e al management una **dashboard di osservabilità** (refresh periodico ogni 10 minuti, non real-time) dei ticket ad alta priorità del progetto *Customer Care*, con evidenziazione di SLA a rischio, aging critico e carico per assegnatario.

L'obiettivo operativo è **ridurre i tempi di reazione sugli incident ad alto impatto** e fornire un punto unico di verità sullo stato del servizio, senza dover costruire filtri manuali in Jira. In fase 1 il valore è puramente di **osservabilità**: ogni intervento avviene in Jira.

> **Nota**: la UI deve esporre in modo visibile (header o footer della board) il **timestamp dell'ultimo aggiornamento** dei dati.

---

## 2. Contesto e stakeholder

| Ruolo | Responsabilità sull'app |
|---|---|
| Responsabile Customer Service | Sponsor, fruitore principale della dashboard manageriale |
| Team Lead Customer Care | Gestione operativa quotidiana, assegnazioni, escalation |
| Agenti Customer Care | Consultazione dei ticket assegnati e vista personale |
| IT / Owner tecnico | Manutenzione integrazione JSM, gestione deploy |

**Progetto Jira di riferimento:** Customer Care (Archiva Group) su Jira Service Management Cloud.

---

## 3. Utenti e casi d'uso principali

### UC1 — Vista manageriale (dashboard)
Il Responsabile apre la home e vede in <3 secondi i 4 KPI di sintesi sui TC (vedi §4.2.1):
- Totale TC urgenti aperti (Highest OR Spoccato OR In Escalation, ancora attivi)
- Totale TC in corso (`statusCategory ≠ Done`)
- Totale TC in `Waiting for reporter` (palla al cliente)
- Totale TC in `Waiting for son` (palla a un HDX figlio)

### UC2 — Vista operativa
Il Team Lead filtra i ticket ad alta priorità per assegnatario, stato e coda per avere visione immediata del carico e delle situazioni critiche. **Nessuna azione di scrittura sull'app**: ogni intervento (riassegnazione, cambio stato, commento) avviene direttamente in Jira tramite deep link.

### UC3 — Vista utente
L'utente autenticato consulta la board nella stessa modalità del Team Lead, in sola lettura.

### UC4 — Vista ospite
L'ospite vede una versione ridotta della dashboard (solo KPI aggregati, senza dettaglio ticket nominativo), utile per condivisione con stakeholder esterni o monitor di sala.

### UC5 — Alerting
Quando un ticket Highest supera la soglia SLA configurata, la board lo evidenzia in rosso e (opzionale fase 2) invia notifica Teams/email al Team Lead.

---

## 4. Requisiti funzionali

### 4.1 Integrazione Jira Service Management
- Autenticazione verso Atlassian Cloud tramite **API token + email** (account di servizio dedicato, no credenziali personali).
- **Solo operazioni di lettura** verso JSM: l'app non scrive mai su Jira.
- Endpoint REST utilizzati:
  - `GET /rest/api/3/search` con JQL per i ticket TC e HDX correlati
  - `GET /rest/servicedeskapi/request/{id}/sla` per dati SLA
  - `GET /rest/api/3/issue/{id}?expand=changelog` per dettaglio campi custom (`TICKET SPOCCATO`, `IN ESCALATION`) e link
  - `GET /rest/api/3/issue/{id}/comment` per identificare l'ultima risposta verso il cliente
- **Progetti coinvolti**:
  - `TC` (Ticket Customer Care) — ticket di primo livello aperti dal cliente.
  - `HDX` (Helpdesk eXtended) — ticket figli interni generati come Parent link da un TC. Gli **HDX orfani** (senza Parent valido verso un TC) sono **esclusi** dal dataset, anche se di priorità Highest.
- **Logica OR vincolante**: la condizione di ingresso è sempre l'OR `priority = Highest OR "TICKET SPOCCATO" = Yes OR "IN ESCALATION" = Yes`. Un TC declassato a Medium ma ancora flaggato Spoccato o In Escalation **rimane** in board.
- **Finestra temporale**: `created >= 01/01/2026`. I ticket creati prima di questa data **non appaiono mai**, anche se riaperti / ancora attivi nel 2026.
- JQL base (parametrizzabile):
  ```
  (project = TC OR project = HDX)
  AND created >= "2026/01/01"
  AND (priority = Highest OR "TICKET SPOCCATO" = Yes OR "IN ESCALATION" = Yes)
  ORDER BY created DESC
  ```
- **Custom field `TICKET SPOCCATO` e `IN ESCALATION`**: gli ID `customfield_*` reali e il loro tipo (`option` con value Yes/No, boolean, ecc.) **vanno verificati in fase di implementazione** sull'istanza JSM Archiva e mappati nelle env var dedicate (`JIRA_FIELD_TICKET_SPOCCATO`, `JIRA_FIELD_IN_ESCALATION`).
- **TC chiusi**: rientrano nel dataset (necessari per il tab "Tutti i TC") ma sono **esclusi** dai KPI 2/3/4 e dal tab "Vista TC Attivi".
- **Polling** ogni **10 minuti** lato backend (non dal browser) con cache in memoria. Singola istanza Railway in fase 1 (no scaling orizzontale: cache in-memory non condivisa).
- **Comportamento utente concorrente**: un secondo utente che si connette dentro la finestra di cache **non** triggera un nuovo fetch — riceve il dato in cache.
- **Avvio post-restart**: la cache parte vuota; il primo utente attende il primo poll (no fetch eager).
- **Paginazione obbligatoria** su `/search` con `startAt`/`maxResults` per coprire dataset oltre la pagina di default (50 issue).
- Gestione rate limit Atlassian (429): backoff esponenziale + log + **banner UI "dato non aggiornato da Xs"** durante il backoff.

### 4.2 Dashboard

> Logica e impostazione visiva ereditate dal prototipo **Archiva Group - Escalation Monitor**: la board produzione ne è l'evoluzione web autenticata e auto-aggiornante.

#### 4.2.1 KPI header

Set ridotto a **4 KPI**, tutti calcolati esclusivamente sui **TC** (gli HDX non hanno un KPI dedicato; restano comunque visibili nelle righe della tabella tramite la colonna "Ticket Figlio Attivo" e il flag SPOCCATO/IN ESCALATION sul leaf).

1. **Totale TC urgenti aperti** — TC che soddisfano la condizione di ingresso `priority = Highest OR "TICKET SPOCCATO" = Yes OR "IN ESCALATION" = Yes` **e** sono ancora attivi (`statusCategory ≠ Done`). I TC chiusi non vengono conteggiati anche se il flag è ancora attivo.
2. **Totale TC in corso** — tutti i TC del periodo con `statusCategory ≠ Done`, indipendentemente dalla condizione OR. È il denominatore "carico di lavoro" del Customer Care.
3. **Totale TC Waiting for reporter** — TC in stato `Waiting for reporter` (match **case-insensitive** su versione inglese e italiana, es. `Waiting for reporter`, `In attesa cliente`).
4. **Totale TC Waiting for son** — TC in stato `Waiting for son` (palla nelle mani di un HDX figlio). Match analogo al KPI 3 sulle varianti del nome stato.

> **Esclusione esplicita rispetto al PRD precedente**: sono stati rimossi i KPI per HDX totali/attivi, il "Tempo Medio Senza Riscontro" aggregato, il conteggio Totale TC nel periodo, e i contatori SPOCCATO/IN ESCALATION cross-progetto. Il flag su HDX figli resta visibile nella colonna "Condizioni" del leaf in tabella.

> **Coerenza KPI / tabella**: KPI e tabelle si aggiornano sullo **stesso ciclo di polling** e dallo **stesso snapshot** di cache, per garantire numeri coerenti tra header e righe.

#### 4.2.2 Tab "Vista TC Attivi" — colonne tabella
| # | Colonna | Origine dato |
|---|---|---|
| 1 | TC Key | `issue.key` (deep link a Jira) |
| 2 | Cliente | nome + codice cliente (campo custom o lookup `reporter.organization`) |
| 3 | Summary | `issue.fields.summary` |
| 4 | Stato | `issue.fields.status.name` con badge colorato |
| 5 | Condizioni | badge **H** (Highest), **E** (In Escalation), **S** (Spoccato) — derivati da priorità + flag custom |
| 6 | Assegnatario | `issue.fields.assignee.displayName` |
| 7 | Creato il | `issue.fields.created` |
| 8 | **Giorni senza Riscontro** (gg lavorativi) | calcolo §4.2.4 — icona dedicata se il valore deriva dal fallback `updated` (per distinguerlo da una vera ultima risposta) |
| 9 | Ultima Risposta (data) | calcolo §4.2.4 |
| 10 | Autore Risposta | calcolo §4.2.4 |
| 11 | Ticket Figlio Attivo | risoluzione catena §4.2.5 |
| 12 | SLA | dato da `GET /rest/servicedeskapi/request/{id}/sla` (badge tempo residuo / breached). I KPI aggregati basati su SLA sono fuori scope fase 1. |

> **Multipli figli aperti (§4.2.5)**: se un TC ha più di un HDX non completato in stato `waiting…`, la riga del TC viene **duplicata** — una per ogni HDX leaf — così che ogni leaf sia visibile e ordinabile separatamente.

#### 4.2.3 Tab "Tutti i TC" — colonne tabella
Stesse colonne di §4.2.2 escluse `Cliente` e `Ticket Figlio Attivo`, con in aggiunta:
- Data Chiusura (`resolutiondate`)
- Tempo Risoluzione (gg) = `resolutiondate − created` in giorni

#### 4.2.4 Logica "Giorni senza Riscontro" (CHIAVE)

L'app **non** usa banalmente `updated` di Jira — quel campo si muove anche per cambi di stato interni o automazioni e non rappresenta una vera interazione con il cliente.

Algoritmo:
1. Per ogni ticket TC (e per il leaf, vedi §4.2.5), si recuperano i commenti via `GET /rest/api/3/issue/{key}/comment`.
2. Si identifica l'**ultima risposta verso il cliente** = ultimo commento pubblico (`jsdPublic = true`) il cui autore **non** è il reporter del ticket (è un operatore Archiva).
   - **Reporter interno**: se il reporter risulta essere un account interno Archiva (ticket aperto da un operatore per conto del cliente), si tratta logicamente come "cliente" ai fini di questo confronto — i suoi commenti **non** contano come risposta operatore.
   - **Bot / automazioni**: i commenti scritti da account bot / automation Jira sono **esclusi** dal calcolo (blocklist di account configurabile).
3. Si memorizza:
   - `lastResponseToClient` = `comment.created` (mai `updated`, per evitare che l'edit di un vecchio commento azzeri ingiustamente i giorni senza riscontro)
   - `lastResponseAuthor` (displayName)
4. **Giorni senza riscontro** = numero di **giorni lavorativi** (escluso sabato, domenica e festività italiane) intercorsi tra `lastResponseToClient` e `now`.
5. **Fallback**: se non esiste alcuna risposta verso il cliente valida, si usa lo stesso conteggio di giorni lavorativi tra `issue.updated` e `now`. La riga viene marcata con un'icona dedicata (vedi §4.2.2) per indicare che il valore è derivato dal fallback. È accettato che `updated` possa essere mosso da automazioni interne.
6. **Colorazione riga** in base ai giorni lavorativi:
   - `0–2 gg` → verde (success)
   - `3–7 gg` → arancione (warning)
   - `≥ 8 gg` → rosso (danger)

#### 4.2.5 Risoluzione catena `Waiting for son` → leaf ticket

Quando un TC ha figli HDX e uno o più figli sono in stato **`Waiting for son`** (o qualsiasi stato `waiting…` diverso da "Waiting for reporter"), il flusso di lavoro è in realtà fermo su un *nipote* della catena. L'app deve risalire la catena e calcolare i giorni di stallo sul nodo realmente bloccato (il **leaf ticket**).

Regole:
- Se il ticket è in stato `waiting…`, segui il link **Parent/Child standard JSM** (relazione "is parent of / is child of"; **non** l'`Epic Link`) verso un figlio non `Completato`. L'ID/nome esatto del link type va verificato e bloccato in fase di implementazione sull'istanza JSM Archiva.
- **Figli multipli non completati**: se il TC ha più di un HDX in stato `waiting…`, la board mostra **una riga per ogni figlio**, ciascuna con il proprio leaf, anziché sceglierne uno arbitrariamente (vedi §4.2.2).
- Ricorsione max **profondità 10** (safety guard). Se il limite viene raggiunto, si applica il fallback al ticket di partenza e si mostra un badge dedicato **"catena troppo profonda"** sulla riga.
- Se il leaf non è caricato nel dataset principale, fai una **chiamata supplementare** a `GET /rest/api/3/issue/{leafKey}` per popolare i dati. L'overhead di round-trip extra (fino a ~2N per ciclo, dove N = TC in waiting) è considerato sostenibile rispetto al rate limit Atlassian, dato il polling a 10 minuti.
- Visualizzazione: se la catena ha lunghezza > 1, mostra `<figlio diretto> ➜ <leaf>` con stato e assegnatario del leaf, e `⏱ Ngg fermo` sul leaf, calcolato come **giorni dall'ultimo commento del leaf** (non come tempo nello stato corrente da changelog).
- Caso speciale: se il **figlio diretto** è in `Waiting for reporter`, lo stallo è sul cliente — etichetta dedicata "Attesa Cliente / Helpdesk", non si scende ulteriormente nella catena (rimane a livello 2 anche se un eventuale nipote avesse stallo cliente).

#### 4.2.6 Filtri, ricerca, ordinamento
- **Filtri condizione**: Tutti / Highest / In Escalation / Spoccato. La logica resta sempre **OR** (un ticket entra se soddisfa almeno una condizione); non è prevista in fase 1 una combinazione AND tra flag (es. "Highest **e** Spoccato").
- **Toggle**: "Nascondi TC in Waiting for reporter" — agisce **solo sulla tabella**, **non** sui KPI (KPI 3 mantiene il proprio dataset completo).
- **Ricerca** full-text su key / summary / assegnatario / cliente — eseguita **lato client** sul dataset in cache (no chiamate aggiuntive a Jira).
- **Ordinamento** su tutte le colonne dotate di `data-sort` (key, customer, summary, status, assignee, created, days, lastResp, lastRespAuthor). L'ordinamento sulla colonna "Giorni senza Riscontro" è omogeneo: i valori da fallback `updated` partecipano allo stesso ordinamento ma sono distinguibili graficamente dall'icona dedicata.
- **Aggiornamento automatico** della UI ogni 10 minuti senza refresh manuale; il **timestamp dell'ultimo aggiornamento** è sempre visibile in UI.
- **Deep link** su ogni key → apre il ticket in Jira in nuova tab (qualsiasi modifica avviene su Jira, non sull'app).

### 4.3 Autenticazione applicazione
- **Riuso integrale dello standard di autenticazione Archiva** già implementato sulle altre web app interne (es. AI Minute Riunioni, Dashboard Ricavi): login con password, gestione utenti, admin panel, cambio password, **DB locale** (no SSO, no mapping 1:1 con account Atlassian). Nessuna logica di auth viene reinventata per questa app, e la **mappatura ruoli** segue il pattern standard Archiva senza estensioni custom.
- **Ruoli applicativi**:
  - `amministratore` — gestione utenti + accesso completo alla dashboard.
  - `utente` — accesso completo alla dashboard in sola lettura.
  - `ospite` — vista ridotta (solo i 4 KPI di §4.2.1 in forma aggregata, no dettaglio ticket nominativo).
- **Segregazione vista ospite**: in fase 1 implementata **lato UI**. Si accetta esplicitamente il limite che la segregazione è bypassabile da DevTools — non è un requisito di sicurezza forte.
- **Vista per-assegnatario / vista personale dell'agente**: **fuori scope** fase 1.
- **Vista da monitor di sala / kiosk mode**: l'utente ospite **deve essere loggato** anche per la visualizzazione su monitor; è ammessa una sessione persistente di lunga durata (kiosk mode).

### 4.4 Branding
- Palette colori ufficiale Archiva Group.
- Logo Archiva nell'header.
- Layout coerente con le altre app interne (Dashboard Ricavi come riferimento).

---

## 5. Requisiti non funzionali

| Categoria | Requisito |
|---|---|
| Performance | Caricamento iniziale <2s, aggiornamento dashboard <1s sul dato già in cache. Polling backend ogni **10 minuti**; il dato visibile può essere vecchio fino a ~10 minuti — è sufficiente per il caso d'uso. |
| Scalabilità | **Singola istanza Railway** in fase 1 (cache in-memory non condivisa). No scaling orizzontale. |
| Disponibilità | 99% in orario ufficio (lun–ven 8:00–19:00 CET) |
| Sicurezza | API token JSM in variabili d'ambiente Railway (lettura accessibile a tutto il team IT, non solo amministratore app), mai nel codice. HTTPS obbligatorio. Sessioni con cookie `httpOnly` + `secure`. **CSRF token non necessari** (l'app non esegue POST verso JSM). |
| Compatibilità | Chrome, Edge, Firefox ultime 2 major; no supporto mobile in fase 1 |
| GDPR / Logging | Nessun dato personale persistito oltre il minimo per auth (email, hash password, ruolo). I log applicativi **non devono contenere** PII di dominio (`issue.key`, `summary`, `assignee`, reporter, ecc.). |
| i18n | UI in italiano. Gli status name di Jira (es. `Waiting for reporter`) restano nella lingua nativa del workflow (inglese) e non vengono tradotti dall'app. |

---

## 6. Architettura tecnica proposta

```
[Browser] ── HTTPS ──► [Backend Node.js / Python] (singola istanza)
                             │
                             ├── Auth layer (standard Archiva, DB locale)
                             ├── Cache in-memory (TTL 10 min, refresh schedulato)
                             └── Client JSM REST (API token, paginato, backoff 429)
                                     │
                                     └──► Atlassian Cloud — JSM
```

**Stack suggerito:**
- **Backend**: Node.js (Express o Fastify) *oppure* Python (FastAPI) — allineato agli altri tool Archiva.
- **Frontend**: React + Vite, tabelle con TanStack Table, UI leggera.
- **DB**: SQLite o Postgres (solo utenti/auth/log audit).
- **Deploy**: Railway (allineato al pattern Archiva esistente).
- **Secrets**: variabili Railway (`JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_BASE_URL`, `JIRA_PROJECT_KEY`).

---

## 7. Modello dati (locale)

- `users` (id, email, password_hash, role, created_at) — gestita dal modulo auth standard Archiva.

I ticket e ogni dato operativo **non vengono persistiti**: sono sempre letti da Jira (cache in-memory TTL 10 min). L'app è **stateless rispetto al dominio Customer Care** — nessun audit log applicativo, perché non esistono azioni di scrittura.

---

## 8. Milestone

| # | Milestone | Deliverable | Durata stimata |
|---|---|---|---|
| M1 | Setup + auth | Skeleton app con login Archiva-standard riutilizzato, deploy Railway (singola istanza) | 2 gg |
| M2 | Integrazione JSM read-only (TC + HDX) | Polling 10 min, cache in-memory, fetch paginato issues + commenti + link, mappatura custom field, KPI base, banner backoff 429, timestamp ultimo aggiornamento | 4 gg |
| M3 | Logica "Giorni senza Riscontro" + leaf ticket | Calcolo ultima risposta cliente in giorni lavorativi, esclusione bot, gestione reporter interno, risoluzione catena `waiting-for-son` con figli multipli, colorazione, colonna SLA | 3 gg |
| M4 | Tabelle, filtri, ricerca, ordinamento | Tab Vista TC Attivi + Tutti i TC, filtri condizione OR, toggle WFR (solo tabella), ricerca client-side, icona fallback su giorni | 2 gg |
| M5 | Ruoli (amministratore / utente / ospite) | Segregazione delle viste lato UI, vista ospite ridotta ai KPI aggregati, kiosk mode da monitor | 2 gg |
| M6 | Alerting (Teams/email) — opzionale fase 2 | Notifiche su soglia "Giorni senza Riscontro" o escalation (solo evidenziazione visiva in fase 1) | 2 gg |

**Totale core (M1–M5):** ~13 giornate.

---

## 9. Rischi e mitigazioni

| Rischio | Impatto | Mitigazione |
|---|---|---|
| Rate limit Atlassian | Dashboard non aggiornata | Cache 10 min, backoff esponenziale, paginazione, banner di degrado in UI |
| Campo SLA non esposto via API su tutte le request type | KPI incompleti | Validazione in M2 su dataset reale, fallback su `duedate` |
| Catena `Waiting for son` profonda o ciclica | Loop / dato errato sui giorni fermo | Profondità max 10, fallback al ticket di partenza |
| Commenti del cliente classificati come risposta operatore | "Giorni senza Riscontro" sottostimato | Esclusione esplicita dell'autore = `reporter` del ticket |
| Disallineamento ruoli utenti Jira ↔ app | Vista utente errata | Mapping email Jira = email login app |
| Token API revocato | Down integrazione | Alert su 401, runbook per rigenerazione token |

---

## 10. Fuori scope (fase 1)

- App mobile.
- **Qualsiasi azione di scrittura su Jira** (riassegnazione, cambio stato, commenti, transizioni, bulk edit): ogni modifica avviene direttamente in Jira.
- Reportistica storica / trend (resta su Jira nativo o EazyBI).
- Integrazione con altri progetti Jira oltre Customer Care.
- Gestione utenti custom (si riutilizza interamente lo standard auth Archiva).
- **KPI aggregati basati su SLA** (la colonna SLA in tabella c'è; un eventuale KPI dedicato sarà valutato in futuro).
- **Vista personale per assegnatario** (filtrare automaticamente sui ticket dell'agente loggato).
- **Alerting attivo** via Teams/email sulla soglia "Giorni senza Riscontro" (in fase 1 c'è solo l'evidenziazione visiva sulla riga).
- Scaling orizzontale / multi-replica.
- Test automatici contro mock JSM per la catena `Waiting for son`: la validazione è manuale sul risultato finale.

---

## 11. Criteri di accettazione

- [ ] Login funzionante con standard auth Archiva (DB locale), ruoli `amministratore` / `utente` / `ospite` gestiti dall'admin panel standard, senza estensioni custom.
- [ ] Dashboard mostra tutti i TC e HDX nel periodo che soddisfano `priority = Highest OR TICKET SPOCCATO = Yes OR IN ESCALATION = Yes`, con HDX orfani esclusi e finestra `created >= 01/01/2026`.
- [ ] Colonne tabella e KPI rispecchiano §4.2.1 (4 KPI: TC urgenti aperti, TC in corso, TC WFR, TC WFS), §4.2.2 (inclusa colonna SLA), §4.2.3.
- [ ] KPI 1 conta solo TC che soddisfano la condizione OR **e** sono attivi (i TC chiusi non rientrano anche se flaggati). KPI 3 e 4 riconoscono lo status sia in inglese che in italiano (case-insensitive).
- [ ] **Giorni senza Riscontro** calcolati in **giorni lavorativi** sull'ultima risposta verso il cliente (commento `created`, autore non reporter, esclusi bot, esclusi commenti del reporter interno trattato come cliente), validato su un campione reale rappresentativo. Fallback su `updated` correttamente segnalato in UI.
- [ ] Risoluzione catena `Waiting for son` produce il leaf ticket atteso, gestisce **figli multipli** mostrando una riga per ogni HDX, applica fallback con badge "catena troppo profonda" oltre i 10 livelli, e calcola "Ngg fermo" sull'ultimo commento del leaf.
- [ ] Colorazione riga rispetta soglie 0–2 verde / 3–7 arancione / ≥8 rosso (giorni lavorativi).
- [ ] Filtri condizione (OR), ricerca client-side e toggle "Nascondi WFR" (solo tabella) funzionanti.
- [ ] Aggiornamento automatico ogni 10 minuti verificato; **timestamp ultimo aggiornamento** sempre visibile in UI; banner di degrado durante backoff 429.
- [ ] Vista ospite mostra solo i 4 KPI aggregati di §4.2.1, nessun dettaglio ticket nominativo, kiosk mode disponibile previo login.
- [ ] Deep link su ticket apre correttamente Jira (unica via per modifiche).
- [ ] Nessuna chiamata di scrittura verso JSM nei log applicativi; log privi di PII di dominio (key, summary, assignee).
- [ ] Singola istanza Railway, no scaling orizzontale, primo poll lazy post-restart.
- [ ] Custom field `TICKET SPOCCATO` e `IN ESCALATION` mappati correttamente (ID verificati sull'istanza JSM Archiva) tramite env var dedicate.
- [ ] Deploy su Railway con dominio Archiva.
- [ ] Nessun secret nel repository.

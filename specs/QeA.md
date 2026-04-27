# QeA — Customer Service Board

Domande di verifica sulla logica e sulla struttura dell'app, derivate da `prd.md`. Servono a stressare i casi limite e le scelte progettuali **prima** dell'implementazione.

---

## 1. Dataset & integrazione JSM

1. La JQL base unisce `project = TC OR project = HDX`. Un ticket HDX **non legato** ad alcun TC (orfano) entra comunque nella dashboard se ha `priority = Highest`? Vogliamo questo comportamento o filtriamo solo HDX con Parent valido verso TC? solo HDX con un parent
2. La finestra temporale è `created >= 2026/01/01`. Cosa succede a un TC creato il 31/12/2025 ma riaperto e ancora attivo nel 2026: deve apparire o no? Il PRD dice di no — è corretto? corretto
3. Se un TC era Highest al momento dell'apertura ma viene **declassato** a `Medium`, esce immediatamente dalla board? E se ha ancora `IN ESCALATION = Yes`, rimane (perché soddisfa l'OR)? rimane, la logica di OR deve sempre vincere.
4. I campi custom `TICKET SPOCCATO` e `IN ESCALATION` sono assunti come boolean Yes/No: in JSM sono `customfield_*` con `value` o `option`? È prevista una mappatura esplicita degli ID custom field nelle env var? Non lo so, devi verificare te.
5. La JQL non filtra `statusCategory != Done`: i TC chiusi nel 2026 entrano nel dataset. Sono **necessari** per il tab "Tutti i TC" ma vanno **esclusi** dai KPI 2/3/4 e dal tab "Vista TC Attivi"? Confermato? confermato
6. Polling 60s lato backend con cache in-memory: che cosa serve la dashboard di un secondo utente che si collega 5s dopo il primo poll? Il dato in cache (max 60s vecchio) o triggera un nuovo fetch? non triggera. Possiamo protare l'aggiornamento da 60s a 10 minuti
7. Su 429 di Atlassian, il backoff esponenziale congela il poll: durante il backoff la UI mostra dati stale o un banner "dato non aggiornato da Xs"? danner
8. Il dataset può crescere oltre la pagina di default `/search` (50 issue): è prevista paginazione `startAt`/`maxResults` o un cap (es. top 200)? Come si comporta se i ticket attivi superano il cap? paginazione

## 2. KPI header

9. KPI 3 "Tempo Medio Senza Riscontro": media calcolata sui soli **TC attivi** (non HDX, non chiusi). Se un TC attivo non ha mai ricevuto risposta da operatore, contribuisce con il fallback `now − updated` o viene escluso dalla media? contribuisce
10. KPI 4 "TC in Attesa Cliente" usa `status contiene "Waiting for reporter"`: case-sensitive? E se un workflow custom usa "In attesa cliente" in italiano? considera sia itqaliano che inglese
11. KPI 7/8 (SPOCCATO / IN ESCALATION) contano **solo TC** o anche HDX? Se un HDX figlio è in escalation ma il TC padre no, dove viene contato? si deve essere considerato
12. I KPI si aggiornano nello stesso ciclo di polling (60s) della tabella, o c'è un endpoint dedicato? Devono essere coerenti tra loro istante per istante (stesso snapshot)?usa lo stesso

## 3. Logica "Giorni senza Riscontro" (§4.2.4)

13. "Ultimo commento pubblico il cui autore non è il reporter": e se il reporter è un operatore Archiva (ticket aperto internamente per conto di un cliente)? Si rischia di non trovare mai una risposta valida. In questo caso identifica il reporter interno come se fosse il cliente.
14. Un commento di un **bot/automazione Jira** (es. account "automation") va considerato risposta operatore? Probabilmente no — serve una blocklist di account? no non considerare i commenti dei bot.
15. Il timestamp `lastResponseToClient` usa `created` o `updated` del commento? Se un operatore edita un vecchio commento, il "giorno senza riscontro" si azzera ingiustamente? usa solo il created
16. Soglie colorazione: il PRD dichiara `>7 rosso`, `3–7 arancione`, `<3 verde`, ma in §4.2.4 punto 6 e nei criteri di accettazione la finestra arancione include 7 e quella rossa parte da >7. Conferma: 0–2 verde, 3–7 arancione, ≥8 rosso? Confermo
17. Il calcolo è in **giorni di calendario** o **giorni lavorativi**? Per il Customer Care ha senso escludere weekend/festivi italiani? giorni lavorativi.
18. Il fallback su `issue.updated` quando non esistono commenti pubblici: se l'`updated` è mosso da un'automazione (cambio label, SLA tick), restituisce un valore artificialmente basso. È accettabile? ok

## 4. Catena `Waiting for son` → leaf (§4.2.5)

19. La regola dice "segui il primo link `outward` di tipo `Parent`": in JSM lo standard è il link "is parent of / is child of". Il PRD intende il link **Parent/Child** o l'`Epic Link`/`Parent` field? Specifichiamo l'ID/nome esatto.
20. Cosa succede se il ticket in stato `waiting…` ha **più figli** non completati? Si segue il primo? Quello creato per ultimo? Quello con priorità più alta? Il "primo" è instabile. Se il TC ha pi figli metti pi iterazioni del TC, una per ogni HDX in modo da vederli tutti.
21. Profondità max 10 + fallback al ticket di partenza: come si comunica all'utente che è scattato il fallback? Badge "catena troppo profonda"? ok "catena troppo profonda"
22. Caso "figlio diretto in Waiting for reporter" → etichetta "Attesa Cliente / Helpdesk" e niente discesa: e se il **nipote** (livello 3) è quello realmente bloccato sul cliente? Restiamo a livello 2 — confermato? confermo
23. Il leaf risolto fuori dal dataset principale richiede una **chiamata supplementare**: con N TC in stato waiting e medie di 1–2 livelli, sono fino a 2N round-trip extra per ciclo di polling. Sostenibili rispetto al rate limit? sì
24. "⏱ Ngg fermo" sul leaf: il calcolo è giorni nello stato corrente del leaf (`status changed to ...` da changelog) o giorni dall'ultimo commento del leaf? Sono cose diverse. Giorni dall'ultimo commento.

## 5. Filtri, ricerca, ordinamento (§4.2.6)

25. I filtri condizione sono **mutuamente esclusivi**: come si raggiunge "Highest **e** Spoccato"? L'opzione "Multi-condizione (≥2 flag attivi)" copre solo il caso "almeno due", non combinazioni specifiche. È un limite voluto? la condizione è sempre una OR è sufficiente che si verifichi una delle condizioni. A livello di filtri non serve al momento filtrare per questo parametro.
26. Toggle "Nascondi TC in Waiting for reporter": agisce solo sulla tabella o anche sui KPI (in particolare KPI 4 "TC in Attesa Cliente" e KPI 3 media)? Solo sulla tabella
27. Ricerca full-text: lato client (filtra il dataset cached) o lato server (nuova query)? Con cache in-memory dovrebbe essere client. Lato Client.
28. Ordinamento per "Giorni senza Riscontro" mette in cima i ticket col fallback `updated` insieme a quelli con vera ultima risposta: sono numeri **comparabili**? Serve un'icona che distingua i due casi? si, meglio.

## 6. Ruoli e auth (§4.3)

29. Un utente loggato come `ospite` non vede dettaglio nominativo: la segregazione è **lato backend** (l'API non restituisce assignee/reporter) o solo lato UI? Lato UI è bypassabile da DevTools. solo lato UI.
30. L'`amministratore` ha accesso completo + gestione utenti: la gestione utenti vive nello stesso DB locale (riuso pattern Archiva) o è SSO? Il PRD dice riuso, ma chiariamo che gli utenti **non** sono mappati 1:1 con account Atlassian. Usa il locale con il pattern Archiva.
31. Mapping "email login app = email Jira" (rischio R5): è usato per filtrare la "vista personale" dell'agente? Il PRD non descrive una vista per-assegnatario, è in scope? non è in scope.
32. Vista ospite: KPI 7 (SPOCCATO) e KPI 8 (IN ESCALATION) sono mostrati o omessi? Sono aggregati ma in volumi piccoli potrebbero permettere inferenze sui clienti. tienili aggregati.

## 7. Cache, performance, real-time

33. Cache TTL 60s + UI auto-refresh 60s: nel caso peggiore l'utente vede dato vecchio ~120s. Accettabile per "real-time" come da §1? Non è necessario un real time, l'aggiornamento della board va bene ogni 10 minuti (è importatnte che da qualche parte si visualizzato il timestamp dell'ultimo aggiornamento)
34. Cache in-memory: con più repliche su Railway (scaling orizzontale) i poll si moltiplicano e le cache divergono. Per la fase 1 si forza istanza singola? sì
35. Su deploy/restart la cache si svuota: il primo utente loggato post-restart aspetta il primo poll? Serve un fetch eager allo startup? aspetta il primo poll.

## 8. Sicurezza & GDPR

36. API token JSM in env: chi ha permesso di leggere le env Railway? Solo `amministratore` o tutto l'IT? Il PRD parla di "account di servizio" — qual è il fallback se viene revocato (R6)? tutti possono
37. Log applicativi: contengono `key`, `summary`, `assignee`? Sono dati personali (nomi operatori) — retention? no
38. Cookie sessione `httpOnly` + `secure`: SameSite? CSRF protection necessaria solo se la app facesse POST verso JSM (non lo fa) — confermato che non servono token CSRF? confermo

## 9. Edge cases & coerenza interna del PRD

39. §1 dice "ridurre tempi di reazione su incident ad alto impatto" ma §3 UC2 dice "nessuna azione di scrittura sull'app". L'azione di "reazione" è solo *vedere prima* il problema → poi agire in Jira. Confermato che il valore è puramente di **osservabilità**? per ora sì.
40. §4.1 elenca `GET /rest/servicedeskapi/request/{id}/sla` ma il resto del PRD non descrive **come** lo SLA appare in dashboard (badge, KPI, filtro). UC5 cita SLA a rischio: dov'è la colonna o il KPI SLA? Manca la specifica. Aggiungiamo la colonna sla in tabella ticket. per i kpi la valutiamo in futuro.
41. UC5 "Alerting" è marcato opzionale fase 2 ma è citato in §1 come obiettivo. Fase 1 ha solo evidenziazione visiva, niente Teams/email — confermato? confermo
42. §11 criterio "Risoluzione catena `Waiting for son` su almeno 3 casi reali multi-livello": chi fornisce i 3 casi? È un test di accettazione manuale, vogliamo anche test automatici contro un mock JSM? analizzerò direttamente il risultato finale.
43. §10 "Fuori scope: gestione utenti custom (si riutilizza standard auth Archiva)". Ma §4.3 dice che `amministratore` può configurare i ruoli `amministratore/utente/ospite`: la **mappatura ruoli** è gestita dall'admin panel standard Archiva o è specifica di questa app? sovrascrivi con l'attuale gestione standard, non inventiamo cose nuove.
44. Il PRD non menziona **i18n**: tutta la UI è in italiano? Status name di Jira (es. "Waiting for reporter") restano in inglese? si restano in inglese.
45. Schermo da sala (vista ospite su monitor) richiede di essere loggati? Se sì, sessione persistente lunga / kiosk mode previsto? si

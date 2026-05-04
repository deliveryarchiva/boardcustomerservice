// Auth helper standard Archiva — riutilizzato in ogni pagina protetta.
// Token: sessionStorage chiave "archiva_token" (default standard Archiva).
//
// Deroga "kiosk mode" (PRD §4.3, Q&A §9.45): se l'utente seleziona
// "Mantieni accesso" alla login, il token va anche su localStorage per
// sopravvivere alla chiusura del browser (utile per monitor di sala).
// La lettura preferisce sessionStorage; logout svuota entrambi.

const TOKEN_KEY = "archiva_token";
const USER_KEY = "archiva_user";

function getToken() {
  return sessionStorage.getItem(TOKEN_KEY) || localStorage.getItem(TOKEN_KEY);
}

function getCachedUser() {
  try {
    const raw = sessionStorage.getItem(USER_KEY) || localStorage.getItem(USER_KEY);
    return JSON.parse(raw || "null");
  } catch (e) { return null; }
}

function _clearAuthStorage() {
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(USER_KEY);
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}

function authHeaders(jsonBody = true) {
  const h = { "Authorization": "Bearer " + (getToken() || "") };
  if (jsonBody) h["Content-Type"] = "application/json";
  return h;
}

async function apiFetch(url, opts = {}) {
  const isForm = opts.body instanceof FormData;
  const headers = isForm
    ? { "Authorization": "Bearer " + (getToken() || "") }
    : authHeaders(true);
  const r = await fetch(url, { ...opts, headers: { ...headers, ...(opts.headers || {}) } });
  if (r.status === 401) {
    _clearAuthStorage();
    window.location.href = "/login";
    return null;
  }
  return r;
}

async function doLogin(username, password, { keepLoggedIn = false } = {}) {
  const r = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password })
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || data.error || "Credenziali non valide");
  // Pulisci entrambi gli storage prima di salvare per evitare residui di sessioni vecchie.
  _clearAuthStorage();
  const store = keepLoggedIn ? localStorage : sessionStorage;
  store.setItem(TOKEN_KEY, data.token);
  store.setItem(USER_KEY, JSON.stringify(data.user));
  window.location.href = "/";
}

async function doLogout() {
  try {
    await fetch("/api/auth/logout", { method: "POST", headers: authHeaders(false) });
  } catch (e) { /* ignore */ }
  _clearAuthStorage();
  window.location.href = "/login";
}

async function doChangePassword(currentPassword, newPassword) {
  const r = await apiFetch("/api/auth/change-password", {
    method: "POST",
    body: JSON.stringify({ currentPassword, newPassword })
  });
  if (!r) return;
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || "Errore cambio password");
  return data;
}

// Init guard per pagine protette. Restituisce user o redirige a /login.
async function authInit({ requireAdmin = false } = {}) {
  const token = getToken();
  if (!token) { window.location.href = "/login"; return null; }
  let user;
  try {
    const r = await fetch("/api/auth/me", { headers: { "Authorization": "Bearer " + token } });
    if (!r.ok) throw new Error("auth failed");
    const data = await r.json();
    user = data.user;
    // Aggiorna lo storage in cui il token è effettivamente memorizzato.
    const store = sessionStorage.getItem(TOKEN_KEY) ? sessionStorage : localStorage;
    store.setItem(USER_KEY, JSON.stringify(user));
  } catch (e) {
    _clearAuthStorage();
    window.location.href = "/login";
    return null;
  }
  if (requireAdmin && user.ruolo !== "admin") {
    window.location.href = "/";
    return null;
  }
  renderUserChip(user);
  return user;
}

function renderUserChip(user) {
  const chip = document.getElementById("userChip");
  if (!chip) return;
  const initials = (user.nome || user.username || "?")
    .split(/\s+/).map(s => s[0] || "").join("").slice(0, 2).toUpperCase();
  const ruoloLabel = ({ admin: "Amministratore", user: "Utente", ospite: "Ospite" })[user.ruolo] || user.ruolo;
  chip.innerHTML = `
    <div class="avatar">${initials}</div>
    <div class="user-meta">
      <div class="name">${escapeHtml(user.nome || user.username)}</div>
      <div class="role">${ruoloLabel}</div>
    </div>
    <div class="user-menu" id="userMenu">
      <button type="button" id="btnChangePwd">🔑 Cambia password</button>
      <button type="button" id="btnLogout">↩ Esci</button>
    </div>
  `;
  chip.addEventListener("click", (ev) => {
    ev.stopPropagation();
    document.getElementById("userMenu")?.classList.toggle("open");
  });
  document.addEventListener("click", () => {
    document.getElementById("userMenu")?.classList.remove("open");
  });
  document.getElementById("btnLogout")?.addEventListener("click", (ev) => {
    ev.stopPropagation();
    doLogout();
  });
  document.getElementById("btnChangePwd")?.addEventListener("click", (ev) => {
    ev.stopPropagation();
    openChangePasswordDialog();
  });
}

function openChangePasswordDialog() {
  const cur = prompt("Password attuale:");
  if (cur === null) return;
  const nw = prompt("Nuova password (min 8 caratteri):");
  if (nw === null) return;
  doChangePassword(cur, nw)
    .then(() => alert("Password aggiornata."))
    .catch(e => alert("Errore: " + e.message));
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// node_modules/@tauri-apps/api/external/tslib/tslib.es6.js
function __classPrivateFieldGet(receiver, state, kind, f) {
  if (kind === "a" && !f) throw new TypeError("Private accessor was defined without a getter");
  if (typeof state === "function" ? receiver !== state || !f : !state.has(receiver)) throw new TypeError("Cannot read private member from an object whose class did not declare it");
  return kind === "m" ? f : kind === "a" ? f.call(receiver) : f ? f.value : state.get(receiver);
}
function __classPrivateFieldSet(receiver, state, value, kind, f) {
  if (kind === "m") throw new TypeError("Private method is not writable");
  if (kind === "a" && !f) throw new TypeError("Private accessor was defined without a setter");
  if (typeof state === "function" ? receiver !== state || !f : !state.has(receiver)) throw new TypeError("Cannot write private member to an object whose class did not declare it");
  return kind === "a" ? f.call(receiver, value) : f ? f.value = value : state.set(receiver, value), value;
}

// node_modules/@tauri-apps/api/core.js
var _Channel_onmessage;
var _Channel_nextMessageIndex;
var _Channel_pendingMessages;
var _Channel_messageEndIndex;
var _Resource_rid;
var SERIALIZE_TO_IPC_FN = "__TAURI_TO_IPC_KEY__";
function transformCallback(callback, once = false) {
  return window.__TAURI_INTERNALS__.transformCallback(callback, once);
}
var Channel = class {
  constructor(onmessage) {
    _Channel_onmessage.set(this, void 0);
    _Channel_nextMessageIndex.set(this, 0);
    _Channel_pendingMessages.set(this, []);
    _Channel_messageEndIndex.set(this, void 0);
    __classPrivateFieldSet(this, _Channel_onmessage, onmessage || (() => {
    }), "f");
    this.id = transformCallback((rawMessage) => {
      const index = rawMessage.index;
      if ("end" in rawMessage) {
        if (index == __classPrivateFieldGet(this, _Channel_nextMessageIndex, "f")) {
          this.cleanupCallback();
        } else {
          __classPrivateFieldSet(this, _Channel_messageEndIndex, index, "f");
        }
        return;
      }
      const message = rawMessage.message;
      if (index == __classPrivateFieldGet(this, _Channel_nextMessageIndex, "f")) {
        __classPrivateFieldGet(this, _Channel_onmessage, "f").call(this, message);
        __classPrivateFieldSet(this, _Channel_nextMessageIndex, __classPrivateFieldGet(this, _Channel_nextMessageIndex, "f") + 1, "f");
        while (__classPrivateFieldGet(this, _Channel_nextMessageIndex, "f") in __classPrivateFieldGet(this, _Channel_pendingMessages, "f")) {
          const message2 = __classPrivateFieldGet(this, _Channel_pendingMessages, "f")[__classPrivateFieldGet(this, _Channel_nextMessageIndex, "f")];
          __classPrivateFieldGet(this, _Channel_onmessage, "f").call(this, message2);
          delete __classPrivateFieldGet(this, _Channel_pendingMessages, "f")[__classPrivateFieldGet(this, _Channel_nextMessageIndex, "f")];
          __classPrivateFieldSet(this, _Channel_nextMessageIndex, __classPrivateFieldGet(this, _Channel_nextMessageIndex, "f") + 1, "f");
        }
        if (__classPrivateFieldGet(this, _Channel_nextMessageIndex, "f") === __classPrivateFieldGet(this, _Channel_messageEndIndex, "f")) {
          this.cleanupCallback();
        }
      } else {
        __classPrivateFieldGet(this, _Channel_pendingMessages, "f")[index] = message;
      }
    });
  }
  cleanupCallback() {
    window.__TAURI_INTERNALS__.unregisterCallback(this.id);
  }
  set onmessage(handler) {
    __classPrivateFieldSet(this, _Channel_onmessage, handler, "f");
  }
  get onmessage() {
    return __classPrivateFieldGet(this, _Channel_onmessage, "f");
  }
  [(_Channel_onmessage = /* @__PURE__ */ new WeakMap(), _Channel_nextMessageIndex = /* @__PURE__ */ new WeakMap(), _Channel_pendingMessages = /* @__PURE__ */ new WeakMap(), _Channel_messageEndIndex = /* @__PURE__ */ new WeakMap(), SERIALIZE_TO_IPC_FN)]() {
    return `__CHANNEL__:${this.id}`;
  }
  toJSON() {
    return this[SERIALIZE_TO_IPC_FN]();
  }
};
async function invoke(cmd, args = {}, options) {
  return window.__TAURI_INTERNALS__.invoke(cmd, args, options);
}
_Resource_rid = /* @__PURE__ */ new WeakMap();

// frontend/app.js
var current = null;
var tab = "deezer";
var tracks = [];
var pollTimer = null;
var cachedConnection = null;
var $ = (s) => document.querySelector(s);
var esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
var fmtDur = (s) => s ? `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}` : "—";
function isPackagedAppOrigin() {
  return window.location.protocol === "tauri:" || window.location.origin === "http://tauri.localhost";
}
function isValidLoopbackBaseUrl(baseUrl) {
  try {
    const url = new URL(baseUrl);
    return url.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname);
  } catch {
    return false;
  }
}
function validateConnection(raw) {
  if (!raw || !isValidLoopbackBaseUrl(raw.baseUrl) || typeof raw.token !== "string" || raw.token.length === 0) {
    throw new Error("backend connection unavailable");
  }
  return { baseUrl: raw.baseUrl.replace(/\/+$/, ""), token: raw.token };
}
async function getConnection() {
  if (cachedConnection) return cachedConnection;
  const devConnection = globalThis.__DECKPIPE_DEV_CONNECTION__;
  if (devConnection) {
    cachedConnection = validateConnection(devConnection);
    return cachedConnection;
  }
  if (isPackagedAppOrigin()) {
    cachedConnection = validateConnection(await invoke("backend_connection"));
    return cachedConnection;
  }
  cachedConnection = { baseUrl: window.location.origin, token: "" };
  return cachedConnection;
}
async function api(path, opts = {}) {
  const connection = await getConnection();
  const url = new URL(path, connection.baseUrl);
  const method = opts.method || (opts.body !== void 0 ? "POST" : "GET");
  const headers = { Accept: "application/json" };
  if (opts.body !== void 0) headers["Content-Type"] = "application/json";
  if (connection.token) headers.Authorization = `Bearer ${connection.token}`;
  const request = { method, headers };
  if (opts.body !== void 0) request.body = JSON.stringify(opts.body || {});
  const r = await fetch(url.href, request);
  if (!r.ok) throw new Error((await r.json().catch(() => ({ detail: r.statusText }))).detail);
  return r.json();
}
function setHidden(node, hidden) {
  node.classList.toggle("hidden", hidden);
}
function setFlexVisible(node, visible) {
  node.classList.toggle("visible-flex", visible);
  node.classList.toggle("hidden", !visible);
}
function setAuthActive(node, active) {
  node.classList.toggle("auth-active", active);
}
function setResultState(state) {
  const node = $("#loginResult");
  node.classList.toggle("login-ok", state === "ok");
  node.classList.toggle("login-error", state === "error");
}
async function loadConfig() {
  const c = await api("/api/config");
  $("#musicRoot").value = c.music_root;
  $("#wavMode").value = c.wav_mode || "source";
  $("#numbering").checked = !!c.numbering;
  $("#btnLoginDeezer").textContent = c.user && c.user.email ? `Deezer: ${c.user.email}` : "Deezer: вход";
  setAuthActive($("#btnLoginDeezer"), !!(c.user && c.user.email));
  $("#btnLoginSc").textContent = c.sc_user ? `SC: ${c.sc_user}` : "SC: вход";
  setAuthActive($("#btnLoginSc"), !!c.sc_user);
}
var loginService = "deezer";
function openLogin(service) {
  loginService = service;
  if (isPackagedAppOrigin()) {
    tauriLogin(service);
    return;
  }
  $("#loginResult").textContent = "";
  setResultState(null);
  $("#loginToken").value = "";
  setHidden($("#scImport"), true);
  if (service === "deezer") {
    $("#loginTitle").textContent = "Вход в Deezer";
    $("#loginSteps").innerHTML = `<b>Просто email и пароль</b> — как в Saturn:<br>`;
    setHidden($("#loginPasswordBlock"), false);
    $("#loginToken").placeholder = "…или вставьте ARL cookie вручную сюда";
  } else {
    setHidden($("#loginPasswordBlock"), true);
    $("#loginTitle").textContent = "Вход в SoundCloud";
    $("#loginSteps").innerHTML = `<b>Способ 1 — расширение DeckPipe Helper</b> (папка extension/):<br>
      opera://extensions → Режим разработчика → Загрузить распакованное → клик по иконке DeckPipe — вход подхватится из браузера сам.<br>
      <b>Способ 2 — вручную:</b> F12 → Application → Cookies → <b>oauth_token</b> на soundcloud.com → вставить ниже`;
    $("#loginToken").placeholder = "oauth_token cookie";
  }
  setHidden($("#modalOverlay"), false);
  $("#loginToken").focus();
  if (service === "sc" && $("#btnLoginSc").textContent.startsWith("SC: ") && $("#btnLoginSc").classList.contains("auth-active")) {
    setResultState("ok");
    $("#loginResult").textContent = "✔ уже выполнен вход — выберите, что импортировать";
    loadScAccount();
  }
}
function closeLogin() {
  setHidden($("#modalOverlay"), true);
}
async function tauriLogin(service) {
  try {
    const token = await invoke("service_login", { service });
    const url = service === "deezer" ? "/api/login/deezer" : "/api/login/soundcloud";
    const body = service === "deezer" ? { arl: token } : { oauth_token: token };
    const r = await api(url, { body });
    alert(`✔ Вход выполнен: ${r.email || r.username}`);
    loadConfig();
    if (service === "sc") {
      switchTab("sc");
    } else {
      loadPlaylists();
    }
  } catch (e) {
    if (!String(e).includes("закрыто")) alert("Логин не удался: " + e);
  }
}
async function doLogin() {
  const token = $("#loginToken").value.trim();
  const email = $("#loginEmail").value.trim();
  const password = $("#loginPassword").value;
  let url, body;
  if (loginService === "deezer") {
    if (email && password) {
      url = "/api/login/deezer/password";
      body = { email, password };
    } else if (token) {
      url = "/api/login/deezer";
      body = { arl: token };
    } else return;
  } else {
    if (!token) return;
    url = "/api/login/soundcloud";
    body = { oauth_token: token };
  }
  try {
    const r = await api(url, { body });
    setResultState("ok");
    $("#loginResult").textContent = loginService === "deezer" ? `✔ ${r.email}` : `✔ ${r.username}`;
    loadConfig();
    if (loginService === "sc") loadScAccount();
    else {
      closeLogin();
      loadPlaylists();
    }
  } catch (e) {
    setResultState("error");
    $("#loginResult").textContent = "✖ " + e.message;
  }
}
async function loadScAccount() {
  try {
    const d = await api("/api/sc/account");
    const items = [d.likes, ...d.playlists];
    $("#scAccountList").innerHTML = items.map((p) => `
      <label class="scacc"><input type="checkbox" class="scacc-cb"
        data-id="${p.id}" data-title="${esc(p.title)}" data-url="${esc(p.url)}" data-count="${p.count || 0}">
        ${esc(p.title)} <span class="dim">(${p.count ?? "?"})</span></label>`).join("");
    setHidden($("#scImport"), false);
  } catch (e) {
    setResultState("error");
    $("#loginResult").textContent = "аккаунт не загрузился: " + e.message;
  }
}
async function importScAccount() {
  const items = [...document.querySelectorAll(".scacc-cb:checked")].map((c) => ({
    id: c.dataset.id,
    title: c.dataset.title,
    url: c.dataset.url,
    count: +c.dataset.count
  }));
  if (!items.length) return;
  const r = await api("/api/sc/account/import", { body: { items } });
  alert(`Импортировано: ${r.added}`);
  closeLogin();
  switchTab("sc");
}
async function saveWavMode() {
  await api("/api/config", { body: { wav_mode: $("#wavMode").value } });
}
async function saveNumbering() {
  await api("/api/config", { body: { numbering: $("#numbering").checked } });
}
async function sendReport() {
  const text = prompt("Опишите проблему (что делали, что ожидали, что произошло):");
  if (!text) return;
  try {
    await api("/api/report", { body: { text, current: current ? current.title : tab } });
    alert("Репорт отправлен, спасибо!");
  } catch (e) {
    alert("Не отправилось: " + e.message);
  }
}
async function saveRoot() {
  await api("/api/config", { body: { music_root: $("#musicRoot").value } });
  loadPlaylists();
  if (current) loadTracks(current);
}
async function loadPlaylists() {
  if (tab === "sc") return loadScSources();
  if (tab === "errors") return loadErrors();
  if (tab === "search") return loadSearchTargets();
  const pls = await api("/api/playlists");
  $("#playlists").innerHTML = pls.map((p) => `
    <div class="pl ${current && current.id === p.id ? "active" : ""}" data-action="select-playlist" data-id="${esc(p.id)}" data-title="${esc(p.title)}">
      ${p.cover ? `<img src="${p.cover}">` : '<img alt="">'}
      <div class="pl-body">
        <div class="t">${esc(p.title)}</div>
        <div class="c">${p.ok || 0}/${p.count} ${p.errors ? `<span class="badge-err">⚠ ${p.errors}</span>` : ""}</div>
      </div>
    </div>`).join("");
}
async function loadErrors() {
  const errs = await api("/api/errors");
  $("#playlists").innerHTML = errs.length ? `
    <div class="list-pad"><button class="full-width" data-action="retry-all">Повторить все (${errs.length})</button></div>
    ` + errs.map((e, i) => `
    <div class="pl pl-static">
      <div class="pl-fill">
        <div class="t">${esc(e.track.title)}</div>
        <div class="c">${esc(e.playlist_title)} · ${esc(e.track.artist)}</div>
        <div class="badge-err">${esc(e.error)}</div>
      </div>
      <button class="ghost button-small" data-action="retry-one" data-index="${i}">↻</button>
    </div>`).join("") : '<div id="empty">Ошибок нет 🎉</div>';
  window._errors = errs;
}
async function retryOne(i) {
  const e = window._errors[i];
  await api("/api/errors/retry", { body: { playlist_key: e.playlist_key, playlist_title: e.playlist_title, track: e.track } });
  startPolling();
}
async function retryAll() {
  for (const e of window._errors || []) {
    await api("/api/errors/retry", { body: { playlist_key: e.playlist_key, playlist_title: e.playlist_title, track: e.track } });
  }
  startPolling();
}
function switchTab(t) {
  tab = t;
  $("#tab-deezer").classList.toggle("active", t === "deezer");
  $("#tab-sc").classList.toggle("active", t === "sc");
  $("#tab-search").classList.toggle("active", t === "search");
  $("#tab-errors").classList.toggle("active", t === "errors");
  setFlexVisible($("#sc-add"), t === "sc");
  setFlexVisible($("#searchbar"), t === "search");
  setFlexVisible($("#searchFilters"), t === "search");
  current = null;
  setFlexVisible($("#toolbar"), false);
  if (t === "search") {
    $("#tracks").innerHTML = '<div id="empty">Слева — цель (плейлист Deezer / источник SC / локальный плейлист / своя папка).<br>Ищите треки, отмечайте чекбоксами или качайте по одному. Альбомы и сеты раскрываются по клику — внутри треки качаются поштучно или все сразу.</div>';
    setSearchFilter(searchFilter);
    loadSearchTargets();
    return;
  }
  $("#tracks").innerHTML = t === "errors" ? '<div id="empty">Треки с ошибками загрузки/верификации — слева. Кнопка ↻ перезапускает сломавшийся этап.</div>' : '<div id="empty">Выберите ' + (t === "sc" ? "источник" : "плейлист") + " слева</div>";
  loadPlaylists();
}
var searchTarget = null;
var searchFilter = "all";
var searchSel = {};
var searchExpanded = {};
var provLabel = (p) => ({ deezer: "Deezer", sc: "SoundCloud", local: "Локальный", dir: "Папка" })[p] || p;
async function loadSearchTargets() {
  const [dz, sc, loc] = await Promise.all([
    api("/api/playlists"),
    api("/api/sc/sources"),
    api("/api/local/playlists")
  ]);
  const rows = [
    ...dz.map((p) => ({ key: p.id, title: p.title, provider: "deezer", count: p.count })),
    ...sc.map((s) => ({ key: "sc:" + s.id, title: s.title, provider: "sc", count: s.count })),
    ...loc.map((s) => ({ key: s.key, title: s.title, provider: "local", count: s.count }))
  ];
  if (!searchTarget && rows.length) searchTarget = rows[0];
  window._targets = rows;
  const cur = searchTarget ? `${provLabel(searchTarget.provider)} · ${esc(searchTarget.title)}` : "не выбрана";
  $("#playlists").innerHTML = `
    <div id="targetBox">
      <div class="cap">Цель загрузки / добавления</div>
      <div class="cur" title="${cur}">${cur}</div>
      <div class="acts">
        <button class="ghost" data-action="create-target-playlist" data-kind="deezer" title="Новый плейлист в Deezer">＋Deezer</button>
        <button class="ghost" data-action="create-target-playlist" data-kind="local" title="Новый локальный плейлист (SoundCloud не даёт создавать плейлисты через API — создаётся локальная папка-плейлист, URL можно привязать позже)">＋Локальный (SC)</button>
        <button class="ghost" data-action="choose-custom-dir" title="Скачивать в произвольную папку">📁 Папка…</button>
      </div>
    </div>` + rows.map((r, i) => `
    <div class="pl ${searchTarget && searchTarget.key === r.key ? "active" : ""}" data-action="set-search-target" data-index="${i}">
      <img alt="">
      <div class="pl-body">
        <div class="t">${esc(r.title)}</div>
        <div class="c">${provLabel(r.provider)} · ${r.count ?? "?"}</div>
      </div>
    </div>`).join("");
}
function setSearchTarget(i) {
  searchTarget = window._targets[i];
  loadSearchTargets();
}
async function chooseCustomDir() {
  const r = await api("/api/browse");
  if (!r.path) return;
  searchTarget = { key: "", title: r.path, provider: "dir", dir: r.path };
  loadSearchTargets();
}
async function createTargetPlaylist(kind) {
  if (kind === "deezer") {
    const title = prompt("Название нового плейлиста в Deezer:");
    if (!title) return;
    const r = await api("/api/deezer/playlist/create", { body: { title, track_ids: [] } });
    searchTarget = { key: r.id, title: r.title, provider: "deezer" };
  } else {
    const title = prompt("Название локального плейлиста (папка в библиотеке; в SoundCloud плейлисты через API создавать нельзя):");
    if (!title) return;
    const r = await api("/api/local/playlists", { body: { title } });
    searchTarget = { key: r.key, title: r.title, provider: "local" };
  }
  loadSearchTargets();
}
function setSearchFilter(f) {
  searchFilter = f;
  document.querySelectorAll("#searchFilters .chip").forEach((c) => c.classList.toggle("active", c.dataset.f === f));
  if (window._search) renderSearch();
}
async function runSearch() {
  const q = $("#searchInput").value.trim();
  if (!q) return;
  searchSel = {};
  searchExpanded = {};
  $("#tracks").innerHTML = '<div id="empty">Ищу…</div>';
  try {
    const d = await api(`/api/search?q=${encodeURIComponent(q)}&service=${$("#searchService").value}`);
    window._search = d;
    setSearchFilter(searchFilter);
  } catch (e) {
    $("#tracks").innerHTML = `<div id="empty">⚠ ${esc(e.message)}</div>`;
  }
}
function _selKey(svc, i) {
  return `${svc}:${i}`;
}
function _trackCb(svc, i, t) {
  const k = _selKey(svc, i);
  if (searchSel[k]) delete searchSel[k];
  else searchSel[k] = t;
  document.getElementById("cb-" + svc + "-" + i).checked = !!searchSel[k];
  _renderBasket();
}
function _renderBasket() {
  const n = Object.keys(searchSel).length;
  let b = document.getElementById("basket");
  if (!n) {
    if (b) b.remove();
    return;
  }
  if (!b) {
    b = document.createElement("div");
    b.id = "basket";
    $("#tracks").appendChild(b);
  }
  const tgt = searchTarget ? `${provLabel(searchTarget.provider)} · ${esc(searchTarget.title)}` : "—";
  b.innerHTML = `<b>${n} тр.</b>
    <button data-action="dl-basket">⬇ в цель: ${tgt}</button>
    <button class="ghost" data-action="clear-basket">✕ очистить</button>`;
}
function _trackRow(svc, i, t, indent) {
  const checked = searchSel[_selKey(svc, i)] ? "checked" : "";
  return `<div class="srow">
    <input type="checkbox" class="cb" id="cb-${svc}-${i}" ${checked} data-action="track-checkbox" data-service="${svc}" data-index="${i}">
    <div class="tt" title="${esc(t.title)} — ${esc(t.artist)}">${esc(t.title)} <span class="meta">${esc(t.artist)} · ${fmtDur(t.duration)}</span></div>
    <span class="prov ${svc === "sc" ? "sc" : ""}">${svc === "sc" ? "SC" : "DZ"}</span>
    ${svc === "deezer" ? `<button class="ghost" title="Добавить в плейлист Deezer без скачивания" data-action="add-dz-track" data-index="${i}">＋</button>` : ""}
    <button title="Скачать в цель" data-action="dl-search-track" data-service="${svc}" data-index="${i}">⬇</button>
  </div>`;
}
function _albumRow(svc, i, a) {
  const k = svc + ":" + i;
  const exp = searchExpanded[k];
  const arrow = exp && exp.tracks ? "▾" : "▸";
  let html = `<div class="srow exp" data-action="toggle-album" data-service="${svc}" data-index="${i}">
    <div class="tt">${arrow} 💿 ${esc(a.title)} <span class="meta">${esc(a.artist)}${a.count ? ` · ${a.count} тр.` : ""}</span></div>
    <span class="prov ${svc === "sc" ? "sc" : ""}">${svc === "sc" ? "SC" : "DZ"}</span>
  </div>`;
  if (exp) {
    if (exp.loading) html += `<div class="stracks"><div class="srow dim">Загружаю треки…</div></div>`;
    else if (exp.error) html += `<div class="stracks"><div class="srow errtext">⚠ ${esc(exp.error)}</div></div>`;
    else if (exp.tracks) {
      html += `<div class="stracks">` + exp.tracks.map((t, j) => `
        <div class="srow">
          <div class="tt" title="${esc(t.title)} — ${esc(t.artist)}">${esc(t.title)} <span class="meta">${esc(t.artist)} · ${fmtDur(t.duration)}</span></div>
          <button title="Скачать в цель" data-action="dl-album-track" data-service="${svc}" data-index="${i}" data-track-index="${j}">⬇</button>
        </div>`).join("") + `<div class="srow"><div class="tt dim">${exp.tracks.length} тр.</div>
          <button data-action="dl-whole-album" data-service="${svc}" data-index="${i}">⬇ все</button></div></div>`;
    }
  }
  return html;
}
async function toggleAlbum(svc, i) {
  const k = svc + ":" + i;
  if (searchExpanded[k] && !searchExpanded[k].loading) {
    delete searchExpanded[k];
    renderSearch();
    return;
  }
  searchExpanded[k] = { loading: true };
  renderSearch();
  try {
    const a = window._search[svc].albums[i];
    searchExpanded[k] = { tracks: svc === "deezer" ? await api(`/api/deezer/album/${a.id}`) : await api(`/api/sc/resolve-tracks?url=${encodeURIComponent(a.url)}`) };
  } catch (e) {
    searchExpanded[k] = { error: e.message };
  }
  renderSearch();
}
function renderSearch() {
  const d = window._search || {};
  const f = searchFilter;
  let html = "";
  const sections = [];
  for (const [svc, label] of [["deezer", "Deezer"], ["sc", "SoundCloud"]]) {
    const s = d[svc];
    if (!s) continue;
    if ((f === "all" || f === "tracks") && s.tracks.length)
      sections.push([`${label} — треки`, s.tracks.map((t, i) => _trackRow(svc, i, t)).join("")]);
    const isPl = f === "playlists";
    if ((f === "all" || f === "albums" || isPl) && s.albums.length) {
      const kind = svc === "deezer" ? "альбомы" : "плейлисты и сеты";
      if (!(isPl && svc === "deezer"))
        sections.push([`${label} — ${kind}`, s.albums.map((a, i) => _albumRow(svc, i, a)).join("")]);
    }
    if ((f === "all" || f === "artists") && s.artists.length)
      sections.push([
        `${label} — исполнители`,
        s.artists.map((a) => `<div class="srow"><div class="tt">👤 ${esc(a.name)}</div></div>`).join("")
      ]);
  }
  html = sections.map(([t, rows]) => `<div class="search-sec">${t}</div>${rows}`).join("");
  $("#tracks").innerHTML = html || '<div id="empty">Ничего не найдено</div>';
  _renderBasket();
}
function _dlPayload(tracks2) {
  if (!searchTarget) {
    alert("Слева выберите цель (плейлист или папку)");
    return null;
  }
  if (searchTarget.provider === "dir")
    return { target_key: "dir", target_title: searchTarget.title, target_dir: searchTarget.dir, tracks: tracks2 };
  return { target_key: searchTarget.key, target_title: searchTarget.title, tracks: tracks2 };
}
async function dlSearchTrack(svc, i) {
  const t = window._search[svc].tracks[i];
  const body = _dlPayload([{ id: t.id, title: t.title, artist: t.artist, duration: t.duration, url: t.url, provider: svc }]);
  if (!body) return;
  const r = await api("/api/search/download", { body });
  if (r.added_to_deezer) console.log(`+${r.added_to_deezer} в плейлист Deezer`);
  startPolling();
}
async function dlAlbumTrack(svc, i, j) {
  const t = searchExpanded[svc + ":" + i].tracks[j];
  const body = _dlPayload([{ id: t.id, title: t.title, artist: t.artist, duration: t.duration, url: t.url, provider: svc }]);
  if (!body) return;
  await api("/api/search/download", { body });
  startPolling();
}
async function dlWholeAlbum(svc, i) {
  const a = window._search[svc].albums[i];
  const tracks2 = searchExpanded[svc + ":" + i].tracks;
  const body = _dlPayload(tracks2.map((t) => ({ id: t.id, title: t.title, artist: t.artist, duration: t.duration, url: t.url, provider: svc })));
  if (!body) return;
  await api("/api/search/download", { body });
  alert(`«${a.title}» поставлен в очередь (${tracks2.length} треков)`);
  startPolling();
}
async function dlBasket() {
  const tracks2 = Object.values(searchSel).map((t) => ({ id: t.id, title: t.title, artist: t.artist, duration: t.duration, url: t.url, provider: t.provider }));
  const body = _dlPayload(tracks2);
  if (!body) return;
  await api("/api/search/download", { body });
  alert(`Поставлено в очередь: ${tracks2.length} тр.`);
  searchSel = {};
  _renderBasket();
  renderSearch();
  startPolling();
}
async function addDzTrack(i) {
  if (!searchTarget || searchTarget.provider !== "deezer") {
    return alert("Выберите слева плейлист Deezer как цель");
  }
  const t = window._search.deezer.tracks[i];
  const r = await api("/api/deezer/playlist/add", { body: { playlist_id: searchTarget.key, track_ids: [t.id] } });
  alert(`«${t.title}» добавлен в «${searchTarget.title}»`);
}
async function loadScSources() {
  try {
    await api("/api/sc/sync-account", { body: {} });
  } catch (e) {
  }
  const srcs = await api("/api/sc/sources");
  $("#playlists").innerHTML = srcs.map((s) => `
    <div class="pl ${current && current.id === s.id ? "active" : ""}" data-action="select-sc-source" data-id="${esc(s.id)}" data-title="${esc(s.title)}">
      <img alt="">
      <div class="pl-body">
        <div class="t">${esc(s.title)}</div>
        <div class="c">${s.ok || 0}/${s.count ?? "?"} ${s.errors ? `<span class="badge-err">⚠ ${s.errors}</span>` : ""}</div>
      </div>
    </div>`).join("") || '<div id="empty">Войдите через «SC: вход» — плейлисты аккаунта появятся сами.<br>Или добавьте URL выше.</div>';
}
async function addScSource() {
  const url = $("#scUrl").value.trim();
  if (!url) return;
  $("#scUrl").value = "";
  try {
    await api("/api/sc/sources", { body: { url } });
  } catch (e) {
    alert(e.message);
  }
  loadPlaylists();
}
function selectScSource(id, title) {
  current = { kind: "sc", id, title };
  loadPlaylists();
  loadTracks(current);
}
function selectPlaylist(id, title) {
  current = { kind: "deezer", id, title };
  loadPlaylists();
  loadTracks(current);
}
async function loadTracks(pl) {
  setFlexVisible($("#toolbar"), true);
  $("#pltitle").textContent = pl.title;
  $("#tracks").innerHTML = '<div id="empty">Загрузка… (большие плейлисты — до ~20 сек)</div>';
  try {
    const url = pl.kind === "sc" ? `/api/sc/sources/${pl.id}/tracks` : `/api/playlists/${pl.id}/tracks?title=${encodeURIComponent(pl.title)}`;
    const d = await api(url);
    tracks = d.tracks;
    $("#plpath").textContent = d.path;
    renderTracks();
  } catch (e) {
    $("#tracks").innerHTML = `<div id="empty">⚠ Не удалось загрузить: ${esc(e.message)}<br><br>
      <button class="ghost" data-action="rescan">Повторить</button></div>`;
  }
}
function renderTracks() {
  const icon = { ok: '<span class="st ok">✔</span>', error: '<span class="st err">⚠</span>', missing: '<span class="st miss">✖</span>' };
  const ok = tracks.filter((t) => t.status === "ok").length;
  const err = tracks.filter((t) => t.status === "error").length;
  const miss = tracks.length - ok - err;
  $("#plstats").textContent = `✔ ${ok} · ✖ ${miss} · ⚠ ${err}`;
  const flipped = tracks.filter((t) => t.flipped).length;
  $("#flipBtn").textContent = flipped ? `⇄ FLAC (${flipped} в WAV)` : "⇄ WAV";
  $("#tracks").innerHTML = `<table>
    <tr><th><input type="checkbox" id="all" data-action="toggle-all"></th>
        <th></th><th>Название</th><th>Исполнитель</th><th>Альбом</th><th>⏱</th><th>Формат</th></tr>
    ${tracks.map((t, i) => `
    <tr>
      <td><input type="checkbox" class="trk" data-i="${i}" ${t.status === "ok" ? "disabled" : ""}></td>
      <td>${icon[t.status] || ""}</td>
      <td>${esc(t.title)}${t.error ? `<div class="errtext">${esc(t.error)}</div>` : ""}</td>
      <td>${esc(t.artist)}</td><td class="dim">${esc(t.album)}</td>
      <td class="dim">${fmtDur(t.duration)}</td>
      <td>${t.format ? `<span class="fmt ${t.format}">${t.format}</span>` : ""}${t.flipped ? '<span class="fmt fmt-wav">→wav</span>' : ""}${t.mp3_source ? '<span class="fmt fmt-mp3src" title="mp3-источник: после конвертации в WAV кью могут сместиться на ~26 мс">mp3</span>' : ""}</td>
    </tr>`).join("")}
  </table>`;
}
function toggleAll(v) {
  document.querySelectorAll(".trk:not(:disabled)").forEach((c) => c.checked = v);
}
function selectedTracks(onlyMissing = false) {
  const idxs = [...document.querySelectorAll(".trk:checked")].map((c) => +c.dataset.i);
  let sel = idxs.map((i) => tracks[i]);
  if (onlyMissing) sel = sel.filter((t) => t.status !== "ok");
  return sel.map((t) => ({ id: t.id, title: t.title, artist: t.artist, duration: t.duration, url: t.url, total: tracks.length }));
}
function downloadUrl(mode) {
  const base = current.kind === "sc" ? `/api/sc/sources/${current.id}/download` : `/api/playlists/${current.id}/download?title=${encodeURIComponent(current.title)}`;
  return base + (base.includes("?") ? "&" : "?") + "mode=" + mode;
}
async function downloadSelected() {
  const sel = selectedTracks(true);
  if (!sel.length) return alert("Ничего не выбрано (уже скачанные пропускаются)");
  await api(downloadUrl("append"), { body: { tracks: sel } });
  startPolling();
}
async function syncPlaylistOrder() {
  const order = tracks.map((t) => t.id);
  const key = current.kind === "sc" ? "sc:" + current.id : current.id;
  const rn = await api(
    `/api/playlists/${key}/renumber?title=${encodeURIComponent(current.title)}`,
    { body: { order, total: tracks.length } }
  );
  const missing = tracks.filter((t) => t.status !== "ok").map((t) => ({
    id: t.id,
    title: t.title,
    artist: t.artist,
    duration: t.duration,
    url: t.url,
    position: tracks.indexOf(t) + 1,
    total: tracks.length
  }));
  if (!missing.length) {
    alert(`Порядок применён (переименовано: ${rn.renamed}). Всё уже скачано.`);
    rescan();
    return;
  }
  if (!confirm(`Перенумеровано: ${rn.renamed}. Скачать ${missing.length} треков?`)) {
    rescan();
    return;
  }
  await api(downloadUrl("playlist_order"), { body: { tracks: missing } });
  startPolling();
}
async function syncAppend() {
  const missing = tracks.filter((t) => t.status !== "ok").map((t) => ({ id: t.id, title: t.title, artist: t.artist, duration: t.duration, url: t.url, total: tracks.length }));
  if (!missing.length) return alert("Всё уже скачано");
  if (!confirm(`Скачать ${missing.length} треков (новые — вниз списка)?`)) return;
  await api(downloadUrl("append"), { body: { tracks: missing } });
  startPolling();
}
async function rbSync() {
  const key = current.kind === "sc" ? "sc:" + current.id : current.id;
  if (!confirm(`Синхронизировать «${current.title}» в Rekordbox?
Rekordbox должен быть ЗАКРЫТ. Бэкап master.db будет создан автоматически.`)) return;
  try {
    const r = await api("/api/rb/sync", { body: { playlist_key: key, playlist_title: current.title } });
    alert(`Готово: плейлист «${r.playlist}»
новых треков в коллекции: ${r.added_content}
добавлено в плейлист: ${r.added_to_playlist}

${r.note}`);
  } catch (e) {
    alert("⚠ " + e.message);
  }
}
async function flipWav() {
  const flipped = tracks.filter((t) => t.flipped).length;
  const to_wav = flipped === 0;
  const msg = to_wav ? `Конвертировать «${current.title}» в WAV для CDJ?
Все кью, сетка, BPM и порядок сохранятся (пути в master.db переключатся на WAV).
Rekordbox должен быть ЗАКРЫТ.` : `Вернуть «${current.title}» к исходникам (FLAC/MP3)?
Пути в master.db переключатся обратно. Rekordbox должен быть ЗАКРЫТ.`;
  if (!confirm(msg)) return;
  const key = current.kind === "sc" ? "sc:" + current.id : current.id;
  await api("/api/flip", { body: { playlist_key: key, playlist_title: current.title, to_wav } });
  startPolling();
}
async function bindPath() {
  let p = null, browseFailed = false;
  try {
    const b = await api("/api/browse");
    p = b.path;
  } catch (e) {
    browseFailed = true;
  }
  if (!p && browseFailed) {
    p = prompt("Папка для этого плейлиста (например на флешке E:\\Playlists\\Set):", $("#plpath").textContent);
  }
  if (!p) return;
  const key = current.kind === "sc" ? "sc:" + current.id : current.id;
  await api(`/api/playlists/${key}/bind`, { body: { path: p } });
  await loadTracks(current);
  loadPlaylists();
}
async function rescan() {
  if (!current) return;
  await loadTracks(current);
  loadPlaylists();
}
function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    const js = await api("/api/jobs");
    const active = js.filter((j) => j.state !== "done");
    $("#jobs").innerHTML = js.slice(0, 5).map((j) => `
      <div class="job"><b>${esc(j.title)}</b>: ${j.done}/${j.total}
        ${j.failed ? `<span class="err">(ошибок: ${j.failed})</span>` : ""}
        ${j.current ? `<span class="dim"> — ${esc(j.current)}</span>` : ""}
        <progress class="bar" max="100" value="${j.total ? 100 * j.done / j.total : 0}"></progress>
      </div>`).join("");
    if (!active.length) {
      clearInterval(pollTimer);
      pollTimer = null;
      if (current) loadTracks(current);
      loadPlaylists();
    }
  }, 1500);
}
function clearBasket() {
  searchSel = {};
  _renderBasket();
  renderSearch();
}
var clickActions = Object.freeze({
  "open-login": (el) => openLogin(el.dataset.service),
  "save-root": () => saveRoot(),
  "send-report": () => sendReport(),
  "switch-tab": (el) => switchTab(el.dataset.tab),
  "add-sc-source": () => addScSource(),
  "rescan": () => rescan(),
  "bind-path": () => bindPath(),
  "download-selected": () => downloadSelected(),
  "sync-playlist-order": () => syncPlaylistOrder(),
  "sync-append": () => syncAppend(),
  "rb-sync": () => rbSync(),
  "flip-wav": () => flipWav(),
  "run-search": () => runSearch(),
  "set-search-filter": (el) => setSearchFilter(el.dataset.filter),
  "overlay-close": (el, event) => {
    if (event.target === el) closeLogin();
  },
  "close-login": () => closeLogin(),
  "do-login": () => doLogin(),
  "import-sc-account": () => importScAccount(),
  "select-playlist": (el) => selectPlaylist(el.dataset.id, el.dataset.title),
  "select-sc-source": (el) => selectScSource(el.dataset.id, el.dataset.title),
  "retry-all": () => retryAll(),
  "retry-one": (el) => retryOne(Number(el.dataset.index)),
  "create-target-playlist": (el) => createTargetPlaylist(el.dataset.kind),
  "choose-custom-dir": () => chooseCustomDir(),
  "set-search-target": (el) => setSearchTarget(Number(el.dataset.index)),
  "dl-basket": () => dlBasket(),
  "clear-basket": () => clearBasket(),
  "add-dz-track": (el) => addDzTrack(Number(el.dataset.index)),
  "dl-search-track": (el) => dlSearchTrack(el.dataset.service, Number(el.dataset.index)),
  "toggle-album": (el) => toggleAlbum(el.dataset.service, Number(el.dataset.index)),
  "dl-album-track": (el) => dlAlbumTrack(el.dataset.service, Number(el.dataset.index), Number(el.dataset.trackIndex)),
  "dl-whole-album": (el) => dlWholeAlbum(el.dataset.service, Number(el.dataset.index))
});
var changeActions = Object.freeze({
  "save-wav-mode": () => saveWavMode(),
  "save-numbering": () => saveNumbering(),
  "toggle-all": (el) => toggleAll(el.checked),
  "track-checkbox": (el) => _trackCb(el.dataset.service, Number(el.dataset.index), window._search[el.dataset.service].tracks[Number(el.dataset.index)])
});
document.addEventListener("click", (event) => {
  const el = event.target.closest("[data-action]");
  if (!el) return;
  const action = clickActions[el.dataset.action];
  if (!action) return;
  if (el.dataset.action === "overlay-close" && event.target !== el) return;
  event.preventDefault();
  action(el, event);
});
document.addEventListener("change", (event) => {
  const el = event.target.closest("[data-action]");
  if (!el) return;
  const action = changeActions[el.dataset.action];
  if (!action) return;
  action(el, event);
});
$("#searchInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter") runSearch();
});
loadConfig();
loadPlaylists();

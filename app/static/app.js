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
var loginService = "deezer";
var previousFocus = null;
var searchTarget = null;
var searchFilter = "all";
var searchSel = {};
var searchExpanded = {};
var $ = (s) => document.querySelector(s);
var RB_APPLY_CONFIRMATION_TOKEN = "APPLY_REKORDBOX_CHANGES";
var FORMAT_CLASS_BY_VALUE = Object.freeze({
  aac: "fmt-aac",
  aiff: "fmt-aiff",
  alac: "fmt-alac",
  flac: "fmt-flac",
  m4a: "fmt-m4a",
  mp3: "fmt-mp3",
  ogg: "fmt-ogg",
  opus: "fmt-opus",
  wav: "fmt-wav"
});
var ERROR_KIND_LABELS = Object.freeze({
  local: "Локальная служба",
  security: "Защита API",
  state: "Состояние библиотеки",
  rekordbox: "Rekordbox",
  dryRun: "Dry-run",
  apply: "Apply",
  reconcile: "Reconcile",
  generic: "Ошибка"
});
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
  throw new Error("backend connection unavailable");
}
async function api(path, opts = {}) {
  const connection = await getConnection();
  const url = new URL(path, connection.baseUrl);
  const method = opts.method || (opts.body !== void 0 ? "POST" : "GET");
  const headers = { Accept: "application/json" };
  if (opts.body !== void 0) headers["Content-Type"] = "application/json";
  headers.Authorization = `Bearer ${connection.token}`;
  const request = { method, headers };
  if (opts.body !== void 0) request.body = JSON.stringify(opts.body || {});
  let response;
  try {
    response = await fetch(url.href, request);
  } catch (error) {
    throw Object.assign(new Error("Локальная служба не отвечает. Запустите DeckPipe заново и повторите действие."), {
      kind: "local",
      cause: error
    });
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: response.statusText }));
    const detail = payload.detail || response.statusText || `HTTP ${response.status}`;
    throw Object.assign(new Error(detail), {
      kind: classifyApiFailure(path, response.status, detail),
      status: response.status,
      payload
    });
  }
  return response.json();
}
function classifyApiFailure(path, status, detail) {
  const textValue = String(detail || "").toLowerCase();
  if (status === 401 || status === 403 || textValue.includes("token") || textValue.includes("authorization")) return "security";
  if (path.includes("/api/rb") || textValue.includes("rekordbox")) return "rekordbox";
  if (textValue.includes("dry-run") || textValue.includes("dry run")) return "dryRun";
  if (textValue.includes("apply")) return "apply";
  if (textValue.includes("reconcile")) return "reconcile";
  if (status === 409 || status === 423 || textValue.includes("state") || textValue.includes("lock")) return "state";
  return "generic";
}
function describeError(error, fallbackKind = "generic") {
  const kind = error && error.kind ? error.kind : fallbackKind;
  const label = ERROR_KIND_LABELS[kind] || ERROR_KIND_LABELS.generic;
  const message = error && error.message ? error.message : String(error || "неизвестная ошибка");
  return `${label}: ${message}`;
}
function showStatus(message) {
  const region = $("#statusRegion");
  region.textContent = message || "";
  region.classList.toggle("hidden", !message);
}
function showError(error, fallbackKind) {
  const region = $("#errorRegion");
  region.textContent = describeError(error, fallbackKind);
  region.classList.remove("hidden");
}
function clearError() {
  const region = $("#errorRegion");
  region.textContent = "";
  region.classList.add("hidden");
}
function text(value) {
  return document.createTextNode(String(value ?? ""));
}
function create(tag, options = {}, children = []) {
  const element = document.createElement(tag);
  if (options.id) element.id = options.id;
  if (options.className) element.className = options.className;
  if (options.type) element.type = options.type;
  if (options.text !== void 0) element.textContent = String(options.text);
  if (options.title) element.title = options.title;
  if (options.placeholder !== void 0) element.placeholder = options.placeholder;
  if (options.value !== void 0) element.value = options.value;
  if (options.checked !== void 0) element.checked = !!options.checked;
  if (options.disabled !== void 0) element.disabled = !!options.disabled;
  if (options.action) element.dataset.action = options.action;
  if (options.dataset) {
    for (const [key, value] of Object.entries(options.dataset)) element.dataset[key] = String(value ?? "");
  }
  if (options.attrs) {
    for (const [key, value] of Object.entries(options.attrs)) {
      if (value !== null && value !== void 0) element.setAttribute(key, String(value));
    }
  }
  for (const child of Array.isArray(children) ? children : [children]) {
    if (child !== null && child !== void 0) element.append(child);
  }
  return element;
}
function button(label, action, options = {}) {
  return create("button", {
    id: options.id,
    className: options.className || "",
    type: "button",
    text: label,
    title: options.title,
    action,
    dataset: options.dataset,
    attrs: options.attrs
  });
}
function replaceChildren(node, children) {
  node.replaceChildren(...Array.isArray(children) ? children : [children]);
}
function setEmpty(message, actionLabel, action) {
  const children = [create("div", { className: "empty-title", text: message })];
  if (actionLabel && action) children.push(button(actionLabel, action, { className: "ghost" }));
  replaceChildren($("#tracks"), create("div", { id: "empty" }, children));
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
function fmtDur(seconds) {
  return seconds ? `${Math.floor(seconds / 60)}:${String(Math.round(seconds % 60)).padStart(2, "0")}` : "—";
}
function provLabel(provider) {
  return { deezer: "Deezer", sc: "SoundCloud", local: "Локальный", dir: "Папка" }[provider] || provider;
}
function safeCoverUrl(value) {
  try {
    const url = new URL(value);
    if (url.protocol === "https:") return url.href;
  } catch {
  }
  return "";
}
function progressValue(done, total) {
  const numericDone = Number(done) || 0;
  const numericTotal = Number(total) || 0;
  if (numericTotal <= 0) return "0";
  return String(Math.max(0, Math.min(100, 100 * numericDone / numericTotal)));
}
function formatBadge(format) {
  const label = String(format ?? "").trim();
  if (!label) return null;
  const className = FORMAT_CLASS_BY_VALUE[label.toLowerCase()] || "fmt-other";
  return create("span", { className: `fmt ${className}`, text: label });
}
function statusIcon(status) {
  const icon = create("span", { className: "st", attrs: { "aria-hidden": "true" } });
  if (status === "ok") {
    icon.classList.add("ok");
    icon.textContent = "✔";
  } else if (status === "error") {
    icon.classList.add("err");
    icon.textContent = "⚠";
  } else {
    icon.classList.add("miss");
    icon.textContent = "✖";
  }
  return icon;
}
function focusableDialogElements() {
  return [...$("#modal").querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')].filter((element) => !element.disabled && !element.closest(".hidden"));
}
function trapDialogFocus(event) {
  if ($("#modalOverlay").classList.contains("hidden")) return;
  if (event.key === "Tab") {
    trapDialogTabCycle(event);
  }
}
function trapDialogTabCycle(event) {
  const focusable = focusableDialogElements();
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}
function releaseDialogFocus() {
  if (previousFocus && typeof previousFocus.focus === "function") previousFocus.focus();
  previousFocus = null;
}
function openDialog(initialFocus) {
  previousFocus = document.activeElement;
  setHidden($("#modalOverlay"), false);
  const target = initialFocus || focusableDialogElements()[0] || $("#modal");
  target.focus();
}
function closeLogin() {
  setHidden($("#modalOverlay"), true);
  releaseDialogFocus();
}
async function loadConfig() {
  const config = await api("/api/config");
  $("#musicRoot").value = config.music_root || "";
  $("#wavMode").value = config.wav_mode || "source";
  $("#numbering").checked = !!config.numbering;
  $("#btnLoginDeezer").textContent = config.user && config.user.email ? `Deezer: ${config.user.email}` : "Deezer: вход";
  setAuthActive($("#btnLoginDeezer"), !!(config.user && config.user.email));
  $("#btnLoginSc").textContent = config.sc_user ? `SC: ${config.sc_user}` : "SC: вход";
  setAuthActive($("#btnLoginSc"), !!config.sc_user);
}
async function openLogin(service) {
  loginService = service;
  if (isPackagedAppOrigin()) {
    await tauriLogin(service);
    return;
  }
  clearError();
  $("#loginResult").textContent = "";
  setResultState(null);
  $("#loginToken").value = "";
  setHidden($("#scImport"), true);
  const steps = [];
  if (service === "deezer") {
    $("#loginTitle").textContent = "Вход в Deezer";
    steps.push(create("strong", { text: "Просто email и пароль" }), text(" — как в Saturn:"));
    setHidden($("#loginPasswordBlock"), false);
    $("#loginToken").placeholder = "…или вставьте ARL cookie вручную сюда";
  } else {
    setHidden($("#loginPasswordBlock"), true);
    $("#loginTitle").textContent = "Вход в SoundCloud";
    steps.push(
      create("strong", { text: "Вручную:" }),
      text(" F12 → Application → Cookies → "),
      create("strong", { text: "oauth_token" }),
      text(" на soundcloud.com → вставить ниже")
    );
    $("#loginToken").placeholder = "oauth_token cookie";
  }
  replaceChildren($("#loginSteps"), steps);
  openDialog($("#loginToken"));
  if (service === "sc" && $("#btnLoginSc").textContent.startsWith("SC: ") && $("#btnLoginSc").classList.contains("auth-active")) {
    setResultState("ok");
    $("#loginResult").textContent = "✔ уже выполнен вход — выберите, что импортировать";
    await loadScAccount();
  }
}
async function tauriLogin(service) {
  try {
    const token = await invoke("service_login", { service });
    const url = service === "deezer" ? "/api/login/deezer" : "/api/login/soundcloud";
    const body = service === "deezer" ? { arl: token } : { oauth_token: token };
    const result = await api(url, { body });
    showStatus(`Вход выполнен: ${result.email || result.username}`);
    await loadConfig();
    if (service === "sc") switchTab("sc");
    else await loadPlaylists();
  } catch (error) {
    if (!String(error).includes("закрыто")) showError(error, "security");
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
    const result = await api(url, { body });
    setResultState("ok");
    $("#loginResult").textContent = loginService === "deezer" ? `✔ ${result.email}` : `✔ ${result.username}`;
    await loadConfig();
    if (loginService === "sc") await loadScAccount();
    else {
      closeLogin();
      await loadPlaylists();
    }
  } catch (error) {
    setResultState("error");
    $("#loginResult").textContent = describeError(error, "security");
    showError(error, "security");
  }
}
async function loadScAccount() {
  try {
    const data = await api("/api/sc/account");
    const items = [data.likes, ...data.playlists].filter(Boolean);
    replaceChildren($("#scAccountList"), items.map((item) => create("label", { className: "scacc" }, [
      create("input", {
        type: "checkbox",
        className: "scacc-cb",
        dataset: { id: item.id, title: item.title, url: item.url, count: item.count || 0 }
      }),
      text(`${item.title} `),
      create("span", { className: "dim", text: `(${item.count ?? "?"})` })
    ])));
    setHidden($("#scImport"), false);
  } catch (error) {
    setResultState("error");
    $("#loginResult").textContent = describeError(error, "security");
    showError(error, "security");
  }
}
async function importScAccount() {
  const items = [...document.querySelectorAll(".scacc-cb:checked")].map((checkbox) => ({
    id: checkbox.dataset.id,
    title: checkbox.dataset.title,
    url: checkbox.dataset.url,
    count: Number(checkbox.dataset.count)
  }));
  if (!items.length) return;
  const result = await api("/api/sc/account/import", { body: { items } });
  showStatus(`Импортировано: ${result.added}`);
  closeLogin();
  switchTab("sc");
}
async function saveWavMode() {
  await api("/api/config", { body: { wav_mode: $("#wavMode").value } });
  showStatus("Режим WAV сохранен");
}
async function saveNumbering() {
  await api("/api/config", { body: { numbering: $("#numbering").checked } });
  showStatus("Нумерация сохранена");
}
async function sendReport() {
  const reportText = prompt("Опишите проблему (что делали, что ожидали, что произошло):");
  if (!reportText) return;
  try {
    await api("/api/report", { body: { text: reportText, current: current ? current.title : tab } });
    showStatus("Репорт отправлен, спасибо!");
  } catch (error) {
    showError(error, "local");
  }
}
async function saveRoot() {
  await api("/api/config", { body: { music_root: $("#musicRoot").value } });
  showStatus("Корень библиотеки сохранен");
  await loadPlaylists();
  if (current) await loadTracks(current);
}
function playlistControl(title, cover, bodyChildren, action, dataset, active) {
  return create("button", {
    type: "button",
    className: `pl${active ? " active" : ""}`,
    action,
    dataset,
    attrs: { "aria-pressed": active ? "true" : "false", "aria-label": title }
  }, [cover, create("span", { className: "pl-body" }, bodyChildren)]);
}
async function loadPlaylists() {
  try {
    clearError();
    if (tab === "sc") return await loadScSources();
    if (tab === "errors") return await loadErrors();
    if (tab === "search") return await loadSearchTargets();
    const playlists = await api("/api/playlists");
    replaceChildren($("#playlists"), playlists.map((playlist) => {
      const cover = create("img", { attrs: { alt: "" } });
      const coverUrl = safeCoverUrl(playlist.cover);
      if (coverUrl) cover.src = coverUrl;
      const active = current && current.id === playlist.id;
      return playlistControl(playlist.title, cover, [
        create("span", { className: "t", text: playlist.title }),
        create("span", { className: "c" }, [
          text(`${playlist.ok || 0}/${playlist.count}`),
          playlist.errors ? create("span", { className: "badge-err", text: ` ⚠ ${playlist.errors}` }) : null
        ])
      ], "select-playlist", { id: playlist.id, title: playlist.title }, active);
    }));
  } catch (error) {
    showError(error, tab === "errors" ? "state" : "local");
    replaceChildren($("#playlists"), create("div", { id: "empty", text: describeError(error) }));
  }
}
async function loadErrors() {
  const errors = await api("/api/errors");
  window._errors = errors;
  if (!errors.length) {
    replaceChildren($("#playlists"), create("div", { id: "empty", text: "Ошибок нет" }));
    return;
  }
  const children = [
    create("div", { className: "list-pad" }, button(`Повторить все (${errors.length})`, "retry-all", { className: "full-width" }))
  ];
  errors.forEach((errorItem, index) => {
    children.push(create("div", { className: "pl pl-static" }, [
      create("span", { className: "pl-fill" }, [
        create("span", { className: "t", text: errorItem.track.title }),
        create("span", { className: "c", text: `${errorItem.playlist_title} · ${errorItem.track.artist}` }),
        create("span", { className: "badge-err", text: errorItem.error })
      ]),
      button("↻", "retry-one", {
        className: "ghost button-small",
        dataset: { index },
        attrs: { "aria-label": `Повторить ${errorItem.track.title}` }
      })
    ]));
  });
  replaceChildren($("#playlists"), children);
}
async function retryOne(index) {
  const errorItem = window._errors[index];
  await api("/api/errors/retry", { body: { playlist_key: errorItem.playlist_key, playlist_title: errorItem.playlist_title, track: errorItem.track } });
  showStatus("Повтор запущен");
  startPolling();
}
async function retryAll() {
  for (const errorItem of window._errors || []) {
    await api("/api/errors/retry", { body: { playlist_key: errorItem.playlist_key, playlist_title: errorItem.playlist_title, track: errorItem.track } });
  }
  showStatus("Повтор всех ошибок запущен");
  startPolling();
}
function switchTab(nextTab) {
  tab = nextTab;
  $("#tab-deezer").classList.toggle("active", nextTab === "deezer");
  $("#tab-sc").classList.toggle("active", nextTab === "sc");
  $("#tab-search").classList.toggle("active", nextTab === "search");
  $("#tab-errors").classList.toggle("active", nextTab === "errors");
  setFlexVisible($("#sc-add"), nextTab === "sc");
  setFlexVisible($("#searchbar"), nextTab === "search");
  setFlexVisible($("#searchFilters"), nextTab === "search");
  current = null;
  setFlexVisible($("#toolbar"), false);
  if (nextTab === "search") {
    setEmpty("Слева — цель (плейлист Deezer / источник SC / локальный плейлист / своя папка). Ищите треки, отмечайте чекбоксами или качайте по одному. Альбомы и сеты раскрываются по клику — внутри треки качаются поштучно или все сразу.");
    setSearchFilter(searchFilter);
    loadSearchTargets();
    return;
  }
  setEmpty(nextTab === "errors" ? "Треки с ошибками загрузки/верификации — слева. Кнопка ↻ перезапускает сломавшийся этап." : `Выберите ${nextTab === "sc" ? "источник" : "плейлист"} слева`);
  loadPlaylists();
}
async function loadSearchTargets() {
  const [deezerRows, scRows, localRows] = await Promise.all([
    api("/api/playlists"),
    api("/api/sc/sources"),
    api("/api/local/playlists")
  ]);
  const rows = [
    ...deezerRows.map((item) => ({ key: item.id, title: item.title, provider: "deezer", count: item.count })),
    ...scRows.map((item) => ({ key: `sc:${item.id}`, title: item.title, provider: "sc", count: item.count })),
    ...localRows.map((item) => ({ key: item.key, title: item.title, provider: "local", count: item.count }))
  ];
  if (!searchTarget && rows.length) searchTarget = rows[0];
  window._targets = rows;
  const currentTarget = searchTarget ? `${provLabel(searchTarget.provider)} · ${searchTarget.title}` : "не выбрана";
  const targetBox = create("div", { id: "targetBox" }, [
    create("div", { className: "cap", text: "Цель загрузки / добавления" }),
    create("div", { className: "cur", text: currentTarget, title: currentTarget }),
    create("div", { className: "acts" }, [
      button("＋Deezer", "create-target-playlist", { className: "ghost", dataset: { kind: "deezer" }, title: "Новый плейлист в Deezer" }),
      button("＋Локальный (SC)", "create-target-playlist", { className: "ghost", dataset: { kind: "local" }, title: "Новый локальный плейлист (SoundCloud не даёт создавать плейлисты через API — создаётся локальная папка-плейлист, URL можно привязать позже)" }),
      button("📁 Папка…", "choose-custom-dir", { className: "ghost", title: "Скачивать в произвольную папку" })
    ])
  ]);
  const controls = [targetBox];
  rows.forEach((row, index) => {
    const active = searchTarget && searchTarget.key === row.key;
    controls.push(playlistControl(row.title, create("img", { attrs: { alt: "" } }), [
      create("span", { className: "t", text: row.title }),
      create("span", { className: "c", text: `${provLabel(row.provider)} · ${row.count ?? "?"}` })
    ], "set-search-target", { index }, active));
  });
  replaceChildren($("#playlists"), controls);
}
function setSearchTarget(index) {
  searchTarget = window._targets[index];
  loadSearchTargets();
}
async function chooseCustomDir() {
  const result = await api("/api/browse");
  if (!result.path) return;
  searchTarget = { key: "", title: result.path, provider: "dir", dir: result.path };
  await loadSearchTargets();
}
async function createTargetPlaylist(kind) {
  if (kind === "deezer") {
    const title = prompt("Название нового плейлиста в Deezer:");
    if (!title) return;
    const result = await api("/api/deezer/playlist/create", { body: { title, track_ids: [] } });
    searchTarget = { key: result.id, title: result.title, provider: "deezer" };
  } else {
    const title = prompt("Название локального плейлиста (папка в библиотеке; в SoundCloud плейлисты через API создавать нельзя):");
    if (!title) return;
    const result = await api("/api/local/playlists", { body: { title } });
    searchTarget = { key: result.key, title: result.title, provider: "local" };
  }
  await loadSearchTargets();
}
function setSearchFilter(filter) {
  searchFilter = filter;
  document.querySelectorAll("#searchFilters .chip").forEach((chip) => chip.classList.toggle("active", chip.dataset.f === filter));
  if (window._search) renderSearch();
}
async function runSearch() {
  const query = $("#searchInput").value.trim();
  if (!query) return;
  searchSel = {};
  searchExpanded = {};
  setEmpty("Ищу…");
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(query)}&service=${$("#searchService").value}`);
    window._search = data;
    setSearchFilter(searchFilter);
  } catch (error) {
    showError(error, "local");
    setEmpty(describeError(error));
  }
}
function _selKey(service, index) {
  return `${service}:${index}`;
}
function _trackCb(service, index, track) {
  const key = _selKey(service, index);
  if (searchSel[key]) delete searchSel[key];
  else searchSel[key] = track;
  const checkbox = document.getElementById(`cb-${service}-${index}`);
  if (checkbox) checkbox.checked = !!searchSel[key];
  _renderBasket();
}
function _renderBasket() {
  const count = Object.keys(searchSel).length;
  const oldBasket = document.getElementById("basket");
  if (!count) {
    if (oldBasket) oldBasket.remove();
    return;
  }
  const target = searchTarget ? `${provLabel(searchTarget.provider)} · ${searchTarget.title}` : "—";
  const basket = oldBasket || create("div", { id: "basket" });
  replaceChildren(basket, [
    create("strong", { text: `${count} тр.` }),
    button(`⬇ в цель: ${target}`, "dl-basket", { attrs: { "aria-label": `Скачать выбранные треки в цель: ${target}` } }),
    button("✕ очистить", "clear-basket", { className: "ghost", attrs: { "aria-label": "Очистить выбранные треки" } })
  ]);
  if (!oldBasket) $("#tracks").append(basket);
}
function trackRow(service, index, track) {
  return create("div", { className: "srow" }, [
    create("input", {
      id: `cb-${service}-${index}`,
      type: "checkbox",
      className: "cb",
      checked: !!searchSel[_selKey(service, index)],
      action: "track-checkbox",
      dataset: { service, index },
      attrs: { "aria-label": `Выбрать ${track.title ?? "трек"}` }
    }),
    create("div", { className: "tt", title: `${track.title ?? ""} — ${track.artist ?? ""}` }, [
      text(track.title),
      create("span", { className: "meta", text: ` ${track.artist ?? ""} · ${fmtDur(track.duration)}` })
    ]),
    create("span", { className: `prov${service === "sc" ? " sc" : ""}`, text: service === "sc" ? "SC" : "DZ" }),
    service === "deezer" ? button("＋", "add-dz-track", {
      className: "ghost",
      title: "Добавить в плейлист Deezer без скачивания",
      dataset: { index },
      attrs: { "aria-label": `Добавить ${track.title ?? "трек"} в плейлист Deezer без скачивания` }
    }) : null,
    button("⬇", "dl-search-track", {
      title: "Скачать в цель",
      dataset: { service, index },
      attrs: { "aria-label": `Скачать ${track.title ?? "трек"} в выбранную цель` }
    })
  ]);
}
function albumRow(service, index, album) {
  const key = `${service}:${index}`;
  const expanded = searchExpanded[key];
  const toggle = button(`${expanded && expanded.tracks ? "▾" : "▸"} 💿 ${album.title}`, "toggle-album", {
    className: "srow exp",
    dataset: { service, index },
    attrs: {
      "aria-expanded": expanded ? "true" : "false",
      "aria-label": `${expanded ? "Свернуть" : "Раскрыть"} ${album.title}`
    }
  });
  toggle.append(create("span", { className: "meta", text: ` ${album.artist ?? ""}${album.count ? ` · ${album.count} тр.` : ""}` }));
  toggle.append(create("span", { className: `prov${service === "sc" ? " sc" : ""}`, text: service === "sc" ? "SC" : "DZ" }));
  const children = [toggle];
  if (!expanded) return children;
  const expandedBox = create("div", { className: "stracks" });
  if (expanded.loading) {
    expandedBox.append(create("div", { className: "srow dim", text: "Загружаю треки…" }));
  } else if (expanded.error) {
    expandedBox.append(create("div", { className: "srow errtext", text: `⚠ ${expanded.error}` }));
  } else if (expanded.tracks) {
    expanded.tracks.forEach((track, trackIndex) => {
      expandedBox.append(create("div", { className: "srow" }, [
        create("div", { className: "tt", title: `${track.title ?? ""} — ${track.artist ?? ""}` }, [
          text(track.title),
          create("span", { className: "meta", text: ` ${track.artist ?? ""} · ${fmtDur(track.duration)}` })
        ]),
        button("⬇", "dl-album-track", {
          title: "Скачать в цель",
          dataset: { service, index, trackIndex },
          attrs: { "aria-label": `Скачать ${track.title ?? "трек"} из альбома в выбранную цель` }
        })
      ]));
    });
    expandedBox.append(create("div", { className: "srow" }, [
      create("div", { className: "tt dim", text: `${expanded.tracks.length} тр.` }),
      button("⬇ все", "dl-whole-album", {
        dataset: { service, index },
        attrs: { "aria-label": `Скачать весь альбом ${album.title}` }
      })
    ]));
  }
  children.push(expandedBox);
  return children;
}
async function toggleAlbum(service, index) {
  const key = `${service}:${index}`;
  if (searchExpanded[key] && !searchExpanded[key].loading) {
    delete searchExpanded[key];
    renderSearch();
    return;
  }
  searchExpanded[key] = { loading: true };
  renderSearch();
  try {
    const album = window._search[service].albums[index];
    searchExpanded[key] = { tracks: service === "deezer" ? await api(`/api/deezer/album/${album.id}`) : await api(`/api/sc/resolve-tracks?url=${encodeURIComponent(album.url)}`) };
  } catch (error) {
    searchExpanded[key] = { error: describeError(error, "local") };
  }
  renderSearch();
}
function createSearchSection(title, rows) {
  return create("section", { className: "search-sec-wrap", attrs: { "aria-label": title } }, [
    create("div", { className: "search-sec", text: title }),
    ...rows
  ]);
}
function renderSearch() {
  const data = window._search || {};
  const sections = [];
  for (const [service, label] of [["deezer", "Deezer"], ["sc", "SoundCloud"]]) {
    const serviceData = data[service];
    if (!serviceData) continue;
    if ((searchFilter === "all" || searchFilter === "tracks") && serviceData.tracks.length) {
      sections.push(createSearchSection(`${label} — треки`, serviceData.tracks.map((track, index) => trackRow(service, index, track))));
    }
    const isPlaylistFilter = searchFilter === "playlists";
    if ((searchFilter === "all" || searchFilter === "albums" || isPlaylistFilter) && serviceData.albums.length) {
      const kind = service === "deezer" ? "альбомы" : "плейлисты и сеты";
      if (!(isPlaylistFilter && service === "deezer")) {
        sections.push(createSearchSection(`${label} — ${kind}`, serviceData.albums.flatMap((album, index) => albumRow(service, index, album))));
      }
    }
    if ((searchFilter === "all" || searchFilter === "artists") && serviceData.artists.length) {
      sections.push(createSearchSection(`${label} — исполнители`, serviceData.artists.map((artist) => create("div", { className: "srow" }, create("div", { className: "tt", text: `👤 ${artist.name}` })))));
    }
  }
  replaceChildren($("#tracks"), sections.length ? sections : create("div", { id: "empty", text: "Ничего не найдено" }));
  _renderBasket();
}
function _dlPayload(trackList) {
  if (!searchTarget) {
    showError(new Error("Слева выберите цель (плейлист или папку)"), "state");
    return null;
  }
  if (searchTarget.provider === "dir") {
    return { target_key: "dir", target_title: searchTarget.title, target_dir: searchTarget.dir, tracks: trackList };
  }
  return { target_key: searchTarget.key, target_title: searchTarget.title, tracks: trackList };
}
async function dlSearchTrack(service, index) {
  const track = window._search[service].tracks[index];
  const body = _dlPayload([{ id: track.id, title: track.title, artist: track.artist, duration: track.duration, url: track.url, provider: service }]);
  if (!body) return;
  await api("/api/search/download", { body });
  showStatus("Трек поставлен в очередь");
  startPolling();
}
async function dlAlbumTrack(service, index, trackIndex) {
  const track = searchExpanded[`${service}:${index}`].tracks[trackIndex];
  const body = _dlPayload([{ id: track.id, title: track.title, artist: track.artist, duration: track.duration, url: track.url, provider: service }]);
  if (!body) return;
  await api("/api/search/download", { body });
  showStatus("Трек поставлен в очередь");
  startPolling();
}
async function dlWholeAlbum(service, index) {
  const album = window._search[service].albums[index];
  const albumTracks = searchExpanded[`${service}:${index}`].tracks;
  const body = _dlPayload(albumTracks.map((track) => ({ id: track.id, title: track.title, artist: track.artist, duration: track.duration, url: track.url, provider: service })));
  if (!body) return;
  await api("/api/search/download", { body });
  showStatus(`«${album.title}» поставлен в очередь (${albumTracks.length} треков)`);
  startPolling();
}
async function dlBasket() {
  const selected = Object.values(searchSel).map((track) => ({ id: track.id, title: track.title, artist: track.artist, duration: track.duration, url: track.url, provider: track.provider }));
  const body = _dlPayload(selected);
  if (!body) return;
  await api("/api/search/download", { body });
  showStatus(`Поставлено в очередь: ${selected.length} тр.`);
  searchSel = {};
  _renderBasket();
  renderSearch();
  startPolling();
}
async function addDzTrack(index) {
  if (!searchTarget || searchTarget.provider !== "deezer") {
    showError(new Error("Выберите слева плейлист Deezer как цель"), "state");
    return;
  }
  const track = window._search.deezer.tracks[index];
  await api("/api/deezer/playlist/add", { body: { playlist_id: searchTarget.key, track_ids: [track.id] } });
  showStatus(`«${track.title}» добавлен в «${searchTarget.title}»`);
}
async function loadScSources() {
  try {
    await api("/api/sc/sync-account", { body: {} });
  } catch {
  }
  const sources = await api("/api/sc/sources");
  if (!sources.length) {
    replaceChildren($("#playlists"), create("div", { id: "empty", text: "Войдите через «SC: вход» — плейлисты аккаунта появятся сами. Или добавьте URL выше." }));
    return;
  }
  replaceChildren($("#playlists"), sources.map((source) => {
    const active = current && current.id === source.id;
    return playlistControl(source.title, create("img", { attrs: { alt: "" } }), [
      create("span", { className: "t", text: source.title }),
      create("span", { className: "c" }, [
        text(`${source.ok || 0}/${source.count ?? "?"}`),
        source.errors ? create("span", { className: "badge-err", text: ` ⚠ ${source.errors}` }) : null
      ])
    ], "select-sc-source", { id: source.id, title: source.title }, active);
  }));
}
async function addScSource() {
  const url = $("#scUrl").value.trim();
  if (!url) return;
  $("#scUrl").value = "";
  try {
    await api("/api/sc/sources", { body: { url } });
    showStatus("Источник SoundCloud добавлен");
  } catch (error) {
    showError(error, "local");
  }
  await loadPlaylists();
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
async function loadTracks(playlist) {
  setFlexVisible($("#toolbar"), true);
  $("#pltitle").textContent = playlist.title;
  setEmpty("Загрузка… (большие плейлисты — до ~20 сек)");
  try {
    const url = playlist.kind === "sc" ? `/api/sc/sources/${playlist.id}/tracks` : `/api/playlists/${playlist.id}/tracks?title=${encodeURIComponent(playlist.title)}`;
    const data = await api(url);
    tracks = data.tracks;
    $("#plpath").textContent = data.path || "";
    renderTracks();
  } catch (error) {
    showError(error, "state");
    setEmpty(`Не удалось загрузить: ${describeError(error, "state")}`, "Повторить", "rescan");
  }
}
function renderTracks() {
  const ok = tracks.filter((track) => track.status === "ok").length;
  const err = tracks.filter((track) => track.status === "error").length;
  const miss = tracks.length - ok - err;
  $("#plstats").textContent = `✔ ${ok} · ✖ ${miss} · ⚠ ${err}`;
  const flipped = tracks.filter((track) => track.flipped).length;
  $("#flipBtn").textContent = flipped ? `⇄ FLAC (${flipped} в WAV)` : "⇄ WAV";
  const table = create("table", { attrs: { "aria-label": "Треки плейлиста" } });
  const thead = create("thead");
  thead.append(create("tr", {}, [
    create("th", {}, create("input", { type: "checkbox", id: "all", action: "toggle-all", attrs: { "aria-label": "Выбрать все недостающие треки" } })),
    create("th", { text: "Статус" }),
    create("th", { text: "Название" }),
    create("th", { text: "Исполнитель" }),
    create("th", { text: "Альбом" }),
    create("th", { text: "⏱" }),
    create("th", { text: "Формат" })
  ]));
  const tbody = create("tbody");
  tracks.forEach((track, index) => {
    const formatCell = create("td");
    const badge = formatBadge(track.format);
    if (badge) formatCell.append(badge);
    if (track.flipped) formatCell.append(create("span", { className: "fmt fmt-wav", text: "→wav" }));
    if (track.mp3_source) formatCell.append(create("span", { className: "fmt fmt-mp3src", text: "mp3", title: "mp3-источник: после конвертации в WAV кью могут сместиться на ~26 мс" }));
    tbody.append(create("tr", {}, [
      create("td", {}, create("input", { type: "checkbox", className: "trk", disabled: track.status === "ok", dataset: { i: index }, attrs: { "aria-label": `Выбрать ${track.title}` } })),
      create("td", {}, statusIcon(track.status)),
      create("td", {}, [text(track.title), track.error ? create("div", { className: "errtext", text: track.error }) : null]),
      create("td", { text: track.artist }),
      create("td", { className: "dim", text: track.album }),
      create("td", { className: "dim", text: fmtDur(track.duration) }),
      formatCell
    ]));
  });
  table.append(thead, tbody);
  replaceChildren($("#tracks"), table);
}
function toggleAll(value) {
  document.querySelectorAll(".trk:not(:disabled)").forEach((checkbox) => {
    checkbox.checked = value;
  });
}
function selectedTracks(onlyMissing = false) {
  const indexes = [...document.querySelectorAll(".trk:checked")].map((checkbox) => Number(checkbox.dataset.i));
  let selected = indexes.map((index) => tracks[index]);
  if (onlyMissing) selected = selected.filter((track) => track.status !== "ok");
  return selected.map((track) => ({ id: track.id, title: track.title, artist: track.artist, duration: track.duration, url: track.url, total: tracks.length }));
}
function downloadUrl(mode) {
  const base = current.kind === "sc" ? `/api/sc/sources/${current.id}/download` : `/api/playlists/${current.id}/download?title=${encodeURIComponent(current.title)}`;
  return base + (base.includes("?") ? "&" : "?") + "mode=" + mode;
}
async function downloadSelected() {
  const selected = selectedTracks(true);
  if (!selected.length) {
    showError(new Error("Ничего не выбрано (уже скачанные пропускаются)"), "state");
    return;
  }
  await api(downloadUrl("append"), { body: { tracks: selected } });
  showStatus("Выбранные треки поставлены в очередь");
  startPolling();
}
async function syncPlaylistOrder() {
  const order = tracks.map((track) => track.id);
  const key = current.kind === "sc" ? `sc:${current.id}` : current.id;
  const renumber = await api(
    `/api/playlists/${key}/renumber?title=${encodeURIComponent(current.title)}`,
    { body: { order, total: tracks.length } }
  );
  const missing = tracks.filter((track) => track.status !== "ok").map((track) => ({
    id: track.id,
    title: track.title,
    artist: track.artist,
    duration: track.duration,
    url: track.url,
    position: tracks.indexOf(track) + 1,
    total: tracks.length
  }));
  if (!missing.length) {
    showStatus(`Порядок применён (переименовано: ${renumber.renamed}). Всё уже скачано.`);
    rescan();
    return;
  }
  if (!confirm(`Перенумеровано: ${renumber.renamed}. Скачать ${missing.length} треков?`)) {
    rescan();
    return;
  }
  await api(downloadUrl("playlist_order"), { body: { tracks: missing } });
  showStatus("Синк по порядку поставлен в очередь");
  startPolling();
}
async function syncAppend() {
  const missing = tracks.filter((track) => track.status !== "ok").map((track) => ({ id: track.id, title: track.title, artist: track.artist, duration: track.duration, url: track.url, total: tracks.length }));
  if (!missing.length) {
    showStatus("Всё уже скачано");
    return;
  }
  if (!confirm(`Скачать ${missing.length} треков (новые — вниз списка)?`)) return;
  await api(downloadUrl("append"), { body: { tracks: missing } });
  showStatus("Новые треки поставлены в очередь");
  startPolling();
}
async function rbSync() {
  const key = current.kind === "sc" ? `sc:${current.id}` : current.id;
  const body = { playlist_key: key, playlist_title: current.title };
  try {
    const dryRun = await api("/api/rb/sync?dry_run=true", { body });
    showStatus(formatRbSyncResult(dryRun));
    if (dryRun.error) {
      showError(new Error(`${dryRun.error.code}: ${dryRun.error.message}`), "rekordbox");
      return;
    }
    if (!rbSyncHasChanges(dryRun)) return;
    if (!confirm(`Dry-run для «${current.title}» готов. Применить экспериментальные изменения в Rekordbox?
Rekordbox должен быть ЗАКРЫТ. Бэкап создается только после явного применения.`)) return;
    const token = prompt(`Для применения введите точно: ${RB_APPLY_CONFIRMATION_TOKEN}`);
    if (token !== RB_APPLY_CONFIRMATION_TOKEN) {
      showStatus(`${formatRbSyncResult(dryRun)} Apply cancelled: изменения не применялись, бэкап не создавался.`);
      return;
    }
    const applied = await api(`/api/rb/sync?dry_run=false&confirmation_token=${encodeURIComponent(RB_APPLY_CONFIRMATION_TOKEN)}`, { body });
    showStatus(formatRbSyncResult(applied));
    if (applied.error) showError(new Error(`${applied.error.code}: ${applied.error.message}`), "rekordbox");
  } catch (error) {
    showError(error, "rekordbox");
  }
}
function rbPlanCounts(result) {
  return result && result.plan && result.plan.counts || {};
}
function rbCountValue(counts, name) {
  return Number.isFinite(Number(counts[name])) ? Number(counts[name]) : 0;
}
function formatRbSyncCounts(result) {
  const counts = rbPlanCounts(result);
  return ["add", "remove", "reorder", "metadata", "path", "unresolved"].map((name) => `${name}: ${rbCountValue(counts, name)}`).join(", ");
}
function rbSyncHasChanges(result) {
  const counts = rbPlanCounts(result);
  return ["add", "remove", "reorder", "metadata", "path", "unresolved"].some((name) => rbCountValue(counts, name) > 0);
}
function formatRbSyncResult(result) {
  const payload = result || {};
  const mode = payload.dry_run === false ? "Apply" : "Dry-run";
  const hash = payload.plan && payload.plan.hash ? `, hash: ${String(payload.plan.hash).slice(0, 12)}` : "";
  const error = payload.error ? `, error: ${payload.error.code || "unknown"} ${payload.error.message || ""}` : "";
  if (payload.dry_run === false) {
    return `Rekordbox ${mode}: applied: ${payload.applied ? "yes" : "no"}, reconciled: ${payload.reconciled ? "yes" : "no"}, backup: ${payload.backup_id || "none"}, ${formatRbSyncCounts(payload)}${hash}${error}`;
  }
  return `Rekordbox ${mode}: изменения не применялись, бэкап не создавался, ${formatRbSyncCounts(payload)}${hash}${error}`;
}
async function flipWav() {
  const flipped = tracks.filter((track) => track.flipped).length;
  const toWav = flipped === 0;
  const message = toWav ? `Конвертировать «${current.title}» в WAV для CDJ?
Все кью, сетка, BPM и порядок сохранятся (пути в master.db переключатся на WAV).
Rekordbox должен быть ЗАКРЫТ.` : `Вернуть «${current.title}» к исходникам (FLAC/MP3)?
Пути в master.db переключатся обратно. Rekordbox должен быть ЗАКРЫТ.`;
  if (!confirm(message)) return;
  const key = current.kind === "sc" ? `sc:${current.id}` : current.id;
  try {
    await api("/api/flip", { body: { playlist_key: key, playlist_title: current.title, to_wav: toWav } });
    showStatus(toWav ? "WAV-конвертация запущена" : "Возврат к исходникам запущен");
    startPolling();
  } catch (error) {
    showError(error, "rekordbox");
  }
}
async function bindPath() {
  let selectedPath = null;
  let browseFailed = false;
  try {
    const browse = await api("/api/browse");
    selectedPath = browse.path;
  } catch {
    browseFailed = true;
  }
  if (!selectedPath && browseFailed) {
    selectedPath = prompt("Папка для этого плейлиста (например на флешке E:\\Playlists\\Set):", $("#plpath").textContent);
  }
  if (!selectedPath) return;
  const key = current.kind === "sc" ? `sc:${current.id}` : current.id;
  await api(`/api/playlists/${key}/bind`, { body: { path: selectedPath } });
  await loadTracks(current);
  await loadPlaylists();
}
async function rescan() {
  if (!current) return;
  await loadTracks(current);
  await loadPlaylists();
}
function renderJobs(jobs) {
  replaceChildren($("#jobs"), jobs.slice(0, 5).map((job) => create("div", { className: "job" }, [
    create("strong", { text: job.title }),
    text(`: ${job.done}/${job.total}`),
    job.failed ? create("span", { className: "err", text: ` (ошибок: ${job.failed})` }) : null,
    job.current ? create("span", { className: "dim", text: ` — ${job.current}` }) : null,
    create("progress", { className: "bar", attrs: { max: "100", value: progressValue(job.done, job.total) } })
  ])));
}
function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    try {
      const jobs = await api("/api/jobs");
      const active = jobs.filter((job) => job.state !== "done");
      renderJobs(jobs);
      if (!active.length) {
        clearInterval(pollTimer);
        pollTimer = null;
        if (current) await loadTracks(current);
        await loadPlaylists();
      }
    } catch (error) {
      clearInterval(pollTimer);
      pollTimer = null;
      showError(error, "state");
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
function activateAction(el, event) {
  const action = clickActions[el.dataset.action];
  if (!action) return false;
  if (el.dataset.action === "overlay-close" && event.target !== el) return false;
  event.preventDefault();
  clearError();
  Promise.resolve(action(el, event)).catch((error) => showError(error));
  return true;
}
document.addEventListener("click", (event) => {
  const el = event.target.closest("[data-action]");
  if (!el) return;
  activateAction(el, event);
});
document.addEventListener("change", (event) => {
  const el = event.target.closest("[data-action]");
  if (!el) return;
  const action = changeActions[el.dataset.action];
  if (!action) return;
  Promise.resolve(action(el, event)).catch((error) => showError(error));
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("#modalOverlay").classList.contains("hidden")) {
    event.preventDefault();
    closeLogin();
    return;
  }
  trapDialogFocus(event);
  if ((event.key === "Enter" || event.key === " ") && event.target.matches("[data-action]:not(button):not(input):not(select):not(textarea)")) {
    activateAction(event.target, event);
  }
});
$("#searchInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter") runSearch();
});
async function init() {
  try {
    await loadConfig();
    await loadPlaylists();
  } catch (error) {
    showError(error, "local");
    setEmpty(describeError(error, "local"));
  }
}
init();

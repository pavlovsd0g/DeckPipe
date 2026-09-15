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
var libraryConfigured = false;
var libraryReady = false;
var libraryPollGeneration = 0;
var activeAuthRequest = null;
var authPollTimer = null;
var authAttemptEpoch = 0;
var nativeAccounts = {};
var playlistLoadGeneration = 0;
var providerAuth = {
  deezer: { generation: 0, required: false },
  sc: { generation: 0, required: false }
};
var rbSelectionEpoch = 0;
var rbOperationGeneration = 0;
var rbOperationBusy = false;
var rbMediaMode = null;
var activeDialogKind = null;
var pendingRbDialog = null;
var rbTargetIdsBySource = /* @__PURE__ */ new Map();
var $ = (s) => document.querySelector(s);
var RB_APPLY_CONFIRMATION_TOKEN = "APPLY_REKORDBOX_CHANGES";
var LOGIN_CANCELLED = "DECKPIPE_LOGIN_CANCELLED";
var RB_ERROR_MESSAGES = Object.freeze({
  adapter_open_failed: "Не удалось открыть библиотеку Rekordbox. Проверьте установку и профиль Rekordbox.",
  apply_not_confirmed: "Подтверждение применения отклонено. Изменения не вносились.",
  ambiguous_playlist_target: "В Rekordbox найдено несколько плейлистов с таким названием. Выберите нужный.",
  catalog_ambiguous: "Для одного или нескольких треков найдено несколько файлов. Сначала выберите точные совпадения.",
  catalog_missing: "Для одного или нескольких треков не найдены локальные файлы.",
  concurrent_apply: "Другая операция Rekordbox уже выполняется. Дождитесь её завершения и повторите просмотр.",
  local_membership_unavailable: "Состав локального плейлиста недоступен или повреждён. Изменения не применялись.",
  music_root_required: "Сначала подключите общую музыкальную папку.",
  music_root_unavailable: "Общая музыкальная папка недоступна. Изменения не применялись.",
  media_state_unavailable: "Изменения записаны, но текущее состояние файлов не удалось перечитать.",
  media_bit_depth_conflict: "Для этих файлов уже подготовлен WAV другой разрядности. Проверьте подготовленные файлы.",
  playlist_not_found: "Выбранный плейлист Rekordbox больше не найден. Обновите просмотр.",
  rekordbox_process_check_failed: "Не удалось проверить, закрыт ли Rekordbox. Запись заблокирована; повторите проверку состояния.",
  recovery_stale_preview: "Незавершённая операция или её файлы изменились. Откройте восстановление заново и проверьте новое состояние.",
  recovery_context_unavailable: "Данные восстановления недоступны. Подключите диск Rekordbox и повторите проверку.",
  recovery_not_needed: "Незавершённых операций Rekordbox нет. Откройте свежий просмотр синхронизации.",
  preview_required: "Сначала откройте свежий просмотр изменений.",
  recovery_needed: "Осталась незавершённая операция Rekordbox. Её нужно отдельно восстановить перед новым просмотром.",
  recovery_restored_preview_required: "Незавершённая операция восстановлена. Перед новыми изменениями нужен свежий просмотр.",
  reconcile_failed: "Проверка результата Rekordbox не завершилась. Не повторяйте действие вслепую; сначала обновите состояние.",
  rekordbox_running: "Закройте Rekordbox и повторите действие.",
  source_incomplete: "Источник загружен не полностью. Изменения не применялись.",
  source_duplicate_identity: "В источнике повторяется один и тот же трек. Проверьте состав плейлиста.",
  source_identity_invalid: "В источнике есть трек без надёжного идентификатора. Изменения не применялись.",
  source_unavailable: "Источник сейчас недоступен. Изменения не применялись.",
  source_unknown: "Источник плейлиста не найден. Изменения не применялись.",
  source_unresolved: "Не все треки разрешены в локальные файлы. Изменения не применялись.",
  stale_preview: "Состав, порядок или файлы изменились после просмотра. Откройте свежий просмотр.",
  unsupported_recovery_schema: "Формат восстановления не поддерживается этой версией DeckPipe. Нужна ручная диагностика.",
  wav_not_prepared: "Не для всех треков подготовлены и проверены WAV-файлы.",
  wav_preparation_failed: "Не удалось подготовить и проверить WAV-файлы. База Rekordbox не изменялась."
});
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
  provider: "Подключение к сервису",
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
  const authGenerations = Object.fromEntries(Object.entries(providerAuth).map(([provider, state]) => [provider, state.generation]));
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
    const rawDetail = payload.detail;
    let detail = typeof rawDetail === "object" && rawDetail !== null ? rawDetail.message || rawDetail.code || `HTTP ${response.status}` : rawDetail || response.statusText || `HTTP ${response.status}`;
    const service = String(rawDetail?.service || "").toLowerCase();
    const provider = service === "soundcloud" || service === "sc" ? "sc" : service === "deezer" ? "deezer" : null;
    const providerError = provider && String(rawDetail?.code || "").startsWith("provider_");
    const authRequired = providerError && rawDetail.code === "provider_auth_required";
    if (authRequired) {
      detail = `Войдите в ${provLabel(provider)} снова через кнопку подключения вверху. Сохранённая музыка доступна в библиотеке.`;
      if (authGenerations[provider] === providerAuth[provider].generation) {
        providerAuth[provider].required = true;
        renderProviderAccount(provider);
      }
    }
    throw Object.assign(new Error(detail), {
      kind: providerError ? "provider" : classifyApiFailure(path, response.status, detail),
      status: response.status,
      payload,
      authProvider: authRequired ? provider : null,
      authGeneration: authRequired ? authGenerations[provider] : null
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
function isLoginCancelled(error) {
  return String(error && error.message ? error.message : error) === LOGIN_CANCELLED;
}
function showStatus(message) {
  const region = $("#statusRegion");
  region.textContent = message || "";
  region.classList.toggle("hidden", !message);
}
function showError(error, fallbackKind) {
  if (isSupersededAuthError(error)) return;
  const region = $("#errorRegion");
  region.textContent = describeError(error, fallbackKind);
  region.classList.remove("hidden");
}
function isSupersededAuthError(error) {
  return !!error?.authProvider && error.authGeneration !== providerAuth[error.authProvider]?.generation;
}
function renderProviderAccount(provider) {
  const control = $(provider === "sc" ? "#btnLoginSc" : "#btnLoginDeezer");
  const account = nativeAccounts[provider];
  const required = providerAuth[provider].required;
  control.textContent = `${provLabel(provider)}: ${required ? "войти снова" : account?.connected ? account.account?.name || "подключён" : "вход"}`;
  setAuthActive(control, !!account?.connected && !required);
}
function clearError() {
  const region = $("#errorRegion");
  region.textContent = "";
  region.classList.add("hidden");
}
function rbErrorMessage(error) {
  if (!error) return "";
  return RB_ERROR_MESSAGES[error.code] || "Операция Rekordbox не завершена. Обновите просмотр и повторите действие.";
}
function showRbResultError(result, fallback = "Операция Rekordbox не завершена.") {
  const unresolved = Array.isArray(result?.unresolved) ? result.unresolved.length : 0;
  const message = result?.error ? rbErrorMessage(result.error) : unresolved ? `Не разрешены локальные файлы: ${unresolved}. Изменения не применялись.` : fallback;
  showError(new Error(message), "rekordbox");
}
function canonicalSourceKey(kind, id) {
  const value = String(id ?? "");
  if (kind === "sc") return `sc:${value.replace(/^sc:/, "")}`;
  if (kind === "local") return `local:${value.replace(/^local:/, "")}`;
  return value;
}
function captureRbSelection() {
  if (!current) return null;
  return {
    kind: current.kind,
    id: current.id,
    title: current.title,
    key: canonicalSourceKey(current.kind, current.id),
    epoch: rbSelectionEpoch
  };
}
function setRbOperationBusy(busy) {
  rbOperationBusy = busy;
  const syncButton = $("#rbSyncBtn");
  const wavButton = $("#flipBtn");
  if (syncButton) syncButton.disabled = busy;
  if (wavButton) wavButton.disabled = busy;
  const recoveryButton = $("#rbRecoveryBtn");
  if (recoveryButton) recoveryButton.disabled = busy;
}
function invalidateRbOperation({ selectionChanged = true } = {}) {
  cancelRbDialog();
  if (selectionChanged) rbSelectionEpoch += 1;
  rbOperationGeneration += 1;
  rbMediaMode = null;
  setRbOperationBusy(false);
}
function beginRbOperation({ global = false } = {}) {
  if (rbOperationBusy || !global && !current) return null;
  const selection = global ? null : captureRbSelection();
  const generation = ++rbOperationGeneration;
  setRbOperationBusy(true);
  return { generation, selection };
}
function rbOperationIsCurrent(operation) {
  if (operation && operation.selection === null) return operation.generation === rbOperationGeneration;
  const selected = captureRbSelection();
  return !!operation && operation.generation === rbOperationGeneration && !!selected && selected.epoch === operation.selection.epoch && selected.key === operation.selection.key && selected.title === operation.selection.title;
}
function finishRbOperation(operation) {
  if (operation && operation.generation === rbOperationGeneration) setRbOperationBusy(false);
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
  } else if (["error", "ambiguous", "offline"].includes(status)) {
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
function settleRbDialog(value, { hide = true } = {}) {
  const pending = pendingRbDialog;
  if (!pending) return false;
  pendingRbDialog = null;
  if (hide && activeDialogKind === "rb") {
    activeDialogKind = null;
    setHidden($("#modalOverlay"), true);
    releaseDialogFocus();
  }
  pending.resolve(value);
  return true;
}
function confirmRbDialog() {
  if (!pendingRbDialog) return;
  const value = pendingRbDialog.value();
  settleRbDialog(value);
}
function cancelRbDialog({ hide = true } = {}) {
  settleRbDialog(null, { hide });
}
function configureRbDialog({ title, children, confirmLabel, cancelLabel, value }) {
  if (activeDialogKind === "auth" && !$("#modalOverlay").classList.contains("hidden")) return Promise.resolve(null);
  cancelRbDialog();
  activeDialogKind = "rb";
  $("#loginTitle").textContent = title;
  $("#modal").setAttribute("aria-labelledby", "loginTitle");
  $("#modalOverlay").setAttribute("data-dialog-kind", "rb");
  const closeButton = $('[data-action="close-login"]');
  if (closeButton) closeButton.setAttribute("aria-label", "Закрыть подтверждение Rekordbox");
  replaceChildren($("#loginSteps"), children);
  const accept = $("#btnAuthStart");
  accept.textContent = confirmLabel;
  accept.dataset.action = "confirm-rb-dialog";
  setHidden(accept, false);
  const cancel = $("#btnAuthRetry");
  cancel.textContent = cancelLabel;
  cancel.dataset.action = "cancel-rb-dialog";
  setHidden(cancel, false);
  setHidden($("#btnChangeAccount"), true);
  setHidden($("#btnLogout"), true);
  setHidden($("#scImport"), true);
  $("#loginResult").textContent = "";
  setResultState(null);
  const promise = new Promise((resolve) => {
    pendingRbDialog = { resolve, value };
  });
  openDialog(accept);
  return promise;
}
function confirmRbAction(operation, title, message, confirmLabel) {
  if (!rbOperationIsCurrent(operation)) return Promise.resolve(null);
  return configureRbDialog({
    title,
    children: [create("p", { className: "rb-dialog-copy", text: message })],
    confirmLabel,
    cancelLabel: "Отмена",
    value: () => true
  });
}
function chooseRbTargetDialog(operation, title, candidates) {
  if (!rbOperationIsCurrent(operation)) return Promise.resolve(null);
  const select = create(
    "select",
    {
      id: "rbTargetSelect",
      className: "rb-target-select",
      attrs: { "aria-label": `Целевой плейлист Rekordbox для ${title}` }
    },
    candidates.map((item) => create("option", {
      value: String(item.id),
      text: `${item.name} — ${item.count ?? "?"} треков — ID ${item.id}${item.folder ? ` — ${item.folder}` : ""}`
    }))
  );
  select.value = String(candidates[0].id);
  return configureRbDialog({
    title: "Выберите плейлист Rekordbox",
    children: [
      create("p", { text: `В Rekordbox несколько плейлистов «${title}». Выберите точную цель:` }),
      select
    ],
    confirmLabel: "Использовать выбранный",
    cancelLabel: "Отмена",
    value: () => select.value
  });
}
function closeLogin({ cancel = true } = {}) {
  if (activeDialogKind === "rb") {
    cancelRbDialog();
    return;
  }
  if (cancel) cancelAuthAttempt().catch((error) => showError(error, "security"));
  activeDialogKind = null;
  setHidden($("#modalOverlay"), true);
  releaseDialogFocus();
}
async function loadConfig(expectedAuthEpoch = null) {
  const authGenerations = Object.fromEntries(Object.entries(providerAuth).map(([provider, state]) => [provider, state.generation]));
  const config = await api("/api/config");
  if (expectedAuthEpoch !== null && expectedAuthEpoch !== authAttemptEpoch) return null;
  libraryConfigured = config.music_root_configured === true;
  $("#musicRoot").value = config.music_root || "";
  $("#wavMode").value = config.wav_mode || "source";
  $("#numbering").checked = !!config.numbering;
  $("#btnLoginDeezer").textContent = config.user && config.user.email ? `Deezer: ${config.user.email}` : "Deezer: вход";
  setAuthActive($("#btnLoginDeezer"), !!(config.user && config.user.email));
  $("#btnLoginSc").textContent = config.sc_user ? `SC: ${config.sc_user}` : "SC: вход";
  setAuthActive($("#btnLoginSc"), !!config.sc_user);
  if (isPackagedAppOrigin()) {
    const state = await invoke("auth_status", { requestId: null });
    if (expectedAuthEpoch !== null && expectedAuthEpoch !== authAttemptEpoch) return null;
    for (const provider of ["deezer", "sc"]) {
      if (authGenerations[provider] === providerAuth[provider].generation) {
        nativeAccounts[provider] = state.accounts?.[provider];
      }
      renderProviderAccount(provider);
    }
  }
  if (expectedAuthEpoch !== null && expectedAuthEpoch !== authAttemptEpoch) return null;
  if (!libraryConfigured) {
    libraryReady = false;
    renderRootSetup();
  }
  return config;
}
async function openLogin(service) {
  invalidateRbOperation({ selectionChanged: false });
  const epoch = authAttemptEpoch + 1;
  const previousRequest = activeAuthRequest;
  authAttemptEpoch = epoch;
  activeAuthRequest = null;
  clearAuthPoll();
  loginService = service;
  clearError();
  if (previousRequest) {
    try {
      await invoke("auth_cancel", { requestId: previousRequest });
    } catch (error) {
      if (epoch === authAttemptEpoch) showError(error, "security");
      return;
    }
  }
  if (epoch !== authAttemptEpoch) return;
  if (!isPackagedAppOrigin()) {
    showError(new Error("Вход через браузер доступен в приложении DeckPipe для Windows."), "security");
    return;
  }
  let state;
  try {
    state = await invoke("auth_status", { requestId: null });
  } catch (error) {
    if (epoch === authAttemptEpoch && !isLoginCancelled(error)) showError(error, "security");
    return;
  }
  if (epoch !== authAttemptEpoch) return;
  nativeAccounts = state.accounts || {};
  const account = nativeAccounts[service];
  if (!account?.connected || providerAuth[service].required) return tauriLogin(service);
  renderLoginDialog(service);
  setResultState("ok");
  $("#loginResult").textContent = `Подключён: ${account.account?.name || provLabel(service)}`;
  setHidden($("#btnLogout"), false);
}
function renderLoginDialog(service) {
  cancelRbDialog();
  activeDialogKind = "auth";
  loginService = service;
  $("#loginTitle").textContent = `Вход в ${provLabel(service)}`;
  $("#modalOverlay").setAttribute("data-dialog-kind", "auth");
  const closeButton = $('[data-action="close-login"]');
  if (closeButton) closeButton.setAttribute("aria-label", "Закрыть вход");
  $("#loginResult").textContent = "";
  setResultState(null);
  setHidden($("#scImport"), true);
  setHidden($("#btnLogout"), !nativeAccounts[service]?.connected);
  setHidden($("#btnChangeAccount"), !nativeAccounts[service]?.connected);
  setHidden($("#btnAuthRetry"), true);
  $("#btnAuthRetry").dataset.action = "retry-login";
  $("#btnAuthRetry").textContent = "Повторить попытку";
  $("#btnAuthStart").dataset.action = "do-login";
  $("#btnAuthStart").textContent = "Открыть окно входа";
  setHidden($("#btnAuthStart"), false);
  replaceChildren($("#loginSteps"), [
    create("p", { text: "DeckPipe откроет отдельное окно входа. Войдите на странице сервиса в этом окне." }),
    create("p", { className: "dim", text: "DeckPipe помнит только собственную сессию входа; первая авторизация не использует вход из обычного браузера. Пароль вводится только на сайте сервиса." })
  ]);
  openDialog($("#btnAuthStart"));
}
function clearAuthPoll() {
  if (authPollTimer) clearTimeout(authPollTimer);
  authPollTimer = null;
}
async function cancelAuthAttempt() {
  const requestId = activeAuthRequest;
  activeAuthRequest = null;
  authAttemptEpoch += 1;
  clearAuthPoll();
  if (requestId) await invoke("auth_cancel", { requestId });
}
async function tauriLogin(service) {
  const epoch = authAttemptEpoch + 1;
  try {
    await cancelAuthAttempt();
    if (epoch !== authAttemptEpoch) return;
    renderLoginDialog(service);
    const state = await invoke("auth_begin", { provider: service });
    if (epoch !== authAttemptEpoch) {
      if (state?.requestId) await invoke("auth_cancel", { requestId: state.requestId });
      return;
    }
    activeAuthRequest = state.requestId;
    await renderAuthState(state, epoch);
  } catch (error) {
    if (epoch === authAttemptEpoch && !isLoginCancelled(error)) showError(error, "security");
  }
}
function waitingAuthNotice(state) {
  if (state.status === "validating") return "Проверяем вход…";
  const notices = {
    AUTH_POPUP_BLOCKED: "Дополнительное окно входа не открылось. Закройте лишние окна входа и попробуйте ещё раз.",
    AUTH_PROVIDER_REJECTED: "Сервис не принял вход. Войдите снова в этом же окне.",
    AUTH_BACKEND_UNAVAILABLE: "Проверка входа временно недоступна. Повторите попытку.",
    AUTH_INVALID_RESPONSE: "Сервис вернул неполный ответ. Повторите попытку."
  };
  return notices[state.errorCode] || "Ожидаем вход в окне DeckPipe…";
}
function canRetryAuth(state) {
  return state.status === "waiting_browser" && ["AUTH_BACKEND_UNAVAILABLE", "AUTH_INVALID_RESPONSE"].includes(state.errorCode);
}
async function renderAuthState(state, epoch = authAttemptEpoch) {
  if (epoch !== authAttemptEpoch || state.requestId && state.requestId !== activeAuthRequest) return;
  if (state.status === "connected") {
    const provider = state.provider || loginService;
    providerAuth[provider].generation += 1;
    providerAuth[provider].required = false;
    const generation = providerAuth[provider].generation;
    nativeAccounts[provider] = { connected: true, account: state.account };
    renderProviderAccount(provider);
    activeAuthRequest = null;
    clearAuthPoll();
    setResultState("ok");
    $("#loginResult").textContent = `Подключён: ${state.account?.name || provLabel(state.provider)}`;
    setHidden($("#btnLogout"), false);
    setHidden($("#btnChangeAccount"), false);
    setHidden($("#btnAuthRetry"), true);
    showStatus($("#loginResult").textContent);
    await loadConfig();
    if (generation === providerAuth[provider].generation && libraryConfigured) await loadPlaylists();
    return;
  }
  if (!["waiting_browser", "validating"].includes(state.status)) {
    activeAuthRequest = null;
    clearAuthPoll();
    setResultState("error");
    setHidden($("#btnAuthRetry"), true);
    $("#loginResult").textContent = {
      expired: "Время входа истекло. Откройте окно входа снова.",
      cancelled: "Вход отменён.",
      failed: {
        AUTH_BROWSER_UNAVAILABLE: "Не удалось открыть окно входа DeckPipe.",
        AUTH_PROFILE_UNAVAILABLE: "Не удалось подготовить профиль входа DeckPipe.",
        AUTH_BROWSER_CLEAR_FAILED: "Не удалось очистить сохранённый вход."
      }[state.errorCode] || "Вход не завершён. Повторите подключение."
    }[state.status] || "Вход не завершён. Повторите подключение.";
    return;
  }
  setResultState(null);
  $("#loginResult").textContent = waitingAuthNotice(state);
  setHidden($("#btnAuthRetry"), !canRetryAuth(state));
  const requestId = activeAuthRequest;
  clearAuthPoll();
  authPollTimer = setTimeout(async () => {
    if (!requestId || epoch !== authAttemptEpoch || requestId !== activeAuthRequest) return;
    try {
      const next = await invoke("auth_status", { requestId });
      if (epoch === authAttemptEpoch && requestId === activeAuthRequest) await renderAuthState(next, epoch);
    } catch (error) {
      if (epoch === authAttemptEpoch && requestId === activeAuthRequest) {
        activeAuthRequest = null;
        showError(error, "security");
      }
    }
  }, 1e3);
}
async function retryAuthLogin() {
  await tauriLogin(loginService);
}
async function forgetAuthSession(service, operationEpoch) {
  try {
    await cancelAuthAttempt();
  } catch {
    if (operationEpoch === authAttemptEpoch) {
      showError(new Error(`Не удалось выйти и забыть вход ${provLabel(service)}. Повторите попытку.`), "security");
    }
    return false;
  }
  if (operationEpoch !== authAttemptEpoch) return false;
  try {
    await invoke("auth_logout", { provider: service });
  } catch {
    if (operationEpoch === authAttemptEpoch) {
      showError(new Error(`Не удалось выйти и забыть вход ${provLabel(service)}. Повторите попытку.`), "security");
    }
    return false;
  }
  if (operationEpoch !== authAttemptEpoch) return false;
  nativeAccounts[service] = { connected: false };
  providerAuth[service].generation += 1;
  providerAuth[service].required = false;
  return true;
}
async function logoutProvider() {
  const service = loginService;
  const operationEpoch = authAttemptEpoch + 1;
  if (!await forgetAuthSession(service, operationEpoch) || operationEpoch !== authAttemptEpoch) return;
  current = null;
  tracks = [];
  closeLogin({ cancel: false });
  try {
    const config = await loadConfig(operationEpoch);
    if (!config || operationEpoch !== authAttemptEpoch) return;
    showStatus(`Аккаунт ${provLabel(service)} отключён. Музыка сохранена на диске.`);
    if (libraryConfigured) {
      await loadPlaylists();
      if (operationEpoch !== authAttemptEpoch) return;
    }
  } catch (error) {
    if (operationEpoch === authAttemptEpoch) showError(error, "security");
  }
}
async function changeAuthAccount() {
  const service = loginService;
  const operationEpoch = authAttemptEpoch + 1;
  if (!await forgetAuthSession(service, operationEpoch) || operationEpoch !== authAttemptEpoch) return;
  await tauriLogin(service);
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
  await loadConfig();
  invalidateRbOperation();
  current = null;
  tracks = [];
  setFlexVisible($("#toolbar"), false);
  setEmpty("Общая папка подключена. Выберите плейлист слева.");
  await scanLibrary();
  await loadPlaylists();
}
function renderRootSetup() {
  playlistLoadGeneration += 1;
  setFlexVisible($("#toolbar"), false);
  replaceChildren($("#playlists"), create("div", { className: "list-pad dim", text: "Сначала подключите музыкальную библиотеку." }));
  replaceChildren($("#tracks"), create("section", { className: "root-setup" }, [
    create("h2", { text: "Где хранится ваша музыка?" }),
    create("p", { text: "Выберите общую папку со всей музыкой. DeckPipe найдёт треки во вложенных папках и покажет их в плейлистах и «Лайках» без повторной загрузки." }),
    button("Выбрать общую папку…", "choose-root"),
    create("p", { className: "dim", text: "Затем можно привязать к отдельному плейлисту свою папку, даже если их названия различаются." })
  ]));
}
async function chooseRoot() {
  const result = await api("/api/browse");
  if (!result.path) return;
  $("#musicRoot").value = result.path;
  await saveRoot();
}
async function scanLibrary() {
  if (!libraryConfigured) {
    renderRootSetup();
    return false;
  }
  const generation = ++libraryPollGeneration;
  libraryReady = false;
  let state = await api("/api/library/scan", { body: {} });
  while (generation === libraryPollGeneration) {
    $("#libraryState").textContent = state.state === "scanning" ? `Сканируем общую библиотеку… файлов: ${state.files || 0}` : `В каталоге: ${state.files || 0} файлов${state.error ? ` · ${state.error}` : ""}`;
    if (state.state !== "scanning") {
      libraryReady = state.state === "ready";
      if (state.error) showError(new Error(state.error), "state");
      return libraryReady;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
    state = await api("/api/library/status");
  }
  return false;
}
function requireLibraryReady() {
  if (!libraryConfigured) {
    renderRootSetup();
    return false;
  }
  if (!libraryReady) {
    showError(new Error("Дождитесь сканирования общей папки или подключите недоступный носитель."), "state");
    return false;
  }
  return true;
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
function beginPlaylistLoad() {
  const generation = ++playlistLoadGeneration;
  const sourceTab = tab;
  if (libraryConfigured) replaceChildren($("#playlists"), create("div", { id: "empty", text: "Загружаем список…" }));
  return () => generation === playlistLoadGeneration && sourceTab === tab;
}
async function loadPlaylists() {
  const isCurrent = beginPlaylistLoad();
  if (!libraryConfigured) {
    renderRootSetup();
    return;
  }
  try {
    clearError();
    if (tab === "sc") return await loadScSources(isCurrent);
    if (tab === "local") return await loadLocalPlaylists(isCurrent);
    if (tab === "errors") return await loadErrors(isCurrent);
    if (tab === "search") return await loadSearchTargets(isCurrent);
    const playlists = await api("/api/playlists");
    if (!isCurrent()) return;
    replaceChildren($("#playlists"), playlists.map((playlist) => {
      const cover = create("img", { attrs: { alt: "" } });
      const coverUrl = safeCoverUrl(playlist.cover);
      if (coverUrl) cover.src = coverUrl;
      const active = current && current.id === playlist.id;
      return playlistControl(playlist.title, cover, [
        create("span", { className: "t", text: playlist.title }),
        create("span", { className: "c" }, [
          text(`${playlist.count} треков · в папке: ${playlist.ok || 0}`),
          playlist.errors ? create("span", { className: "badge-err", text: ` ⚠ ${playlist.errors}` }) : null
        ])
      ], "select-playlist", { id: playlist.id, title: playlist.title }, active);
    }));
  } catch (error) {
    if (!isCurrent() || isSupersededAuthError(error)) return;
    showError(error, tab === "errors" ? "state" : "local");
    replaceChildren($("#playlists"), create("div", { id: "empty", text: describeError(error) }));
  }
}
async function loadLocalPlaylists(isCurrent = beginPlaylistLoad()) {
  const playlists = await api("/api/local/playlists");
  if (!isCurrent()) return;
  if (!playlists.length) {
    replaceChildren($("#playlists"), create("div", { id: "empty", text: "Локальных плейлистов пока нет. Их можно создать во вкладке «Поиск»." }));
    return;
  }
  replaceChildren($("#playlists"), playlists.map((playlist) => {
    const key = canonicalSourceKey("local", playlist.key || playlist.id);
    const active = current && canonicalSourceKey(current.kind, current.id) === key;
    return playlistControl(playlist.title, create("span", { className: "local-cover", text: "♪", attrs: { "aria-hidden": "true" } }), [
      create("span", { className: "t", text: playlist.title }),
      create("span", { className: "c", text: `${playlist.count ?? "?"} треков · локальный источник` })
    ], "select-local-playlist", { id: key, title: playlist.title }, active);
  }));
}
async function loadErrors(isCurrent = beginPlaylistLoad()) {
  const [localResult, remoteResult] = await Promise.allSettled([api("/api/errors?include_status=true"), api("/api/remote-actions")]);
  if (!isCurrent()) return;
  const localValue = localResult.status === "fulfilled" ? localResult.value : [];
  const errors = Array.isArray(localValue) ? localValue : localValue.items || [];
  const warnings = Array.isArray(localValue) ? [] : localValue.errors || [];
  for (const warning of warnings) showError(new Error(warning.message), "state");
  const remote = remoteResult.status === "fulfilled" ? remoteResult.value.filter((action) => action.state !== "succeeded") : [];
  for (const result of [localResult, remoteResult]) if (result.status === "rejected") showError(result.reason, "state");
  window._errors = errors;
  if (!errors.length && !remote.length) {
    replaceChildren($("#playlists"), create("div", { id: "empty", text: warnings.length || [localResult, remoteResult].some((r) => r.status === "rejected") ? "Список ошибок загружен не полностью" : "Ошибок нет" }));
    return;
  }
  const children = errors.length ? [
    create("div", { className: "list-pad" }, button(`Повторить все (${errors.length})`, "retry-all", { className: "full-width" }))
  ] : [];
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
  remote.forEach((action) => children.push(create("div", { className: "pl pl-static" }, [
    create("span", { className: "pl-fill" }, [
      create("span", { className: "t", text: "Добавление в плейлист Deezer" }),
      create("span", { className: "c", text: `Плейлист ${action.target_id} · ${action.track_ids.length} треков` }),
      create("span", { className: "badge-err", text: action.last_error?.message || "Добавление ещё не завершено" })
    ]),
    button("Повторить", "retry-remote", { className: "ghost button-small", dataset: { id: action.id } })
  ])));
  replaceChildren($("#playlists"), children);
}
async function retryRemote(id) {
  try {
    const result = await api(`/api/remote-actions/${encodeURIComponent(id)}/retry`, { body: {} });
    if (result.state === "succeeded") showStatus("Плейлист Deezer обновлён. Повторная загрузка файлов не требовалась.");
  } finally {
    if (tab === "errors") await loadErrors();
  }
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
  invalidateRbOperation();
  tab = nextTab;
  $("#tab-deezer").classList.toggle("active", nextTab === "deezer");
  $("#tab-sc").classList.toggle("active", nextTab === "sc");
  $("#tab-local").classList.toggle("active", nextTab === "local");
  $("#tab-search").classList.toggle("active", nextTab === "search");
  $("#tab-errors").classList.toggle("active", nextTab === "errors");
  setFlexVisible($("#sc-add"), nextTab === "sc");
  setFlexVisible($("#searchbar"), nextTab === "search");
  setFlexVisible($("#searchFilters"), nextTab === "search");
  current = null;
  setFlexVisible($("#toolbar"), false);
  if (!libraryConfigured) {
    renderRootSetup();
    return;
  }
  if (nextTab === "search") {
    setEmpty("Слева — цель (плейлист Deezer / источник SC / локальный плейлист / своя папка). Ищите треки, отмечайте чекбоксами или качайте по одному. Альбомы и сеты раскрываются по клику — внутри треки качаются поштучно или все сразу.");
    setSearchFilter(searchFilter);
    loadSearchTargets();
    return;
  }
  setEmpty(nextTab === "errors" ? "Треки с ошибками загрузки/верификации — слева. Кнопка ↻ перезапускает сломавшийся этап." : `Выберите ${nextTab === "sc" ? "источник" : nextTab === "local" ? "локальный плейлист" : "плейлист"} слева`);
  loadPlaylists();
}
async function loadSearchTargets(isCurrent = beginPlaylistLoad()) {
  if (!libraryConfigured) {
    renderRootSetup();
    return;
  }
  const results = await Promise.allSettled([
    api("/api/playlists"),
    api("/api/sc/sources"),
    api("/api/local/playlists")
  ]);
  if (!isCurrent()) return;
  const [deezerRows, scRows, localRows] = results.map((result) => result.status === "fulfilled" ? result.value : []);
  const failures = results.filter((result) => result.status === "rejected" && !isSupersededAuthError(result.reason));
  if (failures.length) showError(new Error(failures.map((result) => result.reason.message).join("; ")));
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
  if (tab === "search") await loadSearchTargets();
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
  if (tab === "search") await loadSearchTargets();
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
    if (serviceData.errors?.length) sections.push(create("div", {
      className: "live-region error-region",
      text: `${label}: ${serviceData.errors.map((error) => error.message || error.code).join("; ")}`
    }));
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
  if (!requireLibraryReady()) return null;
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
  showDownloadResult(await api("/api/search/download", { body }));
}
async function dlAlbumTrack(service, index, trackIndex) {
  const track = searchExpanded[`${service}:${index}`].tracks[trackIndex];
  const body = _dlPayload([{ id: track.id, title: track.title, artist: track.artist, duration: track.duration, url: track.url, provider: service }]);
  if (!body) return;
  showDownloadResult(await api("/api/search/download", { body }));
}
async function dlWholeAlbum(service, index) {
  const album = window._search[service].albums[index];
  const albumTracks = searchExpanded[`${service}:${index}`].tracks;
  const body = _dlPayload(albumTracks.map((track) => ({ id: track.id, title: track.title, artist: track.artist, duration: track.duration, url: track.url, provider: service })));
  if (!body) return;
  showDownloadResult(await api("/api/search/download", { body }));
}
async function dlBasket() {
  const selected = Object.values(searchSel).map((track) => ({ id: track.id, title: track.title, artist: track.artist, duration: track.duration, url: track.url, provider: track.provider }));
  const body = _dlPayload(selected);
  if (!body) return;
  showDownloadResult(await api("/api/search/download", { body }));
  searchSel = {};
  _renderBasket();
  renderSearch();
}
function showDownloadResult(result) {
  const parts = [result.job_id ? "Локальная загрузка поставлена в очередь." : "Новые файлы скачивать не требуется."];
  if (result.already_present) parts.push(`Уже есть в библиотеке: ${result.already_present}.`);
  if (result.needs_attention) parts.push(`Требуют выбора или проверки: ${result.needs_attention}.`);
  const remote = result.remote_action;
  if (remote?.state === "succeeded") parts.push("Плейлист Deezer обновлён.");
  if (remote && remote.state !== "succeeded") {
    parts.push("Добавление в Deezer не завершено. Повтор доступен во вкладке «Ошибки».");
    showError(new Error(remote.last_error?.message || "Deezer: добавление ожидает повтора."), "state");
  }
  showStatus(parts.join(" "));
  if (result.job_id) startPolling();
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
async function loadScSources(isCurrent = beginPlaylistLoad()) {
  try {
    await api("/api/sc/sync-account", { body: {} });
  } catch (error) {
    if (!isCurrent()) return;
    showError(error, "provider");
  }
  if (!isCurrent()) return;
  const sources = await api("/api/sc/sources");
  if (!isCurrent()) return;
  if (!sources.length) {
    replaceChildren($("#playlists"), create("div", { id: "empty", text: "Войдите через «SC: вход» — плейлисты аккаунта появятся сами. Или добавьте URL выше." }));
    return;
  }
  replaceChildren($("#playlists"), sources.map((source) => {
    const active = current && current.id === source.id;
    return playlistControl(source.title, create("img", { attrs: { alt: "" } }), [
      create("span", { className: "t", text: source.title }),
      create("span", { className: "c" }, [
        text(`${source.count ?? "?"} треков · в папке: ${source.ok || 0}`),
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
async function selectScSource(id, title) {
  invalidateRbOperation();
  current = { kind: "sc", id, title };
  await Promise.all([loadPlaylists(), loadTracks(current)]);
}
async function selectPlaylist(id, title) {
  invalidateRbOperation();
  current = { kind: "deezer", id, title };
  await Promise.all([loadPlaylists(), loadTracks(current)]);
}
async function selectLocalPlaylist(id, title) {
  invalidateRbOperation();
  current = { kind: "local", id: canonicalSourceKey("local", id), title };
  await Promise.all([loadPlaylists(), loadTracks(current)]);
}
async function loadTracks(playlist) {
  if (!libraryConfigured) {
    renderRootSetup();
    return;
  }
  const selectionEpoch = rbSelectionEpoch;
  setFlexVisible($("#toolbar"), true);
  $("#pltitle").textContent = playlist.title;
  setEmpty("Загрузка… (большие плейлисты — до ~20 сек)");
  try {
    const url = playlist.kind === "sc" ? `/api/sc/sources/${playlist.id}/tracks` : `/api/playlists/${canonicalSourceKey(playlist.kind, playlist.id)}/tracks?title=${encodeURIComponent(playlist.title)}`;
    const data = await api(url);
    if (selectionEpoch !== rbSelectionEpoch || current !== playlist) return;
    tracks = data.tracks;
    $("#plpath").textContent = data.path || "";
    renderTracks();
    try {
      await refreshRbMediaState(playlist, { quiet: true });
    } catch {
      if (selectionEpoch === rbSelectionEpoch && current === playlist) renderRbMediaMode("blocked");
    }
  } catch (error) {
    if (selectionEpoch !== rbSelectionEpoch || current !== playlist) return;
    if (isSupersededAuthError(error)) return;
    showError(error, "state");
    setEmpty(`Не удалось загрузить: ${describeError(error, "state")}`, "Повторить", "rescan");
  }
}
function renderTracks() {
  const ok = tracks.filter((track) => track.status === "ok").length;
  const miss = tracks.filter((track) => track.status === "missing").length;
  const err = tracks.length - ok - miss;
  $("#plstats").textContent = `✔ ${ok} · ✖ ${miss} · ⚠ ${err}`;
  renderRbMediaMode(rbMediaMode);
  const table = create("table", { attrs: { "aria-label": "Треки плейлиста" } });
  const thead = create("thead");
  thead.append(create("tr", {}, [
    create("th", {}, create("input", { type: "checkbox", id: "all", action: "toggle-all", attrs: { "aria-label": "Выбрать все недостающие треки" } })),
    create("th", { text: "Статус" }),
    create("th", { text: "Название" }),
    create("th", { text: "Исполнитель" }),
    create("th", { text: "Альбом" }),
    create("th", { text: "⏱" }),
    create("th", { text: "Формат" }),
    create("th", { text: "Расположение файла" })
  ]));
  const tbody = create("tbody");
  tracks.forEach((track, index) => {
    const formatCell = create("td");
    const badge = formatBadge(track.format);
    if (badge) formatCell.append(badge);
    if (track.mp3_source) formatCell.append(create("span", { className: "fmt fmt-mp3src", text: "mp3", title: "mp3-источник: после конвертации в WAV кью могут сместиться на ~26 мс" }));
    const locationCell = create("td", { className: "track-location" });
    if (track.file_path) {
      locationCell.append(create("span", { className: "location-label", text: track.location_scope === "library" ? "Есть в общей библиотеке" : "В папке плейлиста" }));
      locationCell.append(create("span", { className: "file-path", text: track.file_path, title: track.file_path }));
    } else if (track.status === "ambiguous" && track.locations?.length) {
      locationCell.append(create("label", { className: "sr-only", attrs: { for: `location-${index}` }, text: `Файл для ${track.title}` }));
      locationCell.append(create(
        "select",
        { id: `location-${index}`, attrs: { "aria-label": `Выберите файл для ${track.title}` } },
        track.locations.map((location) => create("option", { value: location.path, text: location.path }))
      ));
      locationCell.append(button("Использовать этот файл", "confirm-location", { className: "ghost button-small", dataset: { index } }));
    } else {
      locationCell.append(create("span", { className: "dim", text: track.status === "offline" ? "Носитель недоступен" : "—" }));
    }
    tbody.append(create("tr", {}, [
      create("td", {}, create("input", { type: "checkbox", className: "trk", disabled: track.status !== "missing", dataset: { i: index }, attrs: { "aria-label": `Выбрать ${track.title}` } })),
      create("td", {}, statusIcon(track.status)),
      create("td", {}, [text(track.title), track.error ? create("div", { className: "errtext", text: track.error }) : null]),
      create("td", { text: track.artist }),
      create("td", { className: "dim", text: track.album }),
      create("td", { className: "dim", text: fmtDur(track.duration) }),
      formatCell,
      locationCell
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
  if (onlyMissing) selected = selected.filter((track) => track.status === "missing");
  return selected.map((track) => ({
    id: track.id,
    title: track.title,
    artist: track.artist,
    duration: track.duration,
    url: track.url,
    provider: track.provider || (current?.kind === "sc" ? "sc" : "deezer"),
    total: tracks.length
  }));
}
async function confirmLocation(index) {
  const selected = $(`#location-${index}`).value;
  if (!selected) return;
  await api("/api/library/confirm", { body: { track: tracks[index], path: selected } });
  if (current) await loadTracks(current);
}
function downloadUrl(mode) {
  const base = current.kind === "sc" ? `/api/sc/sources/${current.id}/download` : `/api/playlists/${current.id}/download?title=${encodeURIComponent(current.title)}`;
  return base + (base.includes("?") ? "&" : "?") + "mode=" + mode;
}
async function downloadSelected() {
  if (!requireLibraryReady()) return;
  const selected = selectedTracks(true);
  if (!selected.length) {
    showError(new Error("Ничего не выбрано (уже скачанные пропускаются)"), "state");
    return;
  }
  showDownloadResult(await api(downloadUrl("append"), { body: { tracks: selected } }));
}
async function syncPlaylistOrder() {
  if (!requireLibraryReady()) return;
  const order = tracks.map((track) => (track.provider || current.kind) === "sc" ? `sc:${String(track.id).replace(/^sc:/, "")}` : track.id);
  const key = current.kind === "sc" ? `sc:${current.id}` : current.id;
  const renumber = await api(
    `/api/playlists/${key}/renumber?title=${encodeURIComponent(current.title)}`,
    { body: { order, total: tracks.length } }
  );
  const missing = tracks.filter((track) => track.status === "missing").map((track) => ({
    id: track.id,
    title: track.title,
    artist: track.artist,
    duration: track.duration,
    url: track.url,
    provider: track.provider || (current.kind === "sc" ? "sc" : "deezer"),
    position: tracks.indexOf(track) + 1,
    total: tracks.length
  }));
  if (!missing.length) {
    const unresolved = tracks.filter((track) => track.status !== "ok").length;
    showStatus(`Порядок применён к файлам в папке плейлиста (переименовано: ${renumber.renamed}). ` + (unresolved ? `Требуют проверки: ${unresolved}.` : "Все треки найдены в библиотеке."));
    await rescan();
    return;
  }
  if (!confirm(`Перенумеровано: ${renumber.renamed}. Скачать ${missing.length} треков?`)) {
    rescan();
    return;
  }
  showDownloadResult(await api(downloadUrl("playlist_order"), { body: { tracks: missing } }));
}
async function syncAppend() {
  if (!requireLibraryReady()) return;
  const missing = tracks.filter((track) => track.status === "missing").map((track) => ({
    id: track.id,
    title: track.title,
    artist: track.artist,
    duration: track.duration,
    url: track.url,
    provider: track.provider || (current.kind === "sc" ? "sc" : "deezer"),
    total: tracks.length
  }));
  if (!missing.length) {
    const unresolved = tracks.filter((track) => track.status !== "ok").length;
    showStatus(unresolved ? `Требуют проверки: ${unresolved}. Выберите совпадения или откройте ошибки.` : "Все треки найдены в библиотеке.");
    return;
  }
  if (!confirm(`Скачать ${missing.length} треков (новые — вниз списка)?`)) return;
  showDownloadResult(await api(downloadUrl("append"), { body: { tracks: missing } }));
}
function rbPlanCounts(result) {
  return result && result.plan && result.plan.counts || {};
}
function rbCountValue(counts, name) {
  return Number.isFinite(Number(counts[name])) ? Number(counts[name]) : 0;
}
function formatRbSyncCounts(result) {
  const counts = rbPlanCounts(result);
  return `добавить: ${rbCountValue(counts, "add")}; убрать: ${rbCountValue(counts, "remove")}; изменить порядок: ${rbCountValue(counts, "reorder")}; метаданные: ${rbCountValue(counts, "metadata")}; пути: ${rbCountValue(counts, "path")}; не разрешено: ${rbCountValue(counts, "unresolved")}`;
}
function rbSyncHasChanges(result) {
  return !!result?.plan?.hash && !result.unchanged && !result.error && !(result.unresolved || []).length;
}
function rbOrderedPathLines(result) {
  const rows = [...result?.plan?.desired_resolved || []].sort((left, right) => Number(left.position || 0) - Number(right.position || 0));
  if (!rows.length) return ["Порядок файлов: плейлист пуст."];
  return ["Порядок и пути файлов:", ...rows.map((row, index) => `${Number(row.position) || index + 1}. ${row.title || row.provider_id || "Трек"} — ${row.path || "путь не определён"}`)];
}
function rbSharedEffectLines(result) {
  const shared = result?.plan?.shared_content || [];
  if (!shared.length) return ["Общие треки других плейлистов: пути не затрагиваются."];
  const memberships = shared.reduce((total, item) => total + (item.playlist_ids || []).length, 0);
  return [`Общие треки: ${shared.length}; переключение путей затронет ${memberships} связей плейлистов Rekordbox.`];
}
function formatRbPreview(result, action = "Синхронизация") {
  if (!result?.plan) return `${action}
Безопасный план не получен. Изменения не применялись, новый бэкап не создавался.`;
  const plan = result?.plan || {};
  const target = plan.target || {};
  const targetText = !plan.target ? "Цель Rekordbox не удалось определить." : target.id == null ? `Цель Rekordbox: новый плейлист «${target.name || "без названия"}» будет создан.` : `Цель Rekordbox: существующий плейлист «${target.name || "без названия"}», ID ${target.id}.`;
  const lines = [action, targetText, `Изменения: ${formatRbSyncCounts(result)}.`, ...rbOrderedPathLines(result), ...rbSharedEffectLines(result)];
  if (result?.media_state?.mode) lines.push(`Текущий режим файлов: ${rbMediaModeLabel(result.media_state.mode)}.`);
  if (result?.media_state?.membership === "partial") lines.push(`Состав плейлиста неполный: отсутствует ${result.media_state.missing} треков. Сначала синхронизируйте состав и порядок.`);
  if (result?.unchanged) lines.push("Rekordbox уже соответствует этому плану. Применение и новый бэкап не требуются.");
  else lines.push("Это только просмотр: изменения не применялись, новый бэкап не создавался.");
  return lines.join("\n");
}
function rbMediaModeLabel(mode) {
  return {
    original: "исходные файлы",
    wav: "подготовленные WAV",
    mixed: "смешанный — часть WAV, часть исходников",
    empty: "пустой плейлист",
    not_in_rekordbox: "плейлист ещё не создан в Rekordbox",
    blocked: "состояние нельзя определить безопасно",
    membership_sync_required: "сначала синхронизируйте состав плейлиста"
  }[mode] || "не определён";
}
function renderRbMediaMode(mode) {
  rbMediaMode = mode || null;
  const node = $("#flipBtn");
  if (!node) return;
  node.textContent = {
    original: "⇄ Подготовить WAV",
    wav: "⇄ Вернуть оригиналы",
    mixed: "⇄ WAV / оригиналы",
    empty: "⇄ WAV",
    not_in_rekordbox: "⇄ WAV после синхронизации",
    blocked: "⇄ Проверить состояние",
    membership_sync_required: "⇄ WAV после синхронизации"
  }[mode] || "⇄ Проверить WAV";
  node.title = `Режим Rekordbox: ${rbMediaModeLabel(mode)}`;
}
function rbRequestBody(selection, extra = {}) {
  return { playlist_key: selection.key, playlist_title: selection.title, ...extra };
}
function rbTargetMemoryKey(selection) {
  return `${selection.key}
${selection.title}`;
}
function rememberedRbTarget(selection) {
  return rbTargetIdsBySource.get(rbTargetMemoryKey(selection)) || null;
}
function rememberRbTarget(selection, playlistId) {
  if (playlistId != null) rbTargetIdsBySource.set(rbTargetMemoryKey(selection), String(playlistId));
}
function forgetMissingRbTarget(selection, result) {
  if (selection && result?.error?.code === "playlist_not_found") {
    rbTargetIdsBySource.delete(rbTargetMemoryKey(selection));
  }
}
function rbMediaStatePath(selection, playlistId = null) {
  const params = new URLSearchParams({ playlist_key: selection.key, playlist_title: selection.title });
  if (playlistId) params.set("playlist_id", playlistId);
  return `/api/rb/media-state?${params}`;
}
async function refreshRbMediaState(selection = current, { quiet = false, operation = null, playlistId = null } = {}) {
  if (!selection) return null;
  const selectionEpoch = rbSelectionEpoch;
  const captured = selection.key ? selection : {
    ...selection,
    key: canonicalSourceKey(selection.kind, selection.id)
  };
  const result = await api(rbMediaStatePath(captured, playlistId));
  if (operation && !rbOperationIsCurrent(operation)) return null;
  if (!operation) {
    const selected = captureRbSelection();
    if (selectionEpoch !== rbSelectionEpoch || !selected || selected.key !== captured.key || selected.title !== captured.title) return null;
  }
  forgetMissingRbTarget(captured, result);
  renderRbMediaMode(result?.media_state?.membership && result.media_state.membership !== "complete" ? "membership_sync_required" : result?.media_state?.mode);
  if (!quiet && (result?.error || result?.unresolved?.length || result?.media_state?.mode === "blocked")) {
    showRbResultError(result, "Текущее состояние Rekordbox нельзя определить безопасно.");
  }
  return result;
}
async function chooseRbTarget(operation, title) {
  const status = await api("/api/rb/status");
  if (!rbOperationIsCurrent(operation)) return null;
  const candidates = (status.playlists || []).filter((item) => item.name === title);
  if (!candidates.length) {
    showError(new Error("Плейлист Rekordbox с таким названием не найден. Обновите просмотр."), "rekordbox");
    return null;
  }
  const selected = await chooseRbTargetDialog(operation, title, candidates);
  if (!rbOperationIsCurrent(operation) || selected === null) return null;
  return candidates.some((item) => String(item.id) === String(selected)) ? String(selected) : null;
}
async function restorePendingRbOperation(endpoint, body, preview, operation) {
  preview = await api("/api/rb/recovery");
  if (!rbOperationIsCurrent(operation)) return { stop: true };
  const hash = preview?.plan?.hash;
  if (!hash || preview.error) {
    showRbResultError(preview, "Для восстановления не получен безопасный план.");
    return { stop: true };
  }
  showStatus("Обнаружена незавершённая операция Rekordbox. Новые изменения пока не применяются.");
  const confirmed = await confirmRbAction(
    operation,
    "Восстановление Rekordbox",
    `Восстановить состояние до незавершённой операции Rekordbox?
Операция: ${preview.plan.operation_id}. Будут восстановлены база и связанные файлы.
После восстановления для новых изменений потребуется свежий просмотр и отдельное подтверждение.`,
    "Восстановить"
  );
  if (!rbOperationIsCurrent(operation)) return { stop: true };
  if (!confirmed) {
    showStatus("Восстановление отменено. Новые изменения не применялись.");
    return { stop: true };
  }
  if (!rbOperationIsCurrent(operation)) return { stop: true };
  const restored = await api(
    "/api/rb/recovery?confirmation_token=RESTORE_REKORDBOX_OPERATION",
    { body: { expected_plan_hash: hash } }
  );
  if (!rbOperationIsCurrent(operation)) return { stop: true };
  if (restored?.error?.code !== "recovery_restored_preview_required") {
    showRbResultError(restored, "Восстановление не подтверждено. Откройте свежий просмотр состояния.");
    return { stop: true };
  }
  showStatus("Незавершённая операция восстановлена. Загружаем новый план без автоматического применения.");
  if (!endpoint) {
    showStatus("Незавершённая операция восстановлена. Для синхронизации выберите источник и откройте новый просмотр.");
    return { stop: true };
  }
  const fresh = await api(`${endpoint}?dry_run=true`, { body });
  if (!rbOperationIsCurrent(operation)) return { stop: true };
  return { preview: fresh, prefix: "Незавершённая операция восстановлена.\n" };
}
async function rbRecovery() {
  const operation = beginRbOperation({ global: true });
  if (!operation) return;
  try {
    const status = await api("/api/rb/status");
    if (!rbOperationIsCurrent(operation)) return;
    if (status.error) {
      showRbResultError(status);
      return;
    }
    if (!status.recovery?.needed) {
      showStatus("Rekordbox: незавершённых операций нет.");
      return;
    }
    await restorePendingRbOperation(null, null, null, operation);
  } catch (error) {
    if (rbOperationIsCurrent(operation)) showError(error, "rekordbox");
  } finally {
    finishRbOperation(operation);
  }
}
async function loadRbPreview(endpoint, body, operation) {
  let preview = await api(`${endpoint}?dry_run=true`, { body });
  if (!rbOperationIsCurrent(operation)) return { stop: true };
  let prefix = "";
  if (preview?.error?.code === "recovery_needed") {
    const recovery = await restorePendingRbOperation(endpoint, body, preview, operation);
    if (recovery.stop) return recovery;
    preview = recovery.preview;
    prefix = recovery.prefix;
  }
  if (preview?.error?.code === "ambiguous_playlist_target") {
    const playlistId = await chooseRbTarget(operation, body.playlist_title);
    if (!playlistId) return { stop: true };
    body.playlist_id = playlistId;
    preview = await api(`${endpoint}?dry_run=true`, { body });
    if (!rbOperationIsCurrent(operation)) return { stop: true };
  }
  if (preview?.plan?.target?.id != null) rememberRbTarget(operation.selection, preview.plan.target.id);
  forgetMissingRbTarget(operation.selection, preview);
  return { preview, prefix };
}
function showRbAppliedResult(result, action) {
  if (result?.applied && result?.reconciled) {
    const backup = result.backup_id ? " Резервная копия создана и проверена." : "";
    const stateError = result?.media_state?.mode === "blocked" || result?.error?.code === "media_state_unavailable";
    const postCommitError = !!result?.error && !stateError;
    showStatus(`${action}: изменения применены и результат проверен.${backup}` + (stateError ? " Пути записаны, но текущее состояние файлов не удалось обновить; повторите проверку состояния." : "") + (postCommitError ? " Основная запись завершена, но дополнительная обработка после неё не завершилась." : ""));
    if (stateError) showError(new Error(RB_ERROR_MESSAGES.media_state_unavailable), "rekordbox");
    else if (postCommitError) showError(new Error("Изменения Rekordbox записаны и проверены, но дополнительная обработка не завершилась."), "rekordbox");
    renderRbMediaMode(result?.media_state?.mode);
    return true;
  }
  if (!result?.applied && result?.reconciled && result?.unchanged && !result?.error) {
    showStatus(`${action}: состояние уже совпадает с просмотренным планом; запись и новый бэкап не требовались.`);
    renderRbMediaMode(result?.media_state?.mode);
    return true;
  }
  showStatus(`${action} не подтверждена как завершённая. Не повторяйте действие до нового просмотра состояния.`);
  showRbResultError(result);
  return false;
}
async function applyRbPreview(endpoint, body, preview, operation, action) {
  if (!rbOperationIsCurrent(operation)) return false;
  const viewedHash = preview?.plan?.hash;
  if (!viewedHash) {
    showError(new Error("Просмотр не содержит подтверждённого плана. Изменения не применялись."), "rekordbox");
    return false;
  }
  const applied = await api(
    `${endpoint}?dry_run=false&confirmation_token=${encodeURIComponent(RB_APPLY_CONFIRMATION_TOKEN)}`,
    { body: { ...body, expected_plan_hash: viewedHash } }
  );
  if (!rbOperationIsCurrent(operation)) return false;
  forgetMissingRbTarget(operation.selection, applied);
  return showRbAppliedResult(applied, action);
}
async function rbSync() {
  const operation = beginRbOperation();
  if (!operation) return;
  const rememberedTarget = rememberedRbTarget(operation.selection);
  const body = rbRequestBody(operation.selection, rememberedTarget ? { playlist_id: rememberedTarget } : {});
  try {
    const loaded = await loadRbPreview("/api/rb/sync", body, operation);
    if (loaded.stop || !rbOperationIsCurrent(operation)) return;
    const preview = loaded.preview;
    const previewText = `${loaded.prefix || ""}${formatRbPreview(preview, "Синхронизация плейлиста с Rekordbox")}`;
    showStatus(previewText);
    if (preview?.error || preview?.unresolved?.length || !preview?.plan?.hash) {
      showRbResultError(preview, "Безопасный план синхронизации не получен.");
      return;
    }
    if (!rbSyncHasChanges(preview)) return;
    const confirmed = await confirmRbAction(
      operation,
      "Подтверждение синхронизации Rekordbox",
      `${previewText}

Применить именно этот план? Rekordbox должен быть закрыт.`,
      "Применить план"
    );
    if (!rbOperationIsCurrent(operation)) return;
    if (!confirmed) {
      showStatus(`${previewText}
Применение отменено. Изменения не вносились.`);
      return;
    }
    await applyRbPreview("/api/rb/sync", body, preview, operation, "Синхронизация Rekordbox");
  } catch (error) {
    if (rbOperationIsCurrent(operation)) showError(error, "rekordbox");
  } finally {
    finishRbOperation(operation);
  }
}
async function flipWav() {
  const operation = beginRbOperation();
  if (!operation) return;
  const selection = operation.selection;
  try {
    let playlistId = rememberedRbTarget(selection);
    let state = await refreshRbMediaState(selection, { operation, playlistId, quiet: true });
    if (!state || !rbOperationIsCurrent(operation)) return;
    if (state?.error?.code === "recovery_needed") {
      await restorePendingRbOperation(null, null, null, operation);
      return;
    }
    if (state?.error?.code === "ambiguous_playlist_target") {
      playlistId = await chooseRbTarget(operation, selection.title);
      if (!playlistId || !rbOperationIsCurrent(operation)) return;
      rememberRbTarget(selection, playlistId);
      clearError();
      state = await refreshRbMediaState(selection, { operation, playlistId, quiet: true });
      if (!state || !rbOperationIsCurrent(operation)) return;
    }
    if (state.media_state?.membership && state.media_state.membership !== "complete") {
      showStatus("Сначала синхронизируйте состав и порядок плейлиста через → RB. Затем откройте переключение WAV заново.");
      return;
    }
    if (state.error || state.unresolved?.length || state.media_state?.mode === "blocked") {
      showRbResultError(state, "Текущее состояние Rekordbox нельзя определить безопасно.");
      return;
    }
    const mode = state.media_state?.mode;
    let toWav;
    if (mode === "wav") toWav = false;
    else if (mode === "mixed") {
      const answer = prompt("В плейлисте смешаны WAV и исходники. Введите 1, чтобы перевести все треки в WAV, или 2, чтобы вернуть все к исходникам:");
      if (answer === null) return;
      if (answer === "1") toWav = true;
      else if (answer === "2") toWav = false;
      else {
        showError(new Error("Режим не выбран. Пути Rekordbox не изменялись."), "rekordbox");
        return;
      }
    } else if (mode === "original") toWav = true;
    else {
      showError(new Error(`Для режима «${rbMediaModeLabel(mode)}» переключение путей недоступно.`), "rekordbox");
      return;
    }
    if (toWav) {
      const prepareConfirmed = await confirmRbAction(
        operation,
        "Подготовка WAV",
        `Подготовить отдельные проверенные WAV-файлы для «${selection.title}»?
Исходники сохранятся. База Rekordbox на этом шаге не изменяется.`,
        "Подготовить WAV"
      );
      if (!rbOperationIsCurrent(operation)) return;
      if (!prepareConfirmed) {
        showStatus("Подготовка WAV отменена. Файлы и Rekordbox не изменялись.");
        return;
      }
      const prepared = await api("/api/rb/prepare-wav", { body: rbRequestBody(selection, { bit_depth: 16 }) });
      if (!rbOperationIsCurrent(operation)) return;
      showStatus(`Подготовка WAV: создано ${prepared.prepared || 0}, проверено повторно ${prepared.reused || 0}, всего ${prepared.total || 0}.`);
      if (prepared.state !== "prepared" || prepared.error || prepared.unresolved?.length) {
        showRbResultError(prepared, "Подготовка WAV не завершена. База Rekordbox не изменялась.");
        return;
      }
      const freshState = await refreshRbMediaState(selection, { operation, playlistId, quiet: true });
      if (!freshState || freshState.error || freshState.unresolved?.length || freshState.media_state?.mode === "blocked") {
        if (freshState) showRbResultError(freshState, "Состояние после подготовки WAV нельзя определить безопасно.");
        return;
      }
    }
    const body = rbRequestBody(selection, { to_wav: toWav, ...playlistId ? { playlist_id: playlistId } : {} });
    const loaded = await loadRbPreview("/api/flip", body, operation);
    if (loaded.stop || !rbOperationIsCurrent(operation)) return;
    const preview = loaded.preview;
    const action = toWav ? "Переключение путей на WAV" : "Возврат путей к исходникам";
    const previewText = `${loaded.prefix || ""}${formatRbPreview(preview, action)}`;
    showStatus(previewText);
    if (preview?.error || preview?.unresolved?.length || !preview?.plan?.hash) {
      showRbResultError(preview, "Безопасный план переключения путей не получен.");
      return;
    }
    if (!rbSyncHasChanges(preview)) return;
    const confirmed = await confirmRbAction(
      operation,
      toWav ? "Переключение на WAV" : "Возврат к исходникам",
      `${previewText}

Применить именно это переключение путей? Rekordbox должен быть закрыт.`,
      "Применить пути"
    );
    if (!rbOperationIsCurrent(operation)) return;
    if (!confirmed) {
      showStatus(`${previewText}
Применение отменено. Пути Rekordbox не изменялись.`);
      return;
    }
    await applyRbPreview("/api/flip", body, preview, operation, action);
  } catch (error) {
    if (rbOperationIsCurrent(operation)) showError(error, "rekordbox");
  } finally {
    finishRbOperation(operation);
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
  await scanLibrary();
  if (!current) return;
  await loadTracks(current);
  await loadPlaylists();
}
function renderJobs(jobs) {
  replaceChildren($("#jobs"), jobs.slice(0, 5).map((job) => create("div", { className: "job" }, [
    create("strong", { text: job.title }),
    text(`: ${job.done}/${job.total}`),
    job.failed ? create("span", { className: "err", text: ` (ошибок: ${job.failed})` }) : null,
    job.terminal_error ? create("span", { className: "err", text: ` — ${job.terminal_error.message}` }) : null,
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
  "choose-root": () => chooseRoot(),
  "scan-library": () => rescan(),
  "confirm-location": (el) => confirmLocation(Number(el.dataset.index)),
  "send-report": () => sendReport(),
  "switch-tab": (el) => switchTab(el.dataset.tab),
  "add-sc-source": () => addScSource(),
  "rescan": () => rescan(),
  "bind-path": () => bindPath(),
  "download-selected": () => downloadSelected(),
  "sync-playlist-order": () => syncPlaylistOrder(),
  "sync-append": () => syncAppend(),
  "rb-sync": () => rbSync(),
  "rb-recovery": () => rbRecovery(),
  "flip-wav": () => flipWav(),
  "run-search": () => runSearch(),
  "set-search-filter": (el) => setSearchFilter(el.dataset.filter),
  "overlay-close": (el, event) => {
    if (event.target === el) closeLogin();
  },
  "close-login": () => closeLogin(),
  "confirm-rb-dialog": () => confirmRbDialog(),
  "cancel-rb-dialog": () => cancelRbDialog(),
  "do-login": () => tauriLogin(loginService),
  "retry-login": () => retryAuthLogin(),
  "logout-provider": () => logoutProvider(),
  "change-auth-account": () => changeAuthAccount(),
  "import-sc-account": () => importScAccount(),
  "select-playlist": (el) => selectPlaylist(el.dataset.id, el.dataset.title),
  "select-sc-source": (el) => selectScSource(el.dataset.id, el.dataset.title),
  "select-local-playlist": (el) => selectLocalPlaylist(el.dataset.id, el.dataset.title),
  "retry-all": () => retryAll(),
  "retry-one": (el) => retryOne(Number(el.dataset.index)),
  "retry-remote": (el) => retryRemote(el.dataset.id),
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
    if (!libraryConfigured) return;
    await scanLibrary();
    await loadPlaylists();
  } catch (error) {
    showError(error, "local");
    setEmpty(describeError(error, "local"));
  }
}
init();

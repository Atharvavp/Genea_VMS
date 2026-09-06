/* Genea VMS live view and recording — single page, vanilla JS, no build step.
 *
 * Four state models are kept apart on purpose:
 *
 *   camera.health.state    — ONLINE / OFFLINE / UNKNOWN. The backend's answer to
 *                            "can the VMS obtain this RTSP source?", polled from
 *                            MediaMTX. Same for everyone looking at the system.
 *   player state           — CONNECTING / LIVE / RECONNECTING / ERROR. This one
 *                            browser's WebRTC session for one tile. Lives only
 *                            here, and is never sent back to the server.
 *   camera.recording.state — DISABLED / WAITING / RECORDING / ERROR. Whether the
 *                            backend believes this camera is being written to
 *                            disk right now.
 *   history state          — LOADING / AVAILABLE / EMPTY / UNAVAILABLE. Whether
 *                            the recordings dialog could list past recordings.
 *
 * The last two are deliberately not the same thing. The playback server being
 * unreachable makes history UNAVAILABLE; it does not mean the camera stopped
 * recording, and it must never be shown as REC ERROR.
 *
 * Each tile owns one isolated MediaMTXWebRTCReader, so a camera failing, being
 * edited or being deleted cannot disturb the others. The recordings dialog uses
 * a plain <video> with native controls and a URL the API supplies.
 */

const POLL_INTERVAL_MS = 2000;

const PLAYER = {
  IDLE: "IDLE",
  CONNECTING: "CONNECTING",
  LIVE: "LIVE",
  RECONNECTING: "RECONNECTING",
  ERROR: "ERROR",
};

/* What the backend says about recording, mapped to what a tile shows. */
const RECORDING_LABEL = {
  DISABLED: "REC OFF",
  WAITING: "WAITING",
  RECORDING: "RECORDING",
  ERROR: "REC ERROR",
};

const state = {
  cameras: [],
  editingId: null,
  submitting: false,
  focusedId: null,
  // The recordings dialog. Its own slice of state, so the 2 s poll cannot
  // disturb a list the user is reading or a recording they are watching.
  recordings: { cameraId: null, date: null, selectedId: null, requestId: 0 },
};

/** cameraId -> Player (grid tiles). The focused view has its own, separate. */
const players = new Map();
/** cameraId -> tile element, so polling patches tiles instead of rebuilding. */
const tiles = new Map();
let focusPlayer = null;
let toastTimer = null;

const el = (id) => document.getElementById(id);

/* --- API helpers ------------------------------------------------------- */

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (response.status === 204) return null;
  const text = await response.text();
  let body = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch (err) {
      body = null;
    }
  }
  if (!response.ok) throw new Error(formatApiError(body, response.status));
  return body;
}

function formatApiError(body, status) {
  const error = body && body.error;
  if (!error) return `Request failed (HTTP ${status}).`;
  const fields = error.details && error.details.fields;
  if (Array.isArray(fields) && fields.length) {
    const detail = fields.map((f) => `${f.field}: ${f.message}`).join("\n");
    return `${error.message}\n${detail}`;
  }
  return error.message || `Request failed (HTTP ${status}).`;
}

/** Today as a UTC calendar day, matching how the backend reads `date`. */
function todayUtc() {
  return new Date().toISOString().slice(0, 10);
}

/** `2026-09-05T22:39:16.193314Z` -> `22:39:16`, in UTC. */
function formatUtcTime(value) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toISOString().slice(11, 19);
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return hours > 0
    ? `${hours}:${pad(minutes)}:${pad(secs)}`
    : `${minutes}:${pad(secs)}`;
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/* --- Player ------------------------------------------------------------ */

/**
 * One WebRTC/WHEP session bound to one <video>.
 *
 * The vendored reader retries on its own every couple of seconds, so a source
 * that comes back is picked up without recreating anything here — which is why
 * `onError` maps to RECONNECTING rather than tearing the player down.
 */
class Player {
  constructor(url, video, onChange) {
    this.url = url;
    this.video = video;
    this.onChange = onChange;
    this.state = PLAYER.CONNECTING;
    this.detail = null;
    this.reader = null;
    this.closed = false;

    this._onPlaying = () => this._set(PLAYER.LIVE, null);
    this.video.addEventListener("playing", this._onPlaying);

    try {
      this.reader = new window.MediaMTXWebRTCReader({
        url: this.url,
        onError: (err) => this._onError(String(err)),
        onTrack: (evt) => this._onTrack(evt),
      });
    } catch (err) {
      this._set(PLAYER.ERROR, String(err));
    }
  }

  _set(next, detail) {
    if (this.closed) return;
    if (this.state === next && this.detail === detail) return;
    this.state = next;
    this.detail = detail;
    if (this.onChange) this.onChange(this);
  }

  _onTrack(evt) {
    if (this.closed) return;
    const stream = evt.streams && evt.streams[0];
    if (stream && this.video.srcObject !== stream) {
      this.video.srcObject = stream;
    }
    // Autoplay needs the element muted; the markup sets it, belt and braces here.
    this.video.muted = true;
    const attempt = this.video.play();
    if (attempt && attempt.catch) attempt.catch(() => {});
    if (this.state !== PLAYER.LIVE) this._set(PLAYER.CONNECTING, null);
  }

  _onError(message) {
    // The reader appends "retrying in some seconds" to anything it will retry.
    const retrying = message.includes("retrying");
    this._set(retrying ? PLAYER.RECONNECTING : PLAYER.ERROR, message);
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    this.video.removeEventListener("playing", this._onPlaying);
    try {
      if (this.reader) this.reader.close();
    } catch (err) {
      /* a reader that never negotiated has nothing to close */
    }
    this.video.srcObject = null;
  }
}

function playerStateOf(cameraId) {
  const player = players.get(cameraId);
  return player ? player.state : PLAYER.IDLE;
}

function stopPlayer(cameraId) {
  const player = players.get(cameraId);
  if (player) {
    player.close();
    players.delete(cameraId);
  }
}

/**
 * Start, keep, or stop a tile's player to match the camera.
 *
 * Only `enabled` and `webrtc_url` can force a restart — a rename or a health
 * change leaves a running session completely alone.
 */
function syncTilePlayer(camera) {
  const tile = tiles.get(camera.id);
  if (!tile) return;
  const video = tile.querySelector("video");
  const existing = players.get(camera.id);

  const shouldPlay = camera.enabled && state.focusedId !== camera.id;
  if (!shouldPlay) {
    stopPlayer(camera.id);
    return;
  }
  if (existing && existing.url === camera.webrtc_url) return;

  stopPlayer(camera.id);
  players.set(
    camera.id,
    new Player(camera.webrtc_url, video, () => {
      const current = state.cameras.find((item) => item.id === camera.id);
      if (current) updateTile(tiles.get(camera.id), current);
    })
  );
}

/* --- Tiles ------------------------------------------------------------- */

function createTile(camera) {
  const tile = document.createElement("article");
  tile.className = "tile";
  tile.dataset.cameraId = camera.id;
  tile.innerHTML = `
    <div class="video-frame">
      <video muted autoplay playsinline></video>
      <div class="video-overlay" data-role="overlay"></div>
    </div>
    <div class="tile-body">
      <div class="tile-title">
        <h3 data-role="name"></h3>
        <div class="tile-badges">
          <span class="badge" data-role="health"></span>
          <span class="badge badge-player" data-role="player"></span>
          <span class="badge badge-recording" data-role="recording"></span>
        </div>
      </div>
      <div class="tile-url"><code data-role="url"></code></div>
      <div class="tile-error" data-role="error" hidden></div>
      <div class="tile-actions">
        <button class="btn" type="button" data-action="focus">Focus</button>
        <button class="btn" type="button" data-action="toggle"></button>
        <button class="btn" type="button" data-action="record"></button>
        <button class="btn" type="button" data-action="recordings">Recordings</button>
        <button class="btn" type="button" data-action="edit">Edit</button>
        <button class="btn" type="button" data-action="copy">Copy URL</button>
        <button class="btn btn-danger" type="button" data-action="delete">Delete</button>
      </div>
    </div>`;

  tile.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const current = state.cameras.find((item) => item.id === camera.id);
    if (!current) return;
    handleTileAction(button.dataset.action, current);
  });
  return tile;
}

function overlayText(camera, playerState) {
  if (!camera.enabled) return "Disabled";
  if (state.focusedId === camera.id) return "Playing in focused view";
  if (playerState === PLAYER.LIVE) return "";
  if (playerState === PLAYER.ERROR) return "Player error";
  if (playerState === PLAYER.RECONNECTING) {
    return camera.health.state === "OFFLINE"
      ? "Source offline — reconnecting"
      : "Reconnecting…";
  }
  return "Connecting…";
}

function updateTile(tile, camera) {
  if (!tile) return;
  const playerState = playerStateOf(camera.id);
  const health = camera.health || { state: "UNKNOWN" };

  tile.querySelector('[data-role="name"]').textContent = camera.name;

  const healthBadge = tile.querySelector('[data-role="health"]');
  healthBadge.textContent = camera.enabled ? health.state : "DISABLED";
  healthBadge.className = `badge ${
    camera.enabled ? health.state.toLowerCase() : "unknown"
  }`;
  healthBadge.title = camera.enabled
    ? "Source availability, as reported by MediaMTX"
    : "The VMS is not ingesting this camera";

  const playerBadge = tile.querySelector('[data-role="player"]');
  playerBadge.textContent = playerState;
  playerBadge.className = `badge badge-player ${playerState.toLowerCase()}`;
  playerBadge.title = "This browser's WebRTC session";

  // A third, separate badge: whether the backend believes this camera is being
  // written to disk. Never touched by anything the recordings dialog does.
  const recording = camera.recording || { state: "DISABLED" };
  const recordingBadge = tile.querySelector('[data-role="recording"]');
  recordingBadge.textContent = RECORDING_LABEL[recording.state] || recording.state;
  recordingBadge.className = `badge badge-recording ${recording.state.toLowerCase()}`;
  recordingBadge.title =
    recording.state === "RECORDING"
      ? "Recording is configured and MediaMTX has the source. Inferred: " +
        "MediaMTX does not report recorder-writer health."
      : recording.last_error || "Continuous recording to disk";

  tile.querySelector('[data-role="url"]').textContent = camera.rtsp_url_display;

  const error = tile.querySelector('[data-role="error"]');
  const message =
    camera.enabled && health.state !== "ONLINE" ? health.last_error : null;
  error.textContent = message || "";
  error.hidden = !message;

  const overlay = tile.querySelector('[data-role="overlay"]');
  const text = overlayText(camera, playerState);
  overlay.textContent = text;
  overlay.hidden = text === "";

  const toggle = tile.querySelector('[data-action="toggle"]');
  toggle.textContent = camera.enabled ? "Disable" : "Enable";
  tile.querySelector('[data-action="focus"]').disabled = !camera.enabled;

  const record = tile.querySelector('[data-action="record"]');
  record.textContent = camera.recording_enabled ? "Recording: ON" : "Recording: OFF";
  record.classList.toggle("btn-active", Boolean(camera.recording_enabled));

  // Disabled cameras have no MediaMTX path, and the playback server only
  // answers for a configured path, so their history cannot be listed.
  const recordings = tile.querySelector('[data-action="recordings"]');
  recordings.disabled = !camera.enabled;
  recordings.title = camera.enabled
    ? "Browse recordings by date"
    : "Recordings are on disk, but history can only be browsed while the " +
      "camera is enabled";
}

function renderGrid(cameras) {
  const grid = el("camera-grid");
  const seen = new Set();

  cameras.forEach((camera) => {
    seen.add(camera.id);
    let tile = tiles.get(camera.id);
    if (!tile) {
      tile = createTile(camera);
      tiles.set(camera.id, tile);
      grid.appendChild(tile);
    }
    syncTilePlayer(camera);
    updateTile(tile, camera);
  });

  tiles.forEach((tile, cameraId) => {
    if (seen.has(cameraId)) return;
    stopPlayer(cameraId);
    tile.remove();
    tiles.delete(cameraId);
    if (state.focusedId === cameraId) closeFocus();
  });

  // Keep DOM order matching API order without touching the existing nodes'
  // media state (appendChild of an attached node moves it, it does not reload).
  cameras.forEach((camera) => grid.appendChild(tiles.get(camera.id)));

  el("empty-state").hidden = cameras.length > 0;
}

/* --- Tile actions ------------------------------------------------------ */

async function handleTileAction(action, camera) {
  try {
    if (action === "focus") return openFocus(camera);
    if (action === "copy") return copyUrl(camera);
    if (action === "edit") return openEditor(camera);
    if (action === "toggle") {
      const path = camera.enabled ? "disable" : "enable";
      await api(`/api/cameras/${camera.id}/${path}`, { method: "POST" });
      return refresh();
    }
    if (action === "recordings") return openRecordings(camera);
    if (action === "record") {
      // The same partial update the edit form uses; live is untouched.
      await api(`/api/cameras/${camera.id}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ recording_enabled: !camera.recording_enabled }),
      });
      return refresh();
    }
    if (action === "delete") {
      if (!window.confirm(`Delete "${camera.name}"?`)) return;
      await api(`/api/cameras/${camera.id}`, { method: "DELETE" });
      return refresh();
    }
  } catch (err) {
    showBanner(err.message);
  }
}

async function copyUrl(camera) {
  const note = camera.has_credentials
    ? "Source URL copied (password masked)"
    : "Source URL copied";
  try {
    await navigator.clipboard.writeText(camera.rtsp_url_display);
    showToast(note);
  } catch (err) {
    showToast("Copy failed — select the URL manually");
  }
}

/* --- Focused view ------------------------------------------------------ */

function openFocus(camera) {
  state.focusedId = camera.id;
  // Hand the session over rather than running two for one camera.
  stopPlayer(camera.id);

  el("focus-title").textContent = camera.name;
  el("focus-subtitle").textContent = camera.rtsp_url_display;
  el("focus-url").textContent = camera.webrtc_url;
  setModalOpen("focus-modal", true);

  const video = el("focus-video");
  focusPlayer = new Player(camera.webrtc_url, video, updateFocus);
  updateFocus();
  renderGrid(state.cameras);
}

function updateFocus() {
  if (!state.focusedId) return;
  const camera = state.cameras.find((item) => item.id === state.focusedId);
  if (!camera) return closeFocus();

  const playerState = focusPlayer ? focusPlayer.state : PLAYER.IDLE;
  const healthBadge = el("focus-health");
  healthBadge.textContent = camera.health.state;
  healthBadge.className = `badge ${camera.health.state.toLowerCase()}`;

  const playerBadge = el("focus-player");
  playerBadge.textContent = playerState;
  playerBadge.className = `badge badge-player ${playerState.toLowerCase()}`;

  const recording = camera.recording || { state: "DISABLED" };
  const recordingBadge = el("focus-recording");
  recordingBadge.textContent = RECORDING_LABEL[recording.state] || recording.state;
  recordingBadge.className = `badge badge-recording ${recording.state.toLowerCase()}`;

  el("focus-title").textContent = camera.name;
  const overlay = el("focus-overlay");
  const text = playerState === PLAYER.LIVE ? "" : overlayText(camera, playerState);
  overlay.textContent = text;
  overlay.hidden = text === "";
}

function closeFocus() {
  const cameraId = state.focusedId;
  state.focusedId = null;
  if (focusPlayer) {
    focusPlayer.close();
    focusPlayer = null;
  }
  setModalOpen("focus-modal", false);
  if (cameraId) {
    // The tile takes its player back.
    const camera = state.cameras.find((item) => item.id === cameraId);
    if (camera) {
      syncTilePlayer(camera);
      updateTile(tiles.get(camera.id), camera);
    }
  }
}

/* --- Recordings --------------------------------------------------------- */

/**
 * Show exactly one of the dialog's history states.
 *
 * LOADING / AVAILABLE / EMPTY / UNAVAILABLE describe *this dialog's* attempt to
 * read history. They are not the camera's recording state and never change the
 * tile badges: a camera can be RECORDING while history is UNAVAILABLE.
 */
function setHistoryState(historyState, message) {
  el("recordings-loading").hidden = historyState !== "LOADING";
  el("recordings-empty").hidden = historyState !== "EMPTY";
  el("recordings-list").hidden = historyState !== "AVAILABLE";

  const error = el("recordings-error");
  error.textContent = historyState === "UNAVAILABLE" ? message || "" : "";
  error.hidden = historyState !== "UNAVAILABLE";

  if (historyState !== "AVAILABLE") clearRecordingPlayer();
}

function clearRecordingPlayer() {
  const video = el("recordings-video");
  video.pause();
  video.removeAttribute("src");
  video.load();
  el("recordings-player").hidden = true;
  state.recordings.selectedId = null;
}

function openRecordings(camera) {
  state.recordings.cameraId = camera.id;
  state.recordings.date = todayUtc();
  state.recordings.selectedId = null;

  el("recordings-title").textContent = camera.name;
  el("recordings-subtitle").textContent =
    "Finalised recordings, by UTC day. Times are UTC.";
  el("recordings-date").value = state.recordings.date;
  clearRecordingPlayer();
  setModalOpen("recordings-modal", true);
  loadRecordings();
}

function closeRecordings() {
  clearRecordingPlayer();
  state.recordings.cameraId = null;
  // Anything still in flight belongs to a closed dialog now.
  state.recordings.requestId += 1;
  setModalOpen("recordings-modal", false);
}

async function loadRecordings() {
  const { cameraId, date } = state.recordings;
  if (!cameraId || !date) return;

  // Only the newest request may paint: a quick date change must not be
  // overwritten by a slower earlier response.
  const requestId = ++state.recordings.requestId;
  setHistoryState("LOADING");

  try {
    const body = await api(
      `/api/recordings?camera_id=${encodeURIComponent(cameraId)}` +
        `&date=${encodeURIComponent(date)}`
    );
    if (requestId !== state.recordings.requestId) return;
    if (!body.items.length) return setHistoryState("EMPTY");
    renderRecordings(body.items);
    setHistoryState("AVAILABLE");
  } catch (err) {
    if (requestId !== state.recordings.requestId) return;
    // A history failure stays in this dialog. The tile badges are untouched.
    setHistoryState("UNAVAILABLE", err.message);
  }
}

function renderRecordings(items) {
  const list = el("recordings-list");
  list.innerHTML = items
    .map(
      (item) => `
      <button class="recording-row" type="button" data-recording-id="${escapeHtml(
        item.id
      )}">
        <span class="recording-time">${escapeHtml(
          formatUtcTime(item.start_time)
        )} → ${escapeHtml(formatUtcTime(item.end_time))}</span>
        <span class="recording-duration">${escapeHtml(
          formatDuration(item.duration_seconds)
        )}</span>
      </button>`
    )
    .join("");

  list.querySelectorAll("[data-recording-id]").forEach((row) => {
    const item = items.find((entry) => entry.id === row.dataset.recordingId);
    row.addEventListener("click", () => playRecording(item, list));
  });
}

function playRecording(item, list) {
  if (!item) return;
  state.recordings.selectedId = item.id;
  list.querySelectorAll("[data-recording-id]").forEach((row) => {
    row.classList.toggle("selected", row.dataset.recordingId === item.id);
  });

  const video = el("recordings-video");
  // The URL comes from the API, which built it from VMS settings. Nothing here
  // knows the playback host, the MediaMTX path format or the query shape.
  video.src = item.playback_url;
  el("recordings-player").hidden = false;
  video.load();
  const attempt = video.play();
  if (attempt && attempt.catch) attempt.catch(() => {});
}

/* --- Add / edit dialog -------------------------------------------------- */

function isModalOpen(id) {
  return !el(id).hidden;
}

function setModalOpen(id, open) {
  const modal = el(id);
  modal.hidden = !open;
  modal.setAttribute("aria-hidden", String(!open));
  const anyOpen =
    !el("modal").hidden ||
    !el("focus-modal").hidden ||
    !el("recordings-modal").hidden;
  document.body.classList.toggle("modal-open", anyOpen);
}

function openCreator() {
  state.editingId = null;
  el("modal-title").textContent = "Add Camera";
  el("form-error").hidden = true;
  el("camera-form").reset();
  el("enabled-input").checked = true;
  el("recording-enabled-input").checked = false;  // never on by default
  el("rtsp-url-input").required = true;
  el("keep-source-hint").hidden = true;
  setModalOpen("modal", true);
}

function openEditor(camera) {
  state.editingId = camera.id;
  el("modal-title").textContent = "Edit Camera";
  el("form-error").hidden = true;
  el("camera-form").reset();
  el("camera-form").elements.name.value = camera.name;
  el("enabled-input").checked = camera.enabled;
  el("recording-enabled-input").checked = Boolean(camera.recording_enabled);

  // A URL with credentials is never sent back to the browser, so the field
  // starts empty and an empty field means "keep what is stored".
  const urlInput = el("rtsp-url-input");
  urlInput.value = camera.has_credentials ? "" : camera.rtsp_url_display;
  urlInput.required = false;
  el("keep-source-hint").hidden = !camera.has_credentials;

  setModalOpen("modal", true);
}

function closeEditor() {
  setModalOpen("modal", false);
  state.editingId = null;
}

async function submitForm(event) {
  event.preventDefault();
  if (state.submitting) return;

  const form = el("camera-form");
  const name = form.elements.name.value.trim();
  const url = el("rtsp-url-input").value.trim();
  const enabled = el("enabled-input").checked;
  const recordingEnabled = el("recording-enabled-input").checked;

  const payload = { name, enabled, recording_enabled: recordingEnabled };
  // Omitted, not empty: the API rejects a blank URL rather than guessing.
  if (url) payload.rtsp_url = url;

  state.submitting = true;
  el("modal-submit").disabled = true;
  try {
    if (state.editingId) {
      await api(`/api/cameras/${state.editingId}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      });
    } else {
      await api("/api/cameras", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      });
    }
    closeEditor();
    await refresh();
  } catch (err) {
    const box = el("form-error");
    box.textContent = err.message;
    box.hidden = false;
  } finally {
    state.submitting = false;
    el("modal-submit").disabled = false;
  }
}

/* --- Chrome ------------------------------------------------------------ */

function showBanner(message) {
  const banner = el("banner");
  banner.textContent = message;
  banner.hidden = !message;
}

function showToast(message) {
  const toast = el("toast");
  toast.textContent = message;
  toast.hidden = false;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.hidden = true;
  }, 2200);
}

function renderHealth(report) {
  const badge = el("health-badge");
  if (!report) {
    badge.textContent = "backend unreachable";
    badge.className = "health bad";
    return;
  }
  const mediamtx = report.mediamtx || {};
  const cameras = report.cameras || {};
  badge.textContent = mediamtx.reachable
    ? `MediaMTX ${mediamtx.version || "ok"} · ${cameras.online || 0}/${
        cameras.enabled || 0
      } online`
    : "MediaMTX unreachable";
  badge.className = `health ${mediamtx.reachable ? "ok" : "bad"}`;
}

/* --- Polling ----------------------------------------------------------- */

async function refresh() {
  const cameras = await api("/api/cameras");
  state.cameras = cameras;
  renderGrid(cameras);
  updateFocus();
}

async function tick() {
  try {
    await refresh();
    showBanner("");
  } catch (err) {
    showBanner(`Cannot reach the VMS API: ${err.message}`);
  }
  try {
    renderHealth(await api("/health"));
  } catch (err) {
    // /health answers 503 while MediaMTX is down; that is information, not a
    // failure of the page.
    try {
      const response = await fetch("/health");
      renderHealth(await response.json());
    } catch (inner) {
      renderHealth(null);
    }
  }
}

/* --- Init -------------------------------------------------------------- */

function init() {
  // Deterministic starting state, whatever the browser restored.
  setModalOpen("modal", false);
  setModalOpen("focus-modal", false);
  setModalOpen("recordings-modal", false);
  el("banner").hidden = true;
  el("toast").hidden = true;
  setHistoryState(null);

  el("add-camera").addEventListener("click", openCreator);
  el("modal-close").addEventListener("click", closeEditor);
  el("modal-cancel").addEventListener("click", closeEditor);
  el("camera-form").addEventListener("submit", submitForm);
  el("focus-close").addEventListener("click", closeFocus);
  el("recordings-close").addEventListener("click", closeRecordings);
  el("recordings-date").addEventListener("change", (event) => {
    state.recordings.date = event.target.value;
    loadRecordings();
  });

  el("modal").addEventListener("click", (event) => {
    if (event.target === el("modal")) closeEditor();
  });
  el("focus-modal").addEventListener("click", (event) => {
    if (event.target === el("focus-modal")) closeFocus();
  });
  el("recordings-modal").addEventListener("click", (event) => {
    if (event.target === el("recordings-modal")) closeRecordings();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (isModalOpen("modal")) closeEditor();
    else if (isModalOpen("recordings-modal")) closeRecordings();
    else if (isModalOpen("focus-modal")) closeFocus();
  });

  tick();
  setInterval(tick, POLL_INTERVAL_MS);
}

document.addEventListener("DOMContentLoaded", init);

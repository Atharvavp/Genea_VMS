/* RTSP Camera Simulator - single page UI (no build step, no framework). */

const POLL_INTERVAL_MS = 2000;

const state = {
  cameras: [],
  editingId: null,
  selectedFile: null,
  submitting: false,
  localSources: { directory: "/data/local-sources", files: [] },
};

const el = (id) => document.getElementById(id);
const form = el("camera-form");

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
  if (!response.ok) {
    throw new Error(formatApiError(body, response.status));
  }
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

/* --- Rendering --------------------------------------------------------- */

function humanDuration(seconds) {
  if (seconds == null) return "—";
  const total = Math.round(seconds);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m > 0 ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}

function humanSize(bytes) {
  if (bytes == null) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function describeResolution(resolution) {
  return resolution.mode === "fixed"
    ? `${resolution.width}×${resolution.height}`
    : "same as source";
}

function describeFps(fps) {
  return fps.mode === "fixed" ? `${fps.value} fps` : "same as source";
}

function describeBitrate(bitrate) {
  return bitrate.mode === "fixed" ? `${bitrate.kbps} kbps` : "auto (CRF 23)";
}

function definitionList(rows) {
  return rows
    .map(
      ([term, value]) =>
        `<dt>${escapeHtml(term)}</dt><dd>${escapeHtml(String(value))}</dd>`
    )
    .join("");
}

function escapeHtml(value) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderCard(camera) {
  const source = camera.source || {};
  const busy = camera.status === "STARTING" || camera.status === "STOPPING";
  const running = camera.status === "RUNNING";

  const sourceRows = [
    ["File", source.filename || "—"],
    ["Kind", camera.source_kind === "local" ? "mounted file" : "uploaded"],
    ["Codec", source.video_codec || "—"],
    [
      "Resolution",
      source.width && source.height ? `${source.width}×${source.height}` : "—",
    ],
    ["Frame rate", source.fps != null ? `${source.fps} fps` : "—"],
    ["Duration", humanDuration(source.duration_seconds)],
    ["Bitrate", source.bitrate_kbps != null ? `${source.bitrate_kbps} kbps` : "—"],
    ["Size", humanSize(source.size_bytes)],
    ["Audio", source.has_audio ? "yes (not published)" : "no"],
  ];

  const outputRows = [
    ["Codec", camera.video.codec === "h265" ? "H.265" : "H.264"],
    ["Resolution", describeResolution(camera.video.resolution)],
    ["Frame rate", describeFps(camera.video.fps)],
    ["Bitrate", describeBitrate(camera.video.bitrate)],
    ["Loop", camera.loop ? "yes" : "no (stops at end)"],
    ["Auto start", camera.auto_start ? "yes" : "no"],
    ["Audio", "stripped"],
    ["PID", camera.runtime && camera.runtime.pid ? camera.runtime.pid : "—"],
  ];

  const card = document.createElement("article");
  card.className = "card";
  card.innerHTML = `
    <div class="card-head">
      <div>
        <h3 class="card-title">${escapeHtml(camera.name)}</h3>
        <p class="card-sub">/${escapeHtml(camera.stream_path)}</p>
      </div>
      <span class="badge ${camera.status}">${camera.status}</span>
    </div>
    <div class="rtsp">
      <code>${escapeHtml(camera.rtsp_url)}</code>
      <button class="btn" data-action="copy">Copy</button>
    </div>
    <div class="sections">
      <div class="section">
        <h4>Source video (input file)</h4>
        <dl>${definitionList(sourceRows)}</dl>
      </div>
      <div class="section">
        <h4>Virtual camera output (RTSP)</h4>
        <dl>${definitionList(outputRows)}</dl>
      </div>
    </div>
    ${camera.last_error ? `<div class="error-box">${escapeHtml(camera.last_error)}</div>` : ""}
    <div class="card-actions">
      <button class="btn" data-action="start" ${busy || running ? "disabled" : ""}>Start</button>
      <button class="btn" data-action="stop" ${busy || !running ? "disabled" : ""}>Stop</button>
      <button class="btn" data-action="restart" ${busy ? "disabled" : ""}>Restart</button>
      <button class="btn" data-action="edit" ${busy ? "disabled" : ""}>Edit</button>
      <button class="btn btn-danger" data-action="delete" ${busy ? "disabled" : ""}>Delete</button>
    </div>
  `;

  card.querySelectorAll("button[data-action]").forEach((button) => {
    button.addEventListener("click", () => handleAction(button.dataset.action, camera, button));
  });
  return card;
}

function render() {
  const list = el("camera-list");
  list.innerHTML = "";
  state.cameras.forEach((camera) => list.appendChild(renderCard(camera)));
  el("empty-state").hidden = state.cameras.length > 0;
}

/* --- Actions ----------------------------------------------------------- */

async function handleAction(action, camera, button) {
  if (action === "copy") {
    copyToClipboard(camera.rtsp_url);
    return;
  }
  if (action === "edit") {
    openModal(camera);
    return;
  }
  if (action === "delete") {
    if (!window.confirm(`Delete camera "${camera.name}"? This stops its stream.`)) return;
    await runAction(button, () => api(`/api/cameras/${camera.id}`, { method: "DELETE" }));
    return;
  }
  await runAction(button, () =>
    api(`/api/cameras/${camera.id}/${action}`, { method: "POST" })
  );
}

async function runAction(button, work) {
  button.disabled = true;
  hideBanner();
  try {
    await work();
  } catch (error) {
    showBanner(error.message);
  } finally {
    await refresh();
  }
}

function copyToClipboard(text) {
  const done = () => toast("RTSP URL copied");
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}

function fallbackCopy(text, done) {
  const area = document.createElement("textarea");
  area.value = text;
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try {
    document.execCommand("copy");
    done();
  } catch (err) {
    showBanner("Could not copy automatically. The URL is shown on the card.");
  }
  document.body.removeChild(area);
}

function toast(message) {
  const node = document.createElement("div");
  node.className = "toast";
  node.textContent = message;
  document.body.appendChild(node);
  setTimeout(() => node.remove(), 1800);
}

function showBanner(message) {
  const banner = el("banner");
  banner.textContent = message;
  banner.hidden = false;
}

function hideBanner() {
  el("banner").hidden = true;
}

/* --- Modal ------------------------------------------------------------- */

/* The `hidden` attribute on #modal is the single source of truth for whether
   the dialog is open; nothing else tracks that state. */
function isModalOpen() {
  return !el("modal").hidden;
}

function setModalOpen(open) {
  const modal = el("modal");
  modal.hidden = !open;
  modal.setAttribute("aria-hidden", String(!open));
  document.body.classList.toggle("modal-open", open);
}

function openModal(camera) {
  state.editingId = camera ? camera.id : null;
  state.selectedFile = null;
  form.reset();
  el("form-error").hidden = true;
  el("file-name").textContent = "No file selected";
  el("modal-title").textContent = camera ? "Edit Camera" : "Add Camera";

  const keepOption = el("keep-source-option");
  keepOption.hidden = !camera;
  el("keep-source-info").hidden = !camera;

  if (camera) {
    form.name.value = camera.name;
    form.stream_path.value = camera.stream_path;
    form.codec.value = camera.video.codec;
    form.resolution_mode.value = camera.video.resolution.mode;
    if (camera.video.resolution.mode === "fixed") {
      form.width.value = camera.video.resolution.width;
      form.height.value = camera.video.resolution.height;
    }
    form.fps_mode.value = camera.video.fps.mode;
    if (camera.video.fps.mode === "fixed") form.fps.value = camera.video.fps.value;
    form.bitrate_mode.value = camera.video.bitrate.mode;
    if (camera.video.bitrate.mode === "fixed") form.bitrate.value = camera.video.bitrate.kbps;
    form.loop.checked = camera.loop;
    form.auto_start.checked = camera.auto_start;
    setSourceKind("keep");
    el("keep-source-info").textContent =
      `Currently using ${camera.source.filename} (` +
      `${camera.source_kind === "local" ? "mounted file" : "uploaded file"}).`;
  } else {
    setSourceKind("upload");
  }

  syncConditionalFields();
  updateUrlPreview();
  setModalOpen(true);
  form.name.focus();
}

function closeModal() {
  setModalOpen(false);
  state.editingId = null;
  state.selectedFile = null;
}

function currentSourceKind() {
  const checked = form.querySelector('input[name="source_kind"]:checked');
  return checked ? checked.value : "upload";
}

function setSourceKind(kind) {
  const input = form.querySelector(`input[name="source_kind"][value="${kind}"]`);
  if (input) input.checked = true;
  syncSourcePanels();
}

function syncSourcePanels() {
  const kind = currentSourceKind();
  el("source-upload").hidden = kind !== "upload";
  el("source-local").hidden = kind !== "local";
}

function syncConditionalFields() {
  form.querySelectorAll(".resolution-fixed").forEach((node) => {
    node.hidden = form.resolution_mode.value !== "fixed";
  });
  form.querySelectorAll(".fps-fixed").forEach((node) => {
    node.hidden = form.fps_mode.value !== "fixed";
  });
  form.querySelectorAll(".bitrate-fixed").forEach((node) => {
    node.hidden = form.bitrate_mode.value !== "fixed";
  });
  syncSourcePanels();
}

function updateUrlPreview() {
  const path =
    form.stream_path.value.trim() || slugify(form.name.value) || "<stream-path>";
  const base = window.location.hostname || "localhost";
  el("url-preview").textContent = `rtsp://${base}:8554/simulator/${path}`;
}

function slugify(value) {
  return value
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

function buildPayload() {
  const payload = {
    name: form.name.value.trim(),
    loop: form.loop.checked,
    auto_start: form.auto_start.checked,
    video: {
      codec: form.codec.value,
      resolution:
        form.resolution_mode.value === "fixed"
          ? {
              mode: "fixed",
              width: Number(form.width.value),
              height: Number(form.height.value),
            }
          : { mode: "source" },
      fps:
        form.fps_mode.value === "fixed"
          ? { mode: "fixed", value: Number(form.fps.value) }
          : { mode: "source" },
      bitrate:
        form.bitrate_mode.value === "fixed"
          ? { mode: "fixed", kbps: Number(form.bitrate.value) }
          : { mode: "auto" },
    },
  };

  const streamPath = form.stream_path.value.trim();
  if (streamPath) payload.stream_path = streamPath;

  const kind = currentSourceKind();
  if (kind === "local") {
    const typed = form.local_path_text.value.trim();
    const picked = el("local-select").value;
    payload.source = { kind: "local", local_path: typed || picked };
  } else if (kind === "upload") {
    payload.source = { kind: "upload" };
  }
  return payload;
}

async function submitForm(event) {
  event.preventDefault();
  if (state.submitting) return;

  const errorBox = el("form-error");
  errorBox.hidden = true;

  const kind = currentSourceKind();
  const payload = buildPayload();

  if (kind === "upload" && !state.selectedFile) {
    errorBox.textContent = "Choose a video file to upload.";
    errorBox.hidden = false;
    return;
  }
  if (kind === "local" && !payload.source.local_path) {
    errorBox.textContent = "Choose or type a file from the mounted source directory.";
    errorBox.hidden = false;
    return;
  }

  const body = new FormData();
  body.append("payload", JSON.stringify(payload));
  if (kind === "upload" && state.selectedFile) body.append("file", state.selectedFile);

  state.submitting = true;
  el("submit").disabled = true;
  el("submit").textContent = "Saving…";
  try {
    if (state.editingId) {
      await api(`/api/cameras/${state.editingId}`, { method: "PATCH", body });
    } else {
      await api("/api/cameras", { method: "POST", body });
    }
    closeModal();
    hideBanner();
    await refresh();
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally {
    state.submitting = false;
    el("submit").disabled = false;
    el("submit").textContent = "Save camera";
  }
}

function selectFile(file) {
  if (!file) return;
  state.selectedFile = file;
  el("file-name").textContent = `${file.name} (${humanSize(file.size)})`;
  setSourceKind("upload");
}

/* --- Polling ----------------------------------------------------------- */

async function refresh() {
  try {
    const cameras = await api("/api/cameras");
    state.cameras = cameras || [];
    render();
    hideBanner();
  } catch (error) {
    showBanner(`Could not reach the simulator API: ${error.message}`);
  }
}

async function refreshHealth() {
  const badge = el("health-badge");
  try {
    const response = await fetch("/health");
    const report = await response.json();
    const ok = response.ok && report.status === "ok";
    badge.textContent = ok
      ? `healthy · ${report.running_publishers} publishing`
      : "degraded";
    badge.className = `health ${ok ? "ok" : "bad"}`;
    badge.title = `ffmpeg: ${report.ffmpeg} · ffprobe: ${report.ffprobe} · database: ${report.database}`;
  } catch (error) {
    badge.textContent = "unreachable";
    badge.className = "health bad";
  }
}

async function loadLocalSources() {
  try {
    const data = await api("/api/local-sources");
    state.localSources = data;
    el("local-dir").textContent = data.directory;
    const select = el("local-select");
    select.innerHTML = "";
    if (!data.files.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "(no files in the mounted directory)";
      select.appendChild(option);
      return;
    }
    data.files.forEach((file) => {
      const option = document.createElement("option");
      option.value = file;
      option.textContent = file;
      select.appendChild(option);
    });
  } catch (error) {
    /* The mounted directory is optional; ignore failures. */
  }
}

function tick() {
  if (!state.submitting && !isModalOpen()) {
    refresh();
    refreshHealth();
  }
}

/* --- Wiring ------------------------------------------------------------ */

function init() {
  // Deterministic starting state, whatever the browser restored or cached.
  setModalOpen(false);
  syncConditionalFields();

  el("add-camera").addEventListener("click", () => {
    loadLocalSources();
    openModal(null);
  });
  el("modal-close").addEventListener("click", closeModal);
  el("cancel").addEventListener("click", closeModal);
  el("modal").addEventListener("click", (event) => {
    if (event.target === el("modal")) closeModal();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && isModalOpen()) closeModal();
  });

  form.addEventListener("submit", submitForm);
  form.addEventListener("change", syncConditionalFields);
  form.name.addEventListener("input", updateUrlPreview);
  form.stream_path.addEventListener("input", updateUrlPreview);

  el("browse").addEventListener("click", () => el("file-input").click());
  el("file-input").addEventListener("change", (event) => selectFile(event.target.files[0]));

  const dropzone = el("dropzone");
  ["dragenter", "dragover"].forEach((name) =>
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.add("dragover");
    })
  );
  ["dragleave", "drop"].forEach((name) =>
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.remove("dragover");
    })
  );
  dropzone.addEventListener("drop", (event) => {
    const file = event.dataTransfer.files && event.dataTransfer.files[0];
    selectFile(file);
  });

  refresh();
  refreshHealth();
  loadLocalSources();
  setInterval(tick, POLL_INTERVAL_MS);
}

document.addEventListener("DOMContentLoaded", init);

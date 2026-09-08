/*
 * Component 4 dashboard: hash router, keyed fetch with stale-response
 * protection, visibility-aware polling, canvas line editor, and event browser.
 *
 * Every piece of server text reaches the DOM through textContent. Nothing here
 * builds a playback URL, and nothing here talks to the VMS directly.
 */

const state = {
  route: "cameras",
  camerasById: new Map(),
  selectedCameraId: null,
  selectedEventId: null,
  eventFilters: {},
  eventCursor: null,
  eventIds: new Set(),
  requestGenerationByKey: new Map(),
  abortControllerByKey: new Map(),
  pollTimers: new Map(),
  lineDraft: null,
  lineSaved: null,
  lineDirty: false,
  dragging: null,
  snapshotObjectUrl: null,
  consecutiveNetworkErrors: 0,
  lastUpdatedAt: null,
  editingCameraId: null,
  confirmResolver: null,
};

const CAMERA_LIST_KEY = "camera-list";
const CONFIGURE_KEY = "configure";
const SNAPSHOT_KEY = "snapshot";
const EVENTS_KEY = "events";
const DETAIL_KEY = "event-detail";
const RECORDING_KEY = "recording";
const MIN_LINE_LENGTH = 0.05;

/* ------------------------------------------------------------------ *
 * DOM helpers
 * ------------------------------------------------------------------ */

const $ = (id) => document.getElementById(id);

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function el(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  if (options.text !== undefined) node.textContent = String(options.text);
  if (options.className) node.className = options.className;
  if (options.attrs) {
    for (const [key, value] of Object.entries(options.attrs)) {
      if (value !== null && value !== undefined) node.setAttribute(key, String(value));
    }
  }
  for (const child of children) node.appendChild(child);
  return node;
}

function show(node, visible) {
  node.hidden = !visible;
}

function setText(node, value) {
  node.textContent = value === null || value === undefined ? "" : String(value);
}

/* ------------------------------------------------------------------ *
 * Fetch with per-key generation and abort
 * ------------------------------------------------------------------ */

class StaleResponse extends Error {}

async function apiFetch(key, path, options = {}) {
  const previous = state.abortControllerByKey.get(key);
  if (previous) previous.abort();
  const controller = new AbortController();
  state.abortControllerByKey.set(key, controller);

  const generation = (state.requestGenerationByKey.get(key) || 0) + 1;
  state.requestGenerationByKey.set(key, generation);

  let response;
  try {
    response = await fetch(path, { ...options, signal: controller.signal });
  } catch (error) {
    if (controller.signal.aborted) throw new StaleResponse("aborted");
    state.consecutiveNetworkErrors += 1;
    showConnectionBanner("Cannot reach the analytics service. Retrying...");
    throw error;
  }

  if (state.requestGenerationByKey.get(key) !== generation) {
    throw new StaleResponse("superseded");
  }
  state.consecutiveNetworkErrors = 0;
  hideConnectionBanner();
  return response;
}

async function readJson(response) {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function errorMessage(body, fallback) {
  if (body && body.error && body.error.message) {
    const requestId = body.error.request_id ? ` (${body.error.request_id})` : "";
    return `${body.error.message}${requestId}`;
  }
  return fallback;
}

function fieldErrors(body) {
  const fields =
    body && body.error && body.error.details && body.error.details.fields;
  if (!Array.isArray(fields) || fields.length === 0) return "";
  return fields.map((item) => `${item.field}: ${item.reason}`).join("; ");
}

function showConnectionBanner(message) {
  if (state.consecutiveNetworkErrors < 1) return;
  setText($("connection-banner-text"), message);
  show($("connection-banner"), true);
}

function hideConnectionBanner() {
  show($("connection-banner"), false);
}

/* ------------------------------------------------------------------ *
 * Timers
 * ------------------------------------------------------------------ */

function startPoll(key, callback, intervalMs) {
  stopPoll(key);
  const tick = async () => {
    if (!document.hidden) {
      try {
        await callback();
      } catch (error) {
        if (!(error instanceof StaleResponse)) {
          /* the banner already reported it */
        }
      }
    }
    if (state.pollTimers.has(key)) {
      state.pollTimers.set(key, window.setTimeout(tick, intervalMs));
    }
  };
  state.pollTimers.set(key, window.setTimeout(tick, intervalMs));
  callback().catch(() => {});
}

function stopPoll(key) {
  const handle = state.pollTimers.get(key);
  if (handle) window.clearTimeout(handle);
  state.pollTimers.delete(key);
}

function stopAllViewWork() {
  for (const key of Array.from(state.pollTimers.keys())) stopPoll(key);
  for (const controller of state.abortControllerByKey.values()) controller.abort();
  state.abortControllerByKey.clear();
  releaseSnapshotUrl();
}

function releaseSnapshotUrl() {
  if (state.snapshotObjectUrl) {
    URL.revokeObjectURL(state.snapshotObjectUrl);
    state.snapshotObjectUrl = null;
  }
}

function ageText(isoString) {
  if (!isoString) return "never";
  const seconds = Math.max(0, (Date.now() - Date.parse(isoString)) / 1000);
  if (seconds < 2) return "just now";
  if (seconds < 90) return `${Math.round(seconds)}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}

/* ------------------------------------------------------------------ *
 * Router
 * ------------------------------------------------------------------ */

function parseRoute() {
  const hash = window.location.hash.replace(/^#/, "") || "/cameras";
  const parts = hash.split("/").filter(Boolean);
  if (parts[0] === "events") return { route: "events" };
  if (parts[0] === "cameras" && parts[2] === "configure") {
    return { route: "configure", cameraId: parts[1] };
  }
  return { route: "cameras" };
}

function applyRoute() {
  const target = parseRoute();
  stopAllViewWork();
  state.route = target.route;
  state.selectedCameraId = target.cameraId || null;

  show($("view-cameras"), target.route === "cameras");
  show($("view-configure"), target.route === "configure");
  show($("view-events"), target.route === "events");

  for (const link of document.querySelectorAll("[data-nav]")) {
    const active =
      link.dataset.nav === target.route ||
      (link.dataset.nav === "cameras" && target.route === "configure");
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }

  if (target.route === "cameras") {
    startPoll(CAMERA_LIST_KEY, loadCameras, 2000);
  } else if (target.route === "configure") {
    state.lineDraft = null;
    state.lineSaved = null;
    state.lineDirty = false;
    startPoll(CONFIGURE_KEY, loadConfigure, 2000);
    startPoll(SNAPSHOT_KEY, loadSnapshot, 1000);
  } else {
    loadCameraOptions();
    loadEvents({ reset: true }).catch(() => {});
  }
}

/* ------------------------------------------------------------------ *
 * Cameras view
 * ------------------------------------------------------------------ */

async function loadCameras() {
  const response = await apiFetch(CAMERA_LIST_KEY, "/api/cameras");
  const body = await readJson(response);
  if (state.route !== "cameras" || !Array.isArray(body)) return;
  state.camerasById = new Map(body.map((camera) => [camera.id, camera]));
  state.lastUpdatedAt = new Date().toISOString();
  renderCameras(body);
}

function renderCameras(cameras) {
  const list = $("camera-list");
  clear(list);
  show($("cameras-empty"), cameras.length === 0);
  setText($("cameras-updated"), `Updated ${ageText(state.lastUpdatedAt)}`);
  setText(
    $("header-status"),
    `${cameras.length} camera${cameras.length === 1 ? "" : "s"}`,
  );

  for (const camera of cameras) {
    list.appendChild(renderCameraCard(camera));
  }
}

function renderCameraCard(camera) {
  const runtime = camera.runtime || {};
  const card = el("article", { className: "camera-card" });

  card.appendChild(el("h3", { text: camera.name }));

  const badges = el("div", { className: "badge-row" });
  const badge = el("span", {
    className: "badge",
    text: runtime.stopping ? `${runtime.state} (stopping)` : runtime.state,
    attrs: { "data-state": runtime.state, "data-testid": `state-${camera.id}` },
  });
  badges.appendChild(badge);
  badges.appendChild(
    el("span", {
      className: "badge",
      text: camera.enabled ? "desired: enabled" : "desired: disabled",
    }),
  );
  if (camera.line_configured) {
    badges.appendChild(el("span", { className: "badge", text: "line configured" }));
  }
  card.appendChild(badges);

  const meta = el("div", { className: "camera-meta" });
  meta.appendChild(el("span", { text: `VMS id: ${camera.vms_camera_id}` }));
  meta.appendChild(
    el("span", {
      text: `Source: ${camera.rtsp_url_masked}${
        camera.rtsp_has_credentials ? " (credentials stored)" : ""
      }`,
    }),
  );
  meta.appendChild(
    el("span", {
      text: `${camera.inference_fps} FPS - conf ${camera.confidence_threshold} - ${camera.enabled_classes.join(", ")}`,
    }),
  );
  meta.appendChild(
    el("span", { text: `Last frame: ${ageText(runtime.last_frame_at)}` }),
  );
  meta.appendChild(
    el("span", { text: `Last event: ${ageText(runtime.last_event_at)}` }),
  );
  if (runtime.reconnect_attempt > 0) {
    meta.appendChild(
      el("span", { text: `Reconnect attempt ${runtime.reconnect_attempt}` }),
    );
  }
  card.appendChild(meta);

  if (runtime.last_error) {
    card.appendChild(
      el("p", {
        className: "camera-error",
        text: `${runtime.last_error.code}: ${runtime.last_error.message}`,
      }),
    );
  }

  const actions = el("div", { className: "camera-actions" });
  actions.appendChild(
    el("a", {
      className: "button-link",
      text: "Configure",
      attrs: { href: `#/cameras/${camera.id}/configure` },
    }),
  );

  const toggle = el("button", {
    text: camera.enabled ? "Disable" : "Enable",
    attrs: { type: "button", "data-testid": `toggle-${camera.id}` },
  });
  toggle.addEventListener("click", () =>
    mutateCamera(toggle, camera.id, { enabled: !camera.enabled }),
  );
  actions.appendChild(toggle);

  const edit = el("button", { text: "Edit", attrs: { type: "button" } });
  edit.addEventListener("click", () => openCameraDialog(camera));
  actions.appendChild(edit);

  const remove = el("button", {
    className: "danger",
    text: "Delete",
    attrs: { type: "button", "data-testid": `delete-${camera.id}` },
  });
  remove.addEventListener("click", () => deleteCamera(remove, camera));
  actions.appendChild(remove);

  card.appendChild(actions);
  return card;
}

async function mutateCamera(button, cameraId, payload) {
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "Working...";
  try {
    const response = await fetch(`/api/cameras/${cameraId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await readJson(response);
    if (!response.ok) {
      window.alert(errorMessage(body, "The change could not be applied."));
    }
  } catch {
    window.alert("The analytics service could not be reached.");
  } finally {
    button.disabled = false;
    button.textContent = original;
    loadCameras().catch(() => {});
  }
}

async function deleteCamera(button, camera) {
  const confirmed = await confirmAction(
    `Delete "${camera.name}"? Its event history and images are kept and remain browsable.`,
  );
  if (!confirmed) return;
  button.disabled = true;
  try {
    const response = await fetch(`/api/cameras/${camera.id}`, { method: "DELETE" });
    if (response.status === 503) {
      const body = await readJson(response);
      window.alert(
        errorMessage(body, "The worker did not stop in time.") +
          " The camera stays disabled; use Delete again to retry.",
      );
    } else if (!response.ok && response.status !== 404) {
      window.alert(errorMessage(await readJson(response), "Delete failed."));
    }
  } catch {
    window.alert("The analytics service could not be reached.");
  } finally {
    button.disabled = false;
    loadCameras().catch(() => {});
  }
}

/* ------------------------------------------------------------------ *
 * Camera dialog
 * ------------------------------------------------------------------ */

function setModalOpen(id, open) {
  $(id).hidden = !open;
}

function openCameraDialog(camera) {
  state.editingCameraId = camera ? camera.id : null;
  setText($("camera-modal-title"), camera ? "Edit analytics camera" : "Add analytics camera");
  $("camera-vms-id").value = camera ? camera.vms_camera_id : "";
  $("camera-name").value = camera ? camera.name : "";
  $("camera-rtsp").value = "";
  $("camera-rtsp").required = !camera;
  $("camera-rtsp").placeholder = camera
    ? "Leave blank to keep the stored source"
    : "rtsp://host.docker.internal:8555/vms_cam_0123abcd";
  $("camera-fps").value = camera ? camera.inference_fps : 5;
  $("camera-confidence").value = camera ? camera.confidence_threshold : 0.25;
  $("camera-person").checked = camera
    ? camera.enabled_classes.includes("person")
    : true;
  $("camera-vehicle").checked = camera
    ? camera.enabled_classes.includes("vehicle")
    : true;
  $("camera-enabled").checked = camera ? camera.enabled : true;
  show($("camera-error"), false);
  setModalOpen("camera-modal", true);
  $("camera-vms-id").focus();
}

async function submitCameraForm(event) {
  event.preventDefault();
  const submit = $("camera-submit");
  const errorNode = $("camera-error");
  show(errorNode, false);

  const classes = [];
  if ($("camera-person").checked) classes.push("person");
  if ($("camera-vehicle").checked) classes.push("vehicle");
  if (classes.length === 0) {
    setText(errorNode, "Select at least one object category.");
    show(errorNode, true);
    return;
  }

  const editing = state.editingCameraId;
  const payload = {
    vms_camera_id: $("camera-vms-id").value.trim(),
    name: $("camera-name").value.trim(),
    inference_fps: Number($("camera-fps").value),
    confidence_threshold: Number($("camera-confidence").value),
    enabled_classes: classes,
    enabled: $("camera-enabled").checked,
  };
  const url = $("camera-rtsp").value.trim();
  if (url || !editing) payload.rtsp_url = url;

  submit.disabled = true;
  try {
    const response = await fetch(
      editing ? `/api/cameras/${editing}` : "/api/cameras",
      {
        method: editing ? "PATCH" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    );
    const body = await readJson(response);
    if (!response.ok) {
      const detail = fieldErrors(body);
      setText(
        errorNode,
        detail || errorMessage(body, "The camera could not be saved."),
      );
      show(errorNode, true);
      return;
    }
    setModalOpen("camera-modal", false);
    loadCameras().catch(() => {});
  } catch {
    setText(errorNode, "The analytics service could not be reached.");
    show(errorNode, true);
  } finally {
    submit.disabled = false;
  }
}

function confirmAction(message) {
  setText($("confirm-text"), message);
  setModalOpen("confirm-modal", true);
  return new Promise((resolve) => {
    state.confirmResolver = resolve;
  });
}

function resolveConfirm(value) {
  setModalOpen("confirm-modal", false);
  const resolver = state.confirmResolver;
  state.confirmResolver = null;
  if (resolver) resolver(value);
}

/* ------------------------------------------------------------------ *
 * Configure view
 * ------------------------------------------------------------------ */

async function loadConfigure() {
  const cameraId = state.selectedCameraId;
  if (!cameraId) return;

  const response = await apiFetch(CONFIGURE_KEY, `/api/cameras/${cameraId}`);
  if (state.route !== "configure" || state.selectedCameraId !== cameraId) return;
  if (response.status === 404) {
    window.location.hash = "#/cameras";
    return;
  }
  const camera = await readJson(response);
  if (!camera) return;
  setText($("configure-title"), `Line for ${camera.name}`);
  setText(
    $("header-status"),
    `${camera.runtime.state}${camera.runtime.stopping ? " (stopping)" : ""}`,
  );

  if (state.lineSaved === null && !state.lineDirty) {
    const lineResponse = await fetch(`/api/cameras/${cameraId}/line`);
    if (lineResponse.status === 200) {
      const line = await readJson(lineResponse);
      state.lineSaved = line;
      state.lineDraft = {
        a: { ...line.a },
        b: { ...line.b },
      };
      $("line-name").value = line.name;
      $("line-direction").value = line.direction;
      $("line-enabled").checked = line.enabled;
      writeCoordinateInputs();
    } else if (lineResponse.status === 404) {
      state.lineSaved = false;
      if (!$("line-name").value) $("line-name").value = "Entry line";
    }
  }
  drawOverlay();
}

async function loadSnapshot() {
  const cameraId = state.selectedCameraId;
  if (!cameraId) return;
  if (state.dragging) return; // never swap the image mid-drag

  const response = await apiFetch(
    SNAPSHOT_KEY,
    `/api/cameras/${cameraId}/snapshot`,
  );
  if (state.route !== "configure" || state.selectedCameraId !== cameraId) return;

  const status = $("snapshot-status");
  if (response.status === 200) {
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const previous = state.snapshotObjectUrl;
    state.snapshotObjectUrl = url;
    $("snapshot-image").src = url;
    if (previous) URL.revokeObjectURL(previous);
    const age = response.headers.get("X-Frame-Age-Ms");
    setText(
      status,
      age !== null
        ? `Live frame, ${Math.round(Number(age) / 100) / 10}s old.`
        : "Live frame.",
    );
    status.removeAttribute("data-tone");
  } else if (response.status === 409) {
    setText(status, "Waiting for the first decoded frame from this camera...");
  } else if (response.status === 404) {
    window.location.hash = "#/cameras";
  } else {
    setText(status, "The snapshot is unavailable right now.");
  }
  drawOverlay();
}

function canvasMetrics() {
  const image = $("snapshot-image");
  const canvas = $("line-canvas");
  const rect = image.getBoundingClientRect();
  const width = rect.width || canvas.clientWidth || 1;
  const height = rect.height || canvas.clientHeight || 1;
  if (canvas.width !== Math.round(width) || canvas.height !== Math.round(height)) {
    canvas.width = Math.round(width);
    canvas.height = Math.round(height);
  }
  return { rect, width: canvas.width, height: canvas.height };
}

function toNormalized(event) {
  const { rect } = canvasMetrics();
  const x = (event.clientX - rect.left) / (rect.width || 1);
  const y = (event.clientY - rect.top) / (rect.height || 1);
  return {
    x: Math.min(Math.max(x, 0), 1),
    y: Math.min(Math.max(y, 0), 1),
  };
}

function drawOverlay() {
  const canvas = $("line-canvas");
  const context = canvas.getContext("2d");
  const { width, height } = canvasMetrics();
  context.clearRect(0, 0, width, height);
  const draft = state.lineDraft;
  if (!draft || !draft.a || !draft.b) return;

  const ax = draft.a.x * width;
  const ay = draft.a.y * height;
  const bx = draft.b.x * width;
  const by = draft.b.y * height;

  context.lineWidth = 3;
  context.strokeStyle = "#4c9aff";
  context.beginPath();
  context.moveTo(ax, ay);
  context.lineTo(bx, by);
  context.stroke();

  // Positive unit normal n = (-dy, dx) / |v| points into the B half-plane,
  // matching classify_side() in app/analytics/crossing.py exactly.
  const dx = bx - ax;
  const dy = by - ay;
  const length = Math.hypot(dx, dy) || 1;
  const nx = -dy / length;
  const ny = dx / length;
  const midX = (ax + bx) / 2;
  const midY = (ay + by) / 2;
  const reach = 34;

  const direction = $("line-direction").value;
  context.strokeStyle = "#37c26a";
  context.fillStyle = "#37c26a";
  if (direction === "A_TO_B" || direction === "BOTH") {
    drawArrow(context, midX - nx * reach, midY - ny * reach, midX + nx * reach, midY + ny * reach);
  }
  if (direction === "B_TO_A" || direction === "BOTH") {
    const offset = direction === "BOTH" ? 26 : 0;
    const ox = (dx / length) * offset;
    const oy = (dy / length) * offset;
    drawArrow(
      context,
      midX + nx * reach + ox,
      midY + ny * reach + oy,
      midX - nx * reach + ox,
      midY - ny * reach + oy,
    );
  }

  context.font = "13px system-ui, sans-serif";
  context.fillStyle = "#e6ebf2";
  context.fillText("A side", midX - nx * (reach + 22) - 20, midY - ny * (reach + 22));
  context.fillText("B side", midX + nx * (reach + 22) - 20, midY + ny * (reach + 22));

  drawEndpoint(context, ax, ay, "#ffd166", "A");
  drawEndpoint(context, bx, by, "#ef476f", "B");
}

function drawArrow(context, fromX, fromY, toX, toY) {
  context.beginPath();
  context.moveTo(fromX, fromY);
  context.lineTo(toX, toY);
  context.lineWidth = 3;
  context.stroke();
  const angle = Math.atan2(toY - fromY, toX - fromX);
  context.beginPath();
  context.moveTo(toX, toY);
  context.lineTo(toX - 10 * Math.cos(angle - Math.PI / 7), toY - 10 * Math.sin(angle - Math.PI / 7));
  context.lineTo(toX - 10 * Math.cos(angle + Math.PI / 7), toY - 10 * Math.sin(angle + Math.PI / 7));
  context.closePath();
  context.fill();
}

function drawEndpoint(context, x, y, color, label) {
  context.beginPath();
  context.arc(x, y, 8, 0, Math.PI * 2);
  context.fillStyle = color;
  context.fill();
  context.fillStyle = "#06121f";
  context.font = "bold 11px system-ui, sans-serif";
  context.fillText(label, x - 3, y + 4);
}

function writeCoordinateInputs() {
  const draft = state.lineDraft;
  if (!draft || !draft.a || !draft.b) return;
  $("line-ax").value = draft.a.x.toFixed(3);
  $("line-ay").value = draft.a.y.toFixed(3);
  $("line-bx").value = draft.b.x.toFixed(3);
  $("line-by").value = draft.b.y.toFixed(3);
}

function readCoordinateInputs() {
  const values = ["line-ax", "line-ay", "line-bx", "line-by"].map((id) =>
    Number($(id).value),
  );
  if (values.some((value) => !Number.isFinite(value))) return;
  state.lineDraft = {
    a: { x: clamp01(values[0]), y: clamp01(values[1]) },
    b: { x: clamp01(values[2]), y: clamp01(values[3]) },
  };
  state.lineDirty = true;
  drawOverlay();
}

function clamp01(value) {
  return Math.min(Math.max(value, 0), 1);
}

function nearEndpoint(point, endpoint) {
  if (!endpoint) return false;
  return Math.hypot(point.x - endpoint.x, point.y - endpoint.y) < 0.05;
}

function onCanvasPointerDown(event) {
  const canvas = $("line-canvas");
  const point = toNormalized(event);
  const draft = state.lineDraft;
  canvas.setPointerCapture(event.pointerId);

  if (draft && nearEndpoint(point, draft.a)) {
    state.dragging = "a";
  } else if (draft && nearEndpoint(point, draft.b)) {
    state.dragging = "b";
  } else {
    state.lineDraft = { a: point, b: point };
    state.dragging = "b";
  }
  state.lineDirty = true;
  applyDrag(point);
}

function onCanvasPointerMove(event) {
  if (!state.dragging) return;
  applyDrag(toNormalized(event));
}

function onCanvasPointerUp(event) {
  if (!state.dragging) return;
  applyDrag(toNormalized(event));
  state.dragging = null;
  try {
    $("line-canvas").releasePointerCapture(event.pointerId);
  } catch {
    /* the pointer was already released */
  }
  validateDraft();
}

function applyDrag(point) {
  if (!state.lineDraft || !state.dragging) return;
  state.lineDraft[state.dragging] = point;
  writeCoordinateInputs();
  drawOverlay();
}

function draftLength() {
  const draft = state.lineDraft;
  if (!draft || !draft.a || !draft.b) return 0;
  return Math.hypot(draft.b.x - draft.a.x, draft.b.y - draft.a.y);
}

function validateDraft() {
  const errorNode = $("line-error");
  if (draftLength() < MIN_LINE_LENGTH) {
    setText(
      errorNode,
      `The line must be at least ${MIN_LINE_LENGTH} of the image across. Drag further.`,
    );
    show(errorNode, true);
    return false;
  }
  show(errorNode, false);
  return true;
}

async function saveLine(event) {
  event.preventDefault();
  if (!validateDraft()) return;
  const cameraId = state.selectedCameraId;
  const button = $("line-save");
  const errorNode = $("line-error");
  button.disabled = true;
  try {
    const response = await fetch(`/api/cameras/${cameraId}/line`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: $("line-name").value.trim(),
        a: state.lineDraft.a,
        b: state.lineDraft.b,
        direction: $("line-direction").value,
        enabled: $("line-enabled").checked,
      }),
    });
    const body = await readJson(response);
    if (!response.ok) {
      setText(errorNode, fieldErrors(body) || errorMessage(body, "Save failed."));
      show(errorNode, true);
      return;
    }
    state.lineSaved = body;
    state.lineDraft = { a: { ...body.a }, b: { ...body.b } };
    state.lineDirty = false;
    writeCoordinateInputs();
    drawOverlay();
    const note = $("line-note");
    setText(note, "Line saved. The tracking session for this camera was reset.");
    show(note, true);
  } catch {
    setText(errorNode, "The analytics service could not be reached.");
    show(errorNode, true);
  } finally {
    button.disabled = false;
  }
}

async function deleteLine() {
  const confirmed = await confirmAction(
    "Delete this line? The camera keeps running but stops producing events until a new line is saved.",
  );
  if (!confirmed) return;
  const button = $("line-delete");
  button.disabled = true;
  try {
    await fetch(`/api/cameras/${state.selectedCameraId}/line`, { method: "DELETE" });
    state.lineSaved = false;
    state.lineDraft = null;
    state.lineDirty = false;
    ["line-ax", "line-ay", "line-bx", "line-by"].forEach((id) => {
      $(id).value = "";
    });
    drawOverlay();
    const note = $("line-note");
    setText(note, "Line deleted. The tracking session was reset.");
    show(note, true);
  } finally {
    button.disabled = false;
  }
}

function resetDraft() {
  if (state.lineSaved) {
    state.lineDraft = {
      a: { ...state.lineSaved.a },
      b: { ...state.lineSaved.b },
    };
    $("line-name").value = state.lineSaved.name;
    $("line-direction").value = state.lineSaved.direction;
    $("line-enabled").checked = state.lineSaved.enabled;
    writeCoordinateInputs();
  } else {
    state.lineDraft = null;
  }
  state.lineDirty = false;
  show($("line-error"), false);
  drawOverlay();
}

function redraw() {
  state.lineDraft = null;
  state.lineDirty = true;
  ["line-ax", "line-ay", "line-bx", "line-by"].forEach((id) => {
    $(id).value = "";
  });
  const note = $("line-note");
  setText(note, "Drag on the image to place A, then release to place B.");
  show(note, true);
  drawOverlay();
}

/* ------------------------------------------------------------------ *
 * Events view
 * ------------------------------------------------------------------ */

async function loadCameraOptions() {
  try {
    const response = await fetch("/api/cameras");
    const cameras = await readJson(response);
    if (!Array.isArray(cameras)) return;
    const select = $("filter-camera");
    const current = select.value;
    clear(select);
    select.appendChild(el("option", { text: "All cameras", attrs: { value: "" } }));
    for (const camera of cameras) {
      select.appendChild(
        el("option", { text: camera.name, attrs: { value: camera.id } }),
      );
    }
    select.value = current;
  } catch {
    /* filters still work without the convenience list */
  }
}

function collectFilters() {
  const filters = {};
  const camera = $("filter-camera").value;
  const category = $("filter-category").value;
  const objectClass = $("filter-class").value;
  const direction = $("filter-direction").value;
  const from = $("filter-from").value;
  const to = $("filter-to").value;
  if (camera) filters.camera_id = camera;
  if (category) filters.object_category = category;
  if (objectClass) filters.object_class = objectClass;
  if (direction) filters.direction = direction;
  if (from) filters.from = `${from}:00Z`.replace(/:00:00Z$/, ":00Z");
  if (to) filters.to = `${to}:00Z`.replace(/:00:00Z$/, ":00Z");
  return filters;
}

async function loadEvents({ reset = false } = {}) {
  if (reset) {
    state.eventFilters = collectFilters();
    state.eventCursor = null;
    state.eventIds = new Set();
    clear($("event-list"));
    state.selectedEventId = null;
    show($("event-detail"), false);
  }
  const params = new URLSearchParams(state.eventFilters);
  params.set("limit", "25");
  if (state.eventCursor) params.set("cursor", state.eventCursor);

  const errorNode = $("events-error");
  const response = await apiFetch(EVENTS_KEY, `/api/events?${params.toString()}`);
  const body = await readJson(response);
  if (state.route !== "events") return;
  if (!response.ok) {
    setText(errorNode, errorMessage(body, "Events could not be loaded."));
    show(errorNode, true);
    return;
  }
  show(errorNode, false);

  const list = $("event-list");
  for (const event of body.items) {
    if (state.eventIds.has(event.id)) continue;
    state.eventIds.add(event.id);
    list.appendChild(renderEventRow(event));
  }
  state.eventCursor = body.next_cursor;
  show($("events-more"), Boolean(body.next_cursor));
  show($("events-empty"), state.eventIds.size === 0);
}

function renderEventRow(event) {
  const item = el("li");
  const button = el("button", {
    className: "event-row",
    attrs: {
      type: "button",
      "aria-pressed": "false",
      "data-event-id": event.id,
      "data-testid": `event-${event.id}`,
    },
  });

  button.appendChild(
    el("img", {
      attrs: {
        src: event.crop_url,
        alt: `Cropped ${event.object_class} that crossed ${event.line_name}`,
        loading: "lazy",
      },
    }),
  );

  const body = el("div", { className: "event-row-body" });
  body.appendChild(
    el("p", {
      className: "event-row-title",
      text: `${event.object_class} - ${event.direction}`,
    }),
  );
  body.appendChild(el("p", { className: "event-row-meta", text: event.crossed_at }));
  body.appendChild(
    el("p", {
      className: "event-row-meta",
      text: `${event.camera_name} / ${event.line_name}`,
    }),
  );
  body.appendChild(
    el("p", {
      className: "event-row-meta",
      text: `track ${event.track_id} - session ${event.worker_session_id.slice(0, 9)} - conf ${event.confidence.toFixed(2)}`,
    }),
  );
  button.appendChild(body);

  button.addEventListener("click", () => selectEvent(event.id));
  item.appendChild(button);
  return item;
}

async function selectEvent(eventId) {
  state.selectedEventId = eventId;
  for (const node of document.querySelectorAll(".event-row")) {
    node.setAttribute(
      "aria-pressed",
      node.dataset.eventId === eventId ? "true" : "false",
    );
  }
  show($("recording-status"), false);

  const response = await apiFetch(DETAIL_KEY, `/api/events/${eventId}`);
  if (state.selectedEventId !== eventId) return;
  const event = await readJson(response);
  if (!response.ok || !event) return;

  setText($("detail-title"), `${event.object_class} - ${event.direction}`);
  const image = $("detail-frame");
  const note = $("detail-image-note");
  show(note, false);
  image.hidden = false;
  image.onerror = () => {
    image.hidden = true;
    setText(
      note,
      "The stored full frame for this event is no longer available; its metadata is kept.",
    );
    note.setAttribute("data-tone", "error");
    show(note, true);
  };
  image.src = event.frame_url;
  image.alt = `Full frame for the ${event.object_class} that crossed ${event.line_name}`;

  const fields = $("detail-fields");
  clear(fields);
  const rows = [
    ["Event", event.id],
    ["Crossed at (UTC)", event.crossed_at],
    ["Camera", event.camera_name],
    ["VMS camera", event.vms_camera_id],
    ["Line", event.line_name],
    ["Direction", event.direction],
    ["Class", `${event.object_class} (${event.object_category})`],
    ["Confidence", event.confidence.toFixed(3)],
    ["Track", String(event.track_id)],
    ["Session", event.worker_session_id],
    ["Frame size", `${event.frame_width} x ${event.frame_height}`],
    [
      "Box",
      `${event.bbox.x1.toFixed(3)}, ${event.bbox.y1.toFixed(3)} - ${event.bbox.x2.toFixed(3)}, ${event.bbox.y2.toFixed(3)}`,
    ],
  ];
  for (const [term, value] of rows) {
    fields.appendChild(el("dt", { text: term }));
    fields.appendChild(el("dd", { text: value }));
  }
  show($("event-detail"), true);
}

async function viewRecording() {
  const eventId = state.selectedEventId;
  if (!eventId) return;
  const button = $("detail-recording");
  const status = $("recording-status");
  button.disabled = true;
  setText(status, "Asking the VMS which recording covers this moment...");
  status.removeAttribute("data-tone");
  show(status, true);
  try {
    const response = await apiFetch(
      RECORDING_KEY,
      `/api/events/${eventId}/recording`,
    );
    const body = await readJson(response);
    if (state.selectedEventId !== eventId) return;
    if (!response.ok || !body) {
      setText(status, errorMessage(body, "The lookup failed."));
      status.setAttribute("data-tone", "error");
      return;
    }
    if (body.status === "AVAILABLE") {
      setText(status, "Recording found. Opening it in a new tab.");
      status.setAttribute("data-tone", "ok");
      window.open(body.recording.playback_url, "_blank", "noopener");
    } else if (body.status === "NOT_FOUND") {
      setText(
        status,
        "No VMS recording contains this timestamp. Recording may have been off, or the retention window has passed.",
      );
      status.setAttribute("data-tone", "error");
    } else {
      setText(
        status,
        `The VMS recording service is unavailable (${body.reason}). The event itself is unaffected; press View recording to retry.`,
      );
      status.setAttribute("data-tone", "error");
    }
  } catch (error) {
    if (error instanceof StaleResponse) return;
    setText(status, "The analytics service could not be reached.");
    status.setAttribute("data-tone", "error");
  } finally {
    button.disabled = false;
  }
}

/* ------------------------------------------------------------------ *
 * Wiring
 * ------------------------------------------------------------------ */

function init() {
  setModalOpen("camera-modal", false);
  setModalOpen("confirm-modal", false);
  hideConnectionBanner();

  $("add-camera").addEventListener("click", () => openCameraDialog(null));
  $("camera-form").addEventListener("submit", submitCameraForm);
  $("camera-cancel").addEventListener("click", () =>
    setModalOpen("camera-modal", false),
  );
  $("confirm-yes").addEventListener("click", () => resolveConfirm(true));
  $("confirm-no").addEventListener("click", () => resolveConfirm(false));

  $("line-form").addEventListener("submit", saveLine);
  $("line-delete").addEventListener("click", deleteLine);
  $("line-reset").addEventListener("click", resetDraft);
  $("line-redraw").addEventListener("click", redraw);
  $("line-direction").addEventListener("change", drawOverlay);
  for (const id of ["line-ax", "line-ay", "line-bx", "line-by"]) {
    $(id).addEventListener("change", readCoordinateInputs);
  }

  const canvas = $("line-canvas");
  canvas.addEventListener("pointerdown", onCanvasPointerDown);
  canvas.addEventListener("pointermove", onCanvasPointerMove);
  canvas.addEventListener("pointerup", onCanvasPointerUp);
  canvas.addEventListener("pointercancel", onCanvasPointerUp);

  $("event-filters").addEventListener("submit", (event) => {
    event.preventDefault();
    loadEvents({ reset: true }).catch(() => {});
  });
  $("filters-clear").addEventListener("click", () => {
    $("event-filters").reset();
    loadEvents({ reset: true }).catch(() => {});
  });
  $("events-refresh").addEventListener("click", () =>
    loadEvents({ reset: true }).catch(() => {}),
  );
  $("events-more").addEventListener("click", () => loadEvents().catch(() => {}));
  $("detail-recording").addEventListener("click", viewRecording);

  window.addEventListener("hashchange", () => {
    if (state.route === "configure" && state.lineDirty) {
      if (!window.confirm("Discard the unsaved line edit?")) return;
    }
    applyRoute();
  });

  window.addEventListener("resize", () => {
    if (state.route === "configure") drawOverlay();
  });
  if (window.ResizeObserver) {
    new ResizeObserver(() => {
      if (state.route === "configure") drawOverlay();
    }).observe($("snapshot-frame"));
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    if (state.route === "cameras") loadCameras().catch(() => {});
    if (state.route === "configure") {
      loadConfigure().catch(() => {});
      loadSnapshot().catch(() => {});
    }
  });

  applyRoute();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}

export { state };

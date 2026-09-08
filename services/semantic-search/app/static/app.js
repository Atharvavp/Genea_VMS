/*
 * Component 5 dashboard.
 *
 * Two rules run through the whole file:
 *
 *  - Nothing from the server is ever inserted as markup. Every external string
 *    goes through textContent or a property assignment, so an event whose
 *    camera name is "<img src=x onerror=alert(1)>" renders as that text.
 *  - Every request carries a generation number and an AbortController, so a
 *    slow earlier response can never overwrite a newer one.
 */
"use strict";

(function () {
  const el = (id) => document.getElementById(id);

  const form = el("search-form");
  const queryInput = el("query");
  const imageInput = el("image");
  const imagePreview = el("image-preview");
  const resultsEl = el("results");
  const summaryEl = el("summary");
  const messageEl = el("message");
  const searchButton = el("search-button");
  const drawer = el("drawer");
  const drawerBody = el("drawer-body");
  const drawerTitle = el("drawer-title");

  const state = {
    generation: 0,
    controller: null,
    statusTimer: null,
    lastFocus: null,
    previewUrl: null,
  };

  // ---- helpers ----------------------------------------------------------

  function setMessage(text, kind) {
    if (!text) {
      messageEl.hidden = true;
      messageEl.textContent = "";
      return;
    }
    messageEl.textContent = text;
    messageEl.className = kind === "info" ? "message info" : "message";
    messageEl.hidden = false;
  }

  function clear(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function node(tag, className, text) {
    const created = document.createElement(tag);
    if (className) {
      created.className = className;
    }
    if (text !== undefined && text !== null) {
      created.textContent = String(text);
    }
    return created;
  }

  function utcToInput(value) {
    return value ? value.replace("Z", "").slice(0, 19) : "";
  }

  function inputToUtc(value) {
    if (!value) {
      return null;
    }
    const withSeconds = value.length === 16 ? value + ":00" : value;
    return withSeconds + ".000Z";
  }

  function describeError(payload, fallback) {
    const code = payload && payload.error && payload.error.code;
    const known = {
      invalid_query: "That query is not usable. Enter 1-256 characters of text.",
      invalid_time_range: "Check the From and To times: From must be earlier.",
      validation_error: "Some search options are not valid.",
      image_too_large: "That image is too large. Use a JPEG or PNG under 8 MiB.",
      unsupported_image_type: "Only JPEG and PNG images can be used as a query.",
      invalid_image: "That file could not be read as an image.",
      inference_queue_full: "The model is busy. Try again in a moment.",
      search_unavailable: "Local search is not ready on this service.",
      upstream_unavailable: "Component 4 is unreachable.",
      upstream_timeout: "Component 4 did not respond in time.",
      event_artifact_gone: "Component 4 no longer has that image.",
      event_not_found: "That event is not in this index.",
    };
    return known[code] || fallback;
  }

  // ---- status -----------------------------------------------------------

  function paint(pill, label, value, tone) {
    pill.textContent = label + " · " + value;
    pill.className = "pill " + tone;
  }

  async function refreshStatus() {
    try {
      const [healthResponse, statusResponse] = await Promise.all([
        fetch("/health", { headers: { Accept: "application/json" } }),
        fetch("/api/index/status", { headers: { Accept: "application/json" } }),
      ]);
      const health = await healthResponse.json();
      paint(
        el("status-search"),
        "search",
        health.search === "ok" ? "ready" : "unavailable",
        health.search === "ok" ? "ok" : "bad"
      );

      if (!statusResponse.ok) {
        paint(el("status-index"), "index", "unknown", "warn");
        return;
      }
      const status = await statusResponse.json();
      const pending = status.pending_representations + status.retryable_representations;
      const indexLabel =
        pending > 0
          ? status.searchable_events + " searchable, " + pending + " to index"
          : status.searchable_events + " searchable";
      paint(el("status-index"), "index", indexLabel, pending > 0 ? "warn" : "ok");

      const upstream = status.upstream_state;
      paint(
        el("status-upstream"),
        "Component 4",
        upstream === "available" ? "connected" : upstream,
        upstream === "available" ? "ok" : "warn"
      );
      // A degraded upstream is reported on its own pill and must never be
      // presented as a local search failure.
    } catch (error) {
      paint(el("status-search"), "search", "unreachable", "bad");
    }
  }

  async function loadFacets() {
    try {
      const response = await fetch("/api/index/facets");
      if (!response.ok) {
        return;
      }
      const facets = await response.json();
      fillSelect(el("camera"), "Any camera", facets.cameras.map((camera) => ({
        value: camera.camera_id,
        label: camera.camera_name + " (" + camera.count + ")",
      })));
      fillSelect(el("category"), "Any category", facets.object_categories.map(simple));
      fillSelect(el("klass"), "Any class", facets.object_classes.map(simple));
      fillSelect(el("direction"), "Any direction", facets.directions.map(simple));
      if (facets.earliest_crossed_at && !el("from").value) {
        el("from").min = utcToInput(facets.earliest_crossed_at);
      }
    } catch (error) {
      /* Facets are a convenience; their absence must not block searching. */
    }
  }

  function simple(value) {
    return { value: value, label: value };
  }

  function fillSelect(select, anyLabel, options) {
    const previous = select.value;
    clear(select);
    const any = node("option", null, anyLabel);
    any.value = "";
    select.appendChild(any);
    options.forEach((option) => {
      const created = node("option", null, option.label);
      created.value = option.value;
      select.appendChild(created);
    });
    if (previous) {
      select.value = previous;
    }
  }

  // ---- searching --------------------------------------------------------

  function collectFilters() {
    const filters = {};
    const camera = el("camera").value;
    const category = el("category").value;
    const klass = el("klass").value;
    const direction = el("direction").value;
    const from = inputToUtc(el("from").value);
    const to = inputToUtc(el("to").value);
    if (camera) filters.camera_id = camera;
    if (category) filters.object_category = category;
    if (klass) filters.object_class = klass;
    if (direction) filters.direction = direction;
    if (from) filters.from = from;
    if (to) filters.to = to;
    return filters;
  }

  function currentMode() {
    const checked = form.querySelector('input[name="mode"]:checked');
    return checked ? checked.value : "text";
  }

  async function runSearch(event) {
    event.preventDefault();
    setMessage(null);

    const mode = currentMode();
    const topK = el("top-k").value || "20";
    const minScore = el("min-score").value;

    if (mode === "text" && !queryInput.value.trim()) {
      setMessage("Enter something to search for.", "info");
      queryInput.focus();
      return;
    }
    if (mode === "image" && !imageInput.files.length) {
      setMessage("Choose a JPEG or PNG image to search with.", "info");
      imageInput.focus();
      return;
    }

    if (state.controller) {
      state.controller.abort();
    }
    const controller = new AbortController();
    state.controller = controller;
    const generation = ++state.generation;

    searchButton.disabled = true;
    summaryEl.textContent = "Searching…";

    let response;
    try {
      if (mode === "text") {
        const body = {
          query: queryInput.value,
          top_k: Number(topK),
          filters: collectFilters(),
        };
        if (minScore !== "") {
          body.min_score = Number(minScore);
        }
        response = await fetch("/api/search/text", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: controller.signal,
        });
      } else {
        const params = new URLSearchParams(collectFilters());
        params.set("top_k", topK);
        if (minScore !== "") {
          params.set("min_score", minScore);
        }
        const payload = new FormData();
        payload.append("image", imageInput.files[0]);
        response = await fetch("/api/search/image?" + params.toString(), {
          method: "POST",
          body: payload,
          signal: controller.signal,
        });
      }
    } catch (error) {
      if (error.name === "AbortError") {
        return;
      }
      if (generation === state.generation) {
        searchButton.disabled = false;
        summaryEl.textContent = "";
        setMessage("This service could not be reached.");
      }
      return;
    }

    // A response from an older search is discarded, never rendered.
    if (generation !== state.generation) {
      return;
    }
    searchButton.disabled = false;

    let payload = null;
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }
    if (!response.ok) {
      summaryEl.textContent = "";
      clear(resultsEl);
      setMessage(describeError(payload, "The search could not be completed."));
      return;
    }
    render(payload);
  }

  function render(payload) {
    clear(resultsEl);
    const shown = payload.results.length;
    summaryEl.textContent =
      shown +
      " of " +
      payload.candidate_count +
      " matching events · " +
      payload.elapsed_ms.toFixed(1) +
      " ms · index revision " +
      payload.index_revision;

    if (shown === 0) {
      const empty = node(
        "p",
        "empty",
        payload.candidate_count === 0
          ? "No indexed events match those filters yet."
          : "No event scored above the minimum score."
      );
      resultsEl.appendChild(empty);
      return;
    }
    payload.results.forEach((item) => resultsEl.appendChild(card(item)));
  }

  function card(item) {
    const button = node("button", "card");
    button.type = "button";
    button.setAttribute("aria-label", "Open details for the event on " + item.camera_name);

    const image = document.createElement("img");
    image.className = "thumb";
    image.loading = "lazy";
    image.alt = item.object_class + " on " + item.camera_name;
    image.src = item.crop_url;
    image.addEventListener("error", function () {
      // The image is gone or Component 4 is down. Keep every piece of metadata
      // and replace only the picture.
      const placeholder = node("div", "thumb-missing", "Image unavailable");
      if (image.parentNode) {
        image.parentNode.replaceChild(placeholder, image);
      }
    });
    button.appendChild(image);

    const body = node("div", "card-body");
    const title = node("div", "card-title");
    title.appendChild(node("span", null, item.object_class));
    title.appendChild(node("span", "score", item.score.toFixed(4)));
    body.appendChild(title);
    body.appendChild(node("div", "meta", item.camera_name));
    body.appendChild(node("div", "meta", item.crossed_at));
    body.appendChild(
      node("div", "meta", item.object_category + " · " + item.direction)
    );
    button.appendChild(body);

    button.addEventListener("click", () => openDrawer(item, button));
    return button;
  }

  // ---- detail drawer ----------------------------------------------------

  async function openDrawer(item, source) {
    state.lastFocus = source || null;
    drawer.hidden = false;
    drawerTitle.textContent = item.camera_name;
    clear(drawerBody);

    ["crop", "frame"].forEach((kind) => {
      const image = document.createElement("img");
      image.alt = kind === "crop" ? "Cropped detection" : "Full frame";
      image.src = kind === "crop" ? item.crop_url : item.frame_url;
      image.addEventListener("error", function () {
        const placeholder = node(
          "div",
          "thumb-missing",
          (kind === "crop" ? "Crop" : "Frame") + " image unavailable"
        );
        if (image.parentNode) {
          image.parentNode.replaceChild(placeholder, image);
        }
      });
      drawerBody.appendChild(image);
    });

    const list = node("dl", "kv");
    const rows = [
      ["Score", item.score.toFixed(6)],
      ["Crop score", item.representation_scores.crop === null
        ? "not indexed" : item.representation_scores.crop.toFixed(6)],
      ["Frame score", item.representation_scores.frame === null
        ? "not indexed" : item.representation_scores.frame.toFixed(6)],
      ["Camera", item.camera_name],
      ["Camera id", item.camera_id],
      ["VMS camera id", item.vms_camera_id],
      ["Crossed at", item.crossed_at],
      ["Class", item.object_category + " / " + item.object_class],
      ["Direction", item.direction],
      ["Event id", item.event_id],
    ];
    rows.forEach(([key, value]) => {
      list.appendChild(node("dt", null, key));
      list.appendChild(node("dd", null, value));
    });
    drawerBody.appendChild(list);

    const recordingButton = node("button", null, "View recording");
    recordingButton.type = "button";
    const recordingNote = node("p", "meta", "");
    recordingButton.addEventListener("click", () =>
      lookupRecording(item, recordingButton, recordingNote)
    );
    drawerBody.appendChild(recordingButton);
    drawerBody.appendChild(recordingNote);

    el("drawer-close").focus();
  }

  async function lookupRecording(item, button, note) {
    button.disabled = true;
    note.textContent = "Asking Component 4…";
    try {
      const response = await fetch(item.recording_url);
      const payload = await response.json();
      if (payload.status === "AVAILABLE" && payload.recording) {
        note.textContent =
          "Recording " + payload.recording.id + " (" +
          payload.recording.duration_seconds + "s). Opening in a new tab.";
        // The URL Component 4 returned is opened as-is; this service never
        // rewrites, proxies, or stores it.
        window.open(payload.recording.playback_url, "_blank", "noopener,noreferrer");
      } else if (payload.status === "NOT_FOUND") {
        note.textContent = "No recording covers this event (" +
          (payload.reason || "not found") + ").";
      } else {
        note.textContent = "Recording lookup unavailable (" +
          (payload.reason || "unavailable") + ").";
      }
    } catch (error) {
      note.textContent = "Recording lookup could not be completed.";
    } finally {
      button.disabled = false;
    }
  }

  function closeDrawer() {
    drawer.hidden = true;
    clear(drawerBody);
    if (state.lastFocus && document.contains(state.lastFocus)) {
      state.lastFocus.focus();
    }
  }

  // ---- wiring -----------------------------------------------------------

  form.addEventListener("submit", runSearch);
  el("drawer-close").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !drawer.hidden) {
      closeDrawer();
    }
  });

  form.querySelectorAll('input[name="mode"]').forEach((radio) => {
    radio.addEventListener("change", () => {
      const isText = currentMode() === "text";
      el("text-mode").hidden = !isText;
      el("image-mode").hidden = isText;
      setMessage(null);
    });
  });

  imageInput.addEventListener("change", () => {
    if (state.previewUrl) {
      URL.revokeObjectURL(state.previewUrl);
      state.previewUrl = null;
    }
    const file = imageInput.files[0];
    if (!file) {
      imagePreview.hidden = true;
      return;
    }
    if (file.size > 8 * 1024 * 1024) {
      setMessage("That image is larger than 8 MiB. Choose a smaller one.", "info");
    }
    state.previewUrl = URL.createObjectURL(file);
    imagePreview.src = state.previewUrl;
    imagePreview.hidden = false;
  });

  el("reset-button").addEventListener("click", () => {
    form.reset();
    imagePreview.hidden = true;
    el("text-mode").hidden = false;
    el("image-mode").hidden = true;
    clear(resultsEl);
    summaryEl.textContent = "";
    setMessage(null);
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      window.clearInterval(state.statusTimer);
      state.statusTimer = null;
    } else if (!state.statusTimer) {
      state.statusTimer = window.setInterval(refreshStatus, 10000);
      refreshStatus();
    }
  });

  refreshStatus();
  loadFacets();
  state.statusTimer = window.setInterval(refreshStatus, 10000);
})();

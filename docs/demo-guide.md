# End-to-end demo guide

A deterministic walkthrough from a freshly started repository to the complete
feature story: an RTSP camera, live view in the browser, recording and playback,
a real analytics event, and semantic retrieval of that event by text and by
image.

Every step below states what to **do**, what you should **see**, how long to
**wait**, and what to check **if it does not happen**.

Start here after reading the [README](../README.md) quick start. For *why* the
system behaves this way, see [architecture.md](architecture.md); for what has
been measured, see [validation.md](validation.md).

---

## What this demonstrates

| Steps | Capability |
| --- | --- |
| 1–4 | RTSP source publishing, ingest into the VMS, and browser WebRTC live view |
| 5 | Continuous recording and historical playback from MediaMTX |
| 6–8 | Analytics: decode, detection, tracking, and a directional line-crossing event |
| 9–12 | Semantic Search: indexing, text search, image search, and event detail |

Steps 1–5 are the **core assignment**. Steps 6–12 are the optional extensions.

---

## Before you start

**Resources.** Four images will be built on first run. Allow Docker at least
3 GiB of memory and several GB of disk. The first `start-all.sh` can take a long
time — Semantic Search downloads and bakes its model at build time.

**Browser.** Use Chromium or Chrome. It is the browser this system was validated
against; other browsers are untested for the WebRTC path.

**Environment.** Component 4 needs its local env file before anything starts:

```bash
cp services/video-analytics/.env.example services/video-analytics/.env
```

**Media.** Choose one of two paths before you begin:

| Path | Source video | What it demonstrates |
| --- | --- | --- |
| **A — core streaming** | The bundled `sample-320x240.mp4` (already in the repository, selectable as a mounted file) | Steps 1–5 completely: RTSP, live WebRTC, recording, playback |
| **B — complete C1→C5** | **Your own** legally usable H.264 MP4 in which a supported person or vehicle **visibly crosses the frame** | Everything, including steps 6–12 |

> **The bundled sample cannot produce analytics events.** It is a 10-second
> FFmpeg `testsrc` pattern — a synthetic test image with no person and no
> vehicle in it. Object detection will correctly find nothing. This is not a
> defect; it is why path B exists. Supported classes are `person`, `bicycle`,
> `car`, `motorcycle`, `bus` and `truck`.
>
> No event-generating video ships with this repository, because none that is
> both suitable and freely redistributable was available. Do not fabricate
> events by other means — an empty Events list with the bundled sample is the
> correct outcome.

**Codec.** Publish as **H.264**. The Simulator can publish H.265, but H.264 is
the validated path for both browser playback and analytics decode.

**Naming.** These steps work from completely empty volumes. If you are reusing
volumes from an earlier run, give your demo camera a **unique name and stream
path** so you can tell it apart from anything already there. You never need to
delete data to run this guide.

---

## Start and verify the stacks

**Action**

```bash
# The five-minute production default makes the recording step slow to observe.
# A 5-second segment length makes the demo deterministic without changing any
# committed configuration.
RECORDING_SEGMENT_DURATION=5s ./scripts/start-all.sh

./scripts/status.sh
./scripts/smoke-test.sh
```

**Expected result.** `start-all.sh` reports four distinct Compose projects in
preflight, then brings up Simulator → VMS → Analytics → Semantic Search, waiting
for each `/health`. `status.sh` shows all four as *application ready (HTTP 200)*.
`smoke-test.sh` ends with `passed 38, failed 0`.

**Wait/retry.** The first build is the slow one; later starts take under a
minute. `status.sh` is the readiness authority — a running container is not the
same as a ready application.

**If it does not happen**

- *Preflight fails asking for a signing key* — run the `cp` command above. The
  script will never create a `.env` for you.
- *Docker is not reachable* — start Docker Desktop and retry.
- *A port is already bound* — free `8080`, `8090`, `8100`, `8200`, `8554`,
  `8555`, `8889`, `9996` (TCP) and `8189` (UDP).
- *One stack never becomes ready* — the script prints the exact log command, for
  example `(cd services/video-analytics && docker compose logs --tail 50)`.
- *Smoke reports 37 instead of 38* — the 38th check is "Semantic Search upstream
  Component 4 is available". If Analytics is still starting, wait and re-run.

---

## 1. Create and start a simulated RTSP camera

**Action.** Open `http://localhost:8080/` and click **Add Camera**.

- **Name** — something unique, e.g. `demo-entrance`.
- **Stream path** — leave blank to have it generated from the name, or set it
  explicitly. Note the value: you will need it.
- **Source video** — choose **Use a mounted file** and pick
  `sample-320x240.mp4` (path A), or choose **Upload a file** and supply your own
  clip (path B).
- **Virtual camera output** — leave the codec as **H.264 (recommended)**.
- Tick **Loop the video forever** so the stream does not end mid-demo.
- Click **Save camera**, then **Start** on the new card.

**Expected result.** The card moves `CREATED` → `STARTING` → **`RUNNING`** and
shows the RTSP URL `rtsp://localhost:8554/simulator/<stream_path>`. Use the
**Copy** button to copy it.

**Wait/retry.** `STARTING` should resolve within a few seconds. A long upload of
a large file happens before the camera appears at all.

**If it does not happen**

- *Status is `ERROR`* — the card shows the failure reason. Most often the source
  file is not a readable video, or its container/codec cannot be published as
  requested.
- *It never leaves `STARTING`* — check
  `(cd services/rtsp-simulator && docker compose logs --tail 50 simulator)`.
- **`RUNNING` means the publisher process is alive**, not that anything
  downstream has received a frame. The next steps are what prove delivery.

---

## 2. (Optional) Verify the RTSP stream directly

**Action.** If you have `ffprobe` on the host:

```bash
ffprobe -v error -rtsp_transport tcp \
  -show_entries stream=codec_name,width,height \
  rtsp://localhost:8554/simulator/<stream_path>
```

**Expected result.** `codec_name=h264` with the source's resolution.

**Wait/retry.** None — if the camera is `RUNNING`, this answers immediately.

**If it does not happen.** A `DESCRIBE` 404 means the camera is not running.
This step is entirely optional; the UI path does not require host `ffprobe`.

---

## 3. Register the camera in the VMS

**Action.** Open `http://localhost:8090/` and click **Add Camera**.

- **Name** — unique, e.g. `demo-entrance-vms`.
- **RTSP URL** — the Simulator URL **with the container-reachable host**:

  ```text
  rtsp://host.docker.internal:8554/simulator/<stream_path>
  ```

- Leave **Enabled — the VMS ingests this camera** ticked.
- Tick **Record continuously — MediaMTX writes this camera to disk**.
- Click **Save**.

**Expected result.** A tile appears. Note its camera id (`cam_...`) — copy it
with **Copy URL** or read it from the tile; step 6 needs it.

**Wait/retry.** The tile appears immediately; health follows in the next step.

**If it does not happen**

- **The most common mistake is using `localhost`.** The VMS runs in a container,
  where `localhost` is the container itself. It must be
  `host.docker.internal:8554`, not `localhost:8554`.
- *Validation error* — the URL must be a well-formed `rtsp://` URL.

---

## 4. Verify browser live view

**Action.** Watch the tile, then click **Focus** for a large view.

**Expected result.** Two independent indicators, and both should be understood
separately:

- The **health badge** goes `UNKNOWN` → `OFFLINE` → **`ONLINE`**. This is the
  *source*: MediaMTX has the stream.
- The **player badge** goes `CONNECTING` → **`LIVE`**. This is *your browser's*
  WebRTC session.

With `LIVE`, you should see actually moving video — for the bundled sample, an
animated test pattern.

**Wait/retry.** `OFFLINE → ONLINE` typically resolves within several seconds of
health polling. The player then needs a moment to negotiate WebRTC. Allow a few
polls before concluding anything is wrong. Timings observed during acceptance
were single-digit seconds, but they are observations, not guarantees.

**If it does not happen**

- *Health stays `OFFLINE`* — this is a **source** problem. Confirm the Simulator
  camera is still `RUNNING`, and re-check the `host.docker.internal` URL.
- *Health is `ONLINE` but the player stays `CONNECTING` or flips to
  `RECONNECTING`* — this is a **browser/WebRTC** problem, not a source problem.
  Confirm UDP `8189` is free and not blocked, and use Chromium.
- *The badge reads `DISABLED`* — the camera is not enabled; use the tile's
  **Enable** button.

---

## 5. Confirm recording and play back history

**Action.** Watch the tile's recording badge, then click **Recordings**, choose
today's date under **Date (UTC)**, and select a listed timespan to play.

**Expected result.** The recording badge reads **`RECORDING`**. After at least
one segment has finalized, the Recordings dialog lists one or more timespans;
selecting one plays it in a native video player with normal controls.

**Wait/retry.** A segment must **finalize** before it can be listed. With
`RECORDING_SEGMENT_DURATION=5s` allow at least 10–15 seconds of recording, then
reopen the dialog. With the committed 5-minute default, it takes minutes.

**If it does not happen**

- *"No recordings for this day."* — the current segment has not finalized yet.
  Wait and reopen. Recordings are listed by **UTC** date: if your local date
  differs from UTC, check the neighbouring day.
- *The badge shows `WAITING`* — recording is enabled but the source is not
  available yet; fix step 4 first.
- *The **Recordings** button is disabled* — history can only be browsed while
  the camera is enabled. The files are still on disk.
- The badge is **inferred** from configuration and source availability. It is
  not byte-level proof that a file is growing; the Recordings list is.

> Path A ends here, having demonstrated the complete core assignment: RTSP
> capture, open-protocol streaming, browser live view, recording and playback.
> Steps 6–12 need path B media.

---

## 6. Register the VMS stream in Analytics

**Action.** Open `http://localhost:8100/` and click **Add camera**.

- **VMS camera id** — the `cam_...` id from step 3.
- **Name** — unique, e.g. `demo-entrance-analytics`.
- **RTSP URL** — the VMS *redistribution*, not the Simulator:

  ```text
  rtsp://host.docker.internal:8555/vms_<camera-id>
  ```

- Leave **Inference FPS** at `5`, **Confidence** at `0.25`, and both **person**
  and **vehicle** categories selected. Leave **Enabled** ticked.
- Click **Save**.

**Expected result.** The camera appears and its worker reaches **`RUNNING`**.

**Wait/retry.** `STARTING → RUNNING` normally takes several seconds.

**If it does not happen**

- *The worker sits in `RECONNECTING`* — **this means it is retrying, not
  working.** Almost always the RTSP URL is wrong: it must be port **8555** with
  the `vms_` prefix on the camera id, not port 8554 and not the Simulator path.
- *`ERROR`* — a fatal model, configuration or storage problem; check
  `(cd services/video-analytics && docker compose logs --tail 50)`.
- The source must be H.264. The decode pipeline is H.264-specific.

---

## 7. Configure a directional line

**Action.** Click **Configure line** on the camera. Wait for the snapshot to
appear, then drag a line **across the path objects actually travel** — for road
traffic, a horizontal line across the carriageway. Give it a **Name**, set
**Allowed direction** to **`BOTH`**, leave **Line enabled** ticked, and click
**Save line**.

**Expected result.** The line is drawn over the snapshot, the A and B endpoint
coordinates are shown as normalized values, and it saves without error.

**Wait/retry.** The snapshot needs the worker to have decoded a frame; give it a
few seconds after `RUNNING`.

**If it does not happen**

- *No snapshot appears* — the worker is not decoding. Fix step 6 first; do not
  place a line blind.
- *"Line too short"* — the line has a minimum length; drag further.
- *Direction* — `BOTH` is the right choice for a demo. `A_TO_B` and `B_TO_A`
  filter which crossings count; the emitted event always records the direction
  the object **actually** crossed.

---

## 8. Generate and inspect a real event

**Action.** Let the video play so an object crosses the line. Open the
**Events** tab and click **Refresh**. Click an event to open it.

**Expected result.** One row per crossing, with camera, class, category,
direction and UTC timestamp. Opening an event shows the **full frame** with the
object at the recorded position, and the **cropped** image of the object itself.

**Wait/retry.** Events appear within seconds of a crossing. Click **Refresh** —
the list does not poll continuously.

**If it does not happen**

- *No events, and the source is the bundled sample* — **expected.** The test
  pattern contains no supported object. Switch to path B media.
- *No events with real media* — the line probably does not intersect the path of
  the tracked object's **centre point**. Use **Redraw** and place it squarely
  across the traffic path. Also confirm the object's class is one of the six
  supported ones, and consider lowering **Confidence** slightly.
- *Filters* — check the Category, Class, Direction and UTC range filters are not
  excluding your event; **Clear** resets them.

---

## 9. Wait for Semantic Search to index the event

**Action.** Open `http://localhost:8200/`. Watch the status badges in the
header.

**Expected result.** The **Component 4** badge reads available, the **index**
badge is healthy, and the indexed event count grows to include your new event.
You can read the precise numbers directly:

```bash
curl -s http://localhost:8200/api/index/status
```

Look for `searchable_events` increasing and `upstream_state: "available"`.

**Wait/retry.** Discovery polls Component 4 every 10 seconds by default, and the
UI refreshes its status every 10 seconds. Embedding then takes time that varies
with host CPU and how many events arrived at once — **no exact indexing time is
promised.** An event becomes searchable once its crop has been embedded.

**If it does not happen**

- *Component 4 badge shows unavailable* — Analytics is down or unreachable.
  Semantic Search will report `status: degraded` with `search: ok`: it keeps
  serving what it already indexed and resumes automatically when Analytics
  returns. No restart is needed.
- *Counts stay at zero* — confirm Component 4 actually has events (step 8).

---

## 10. Run a text search

**Action.** Keep **Search mode** on **Text**. In *Describe what you are looking
for*, type what you actually saw — for example `car` if a car crossed. Optionally
narrow with the **Camera**, **Category** or **Class** filters. Click **Search**.

**Expected result.** A ranked result grid. Each card shows the event's crop with
its similarity score; the filters visibly reduce the candidate set.

**Wait/retry.** Search is immediate — it runs against an in-memory index.

**If it does not happen**

- *No results* — check the filters and any **Minimum score** value, and confirm
  step 9 finished indexing.
- **Read the scores correctly.** They are cosine similarities, not
  probabilities, and their absolute values are not meaningful on their own —
  only the ordering is. Across many near-identical crops from one scene, scores
  legitimately cluster tightly. Querying a class the corpus does not contain
  (`person walking` over a corpus of cars) should score visibly lower; that is
  the discrimination to look for.
- There is **no promise** that any particular event ranks first for a generic
  query.

---

## 11. Run an image search

**Action.** Save one event's crop from Component 4:

```bash
curl -s -o /tmp/query-crop.jpg \
  "http://localhost:8100/api/events/<event-id>/crop"
```

(You can also right-click and save the crop image from the Analytics event
view.) In Semantic Search, switch **Search mode** to **Image**, upload
`/tmp/query-crop.jpg` under *Query image*, and click **Search**.

**Expected result.** The source event appears in the results — and when you
query with an event's **own** crop against an unchanged index, it is returned at
rank 1 with a score of `1.000000`, because the query vector is identical to the
stored one.

**Wait/retry.** Immediate.

**If it does not happen**

- *Upload rejected* — JPEG or PNG only, up to 8 MiB.
- *The source event is not rank 1* — that exact-match result holds when the
  query really is that event's own crop and the index still contains it. A
  re-encoded, cropped or resized copy is a *similar* image, not an identical
  one, and will score below 1.0.

---

## 12. Open event detail and recording navigation

**Action.** Click any result to open the detail drawer. Inspect the crop, the
full frame and the metadata, then click **View recording**.

**Expected result.** The drawer shows both images, per-representation scores
(*Crop score* and *Frame score*), and the event's metadata. **View recording**
resolves whether a VMS recording covers the moment the event was recorded.

**Wait/retry.** Immediate.

**If it does not happen**

- *"Not found" or upstream unavailable* — this is an explicit, correct answer,
  not lost data. A recording only exists if that camera had recording enabled
  and a segment covering that timestamp had finalized. The lookup is read-only
  and optional; the event itself is unaffected.
- *An image fails to load* — crop and frame images are proxied from Component 4.
  If Analytics is stopped, images return an explicit error while local search
  and metadata keep working.

---

## Stop the stacks

**Action**

```bash
./scripts/stop-all.sh
```

**Expected result.** All four stacks stop in reverse order and the script prints
that named volumes were kept.

Everything survives: your Simulator camera, the VMS camera and its recordings,
the analytics events and images, and the semantic index. Running
`./scripts/start-all.sh` again brings the same state back.

Deleting the demo cameras from the three UIs afterwards is optional. **Do not**
use `docker compose down -v` as a cleanup step for this guide — it destroys the
volumes for that service.

---

## Troubleshooting the demonstrated path

Issues are documented next to the step they affect. This is the short
consolidated list.

| Symptom | Most likely cause |
| --- | --- |
| `start-all.sh` fails immediately in preflight | Docker is not running, or `services/video-analytics/.env` is missing |
| First start takes a very long time | Expected — four images are being built, Semantic Search last and slowest |
| A stack never becomes ready | A port conflict, or a build still resolving dependencies; the script prints the exact log command |
| Simulator camera reaches `ERROR` | The source file is not a readable video, or the codec cannot be published as requested |
| VMS camera stays `OFFLINE` | The RTSP URL uses `localhost` instead of `host.docker.internal`, or the Simulator camera is stopped |
| VMS `ONLINE` but the player will not go `LIVE` | A browser/WebRTC issue: use Chromium and confirm UDP `8189` is reachable |
| No recordings listed | The segment has not finalized yet, or you are looking at the wrong **UTC** date |
| Analytics worker stuck in `RECONNECTING` | Wrong RTSP URL — it must be port `8555` with the `vms_` prefix, and the source must be H.264 |
| No line-configuration snapshot | The worker is not decoding; fix the source before placing a line |
| No events | The bundled sample has no detectable objects, or the line does not cross the object's tracked centre path |
| Semantic Search shows Component 4 unavailable | Analytics is stopped; local search still works and indexing resumes automatically |
| An event is not searchable yet | Embedding has not finished; poll `GET /api/index/status` |

---

## What the bundled sample does not demonstrate

`services/rtsp-simulator/sample-media/sample-320x240.mp4` is a 10-second
FFmpeg `testsrc` pattern at 320×240, H.264. It exists so the system can be tried
immediately with zero setup, and it fully exercises steps 1–5.

It **cannot** demonstrate:

- object detection, tracking, or line-crossing events — it contains no person
  and no vehicle;
- consequently, anything in Semantic Search, which indexes only Component 4
  events.

To demonstrate steps 6–12, supply your own legally usable H.264 clip with a
supported object visibly crossing the frame. That real-event path has already
been validated during final acceptance — see
[validation.md](validation.md#real-analytics-event-validation) — with a genuine
vehicle crossing that produced events which Component 5 indexed and retrieved by
both text and image.

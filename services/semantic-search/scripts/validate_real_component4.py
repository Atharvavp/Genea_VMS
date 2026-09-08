#!/usr/bin/env python3
"""Drive the acceptance sequence against a RUNNING Component 5 and Component 4.

This is the host-orchestrated validation: it talks to both services over HTTP
only and prints evidence. It creates nothing upstream, deletes nothing, and
never touches the VMS, MediaMTX, a recording, or any container - stopping and
starting Component 4 for the outage steps is the operator's job, and the script
tells you when to do it.

    python scripts/validate_real_component4.py --wait-for-index 200
    python scripts/validate_real_component4.py --outage-check
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import httpx

DEFAULT_C5 = "http://localhost:8200"
DEFAULT_C4 = "http://localhost:8100"

QUERIES = [
    "person",
    "a person walking",
    "bus",
    "street scene with a bus",
    "car",
    "a white vehicle",
    "truck",
]

_failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {label}" + (f" :: {detail}" if detail else ""))
    else:
        _failures.append(label)
        print(f"  FAIL  {label}" + (f" :: {detail}" if detail else ""))


def status(client: httpx.Client, base: str) -> dict[str, Any]:
    return client.get(f"{base}/api/index/status").json()


def wait_for_index(client: httpx.Client, base: str, target: int, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = status(client, base)
        searchable = last["searchable_events"]
        pending = last["pending_representations"] + last["retryable_representations"]
        print(
            f"    indexing: searchable={searchable} pending={pending} "
            f"revision={last['index_revision']}",
            flush=True,
        )
        if searchable >= target or pending == 0:
            return last
        time.sleep(10)
    return last


def search_text(client: httpx.Client, base: str, query: str, **kwargs) -> dict:
    payload = {"query": query, "top_k": kwargs.pop("top_k", 5)}
    payload.update(kwargs)
    response = client.post(f"{base}/api/search/text", json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--c5", default=DEFAULT_C5)
    parser.add_argument("--c4", default=DEFAULT_C4)
    parser.add_argument("--wait-for-index", type=int, default=0)
    parser.add_argument("--index-timeout", type=float, default=1800.0)
    parser.add_argument("--outage-check", action="store_true")
    args = parser.parse_args(argv)

    client = httpx.Client(timeout=30.0)

    print("\n== upstream and local health ==")
    c4_health = client.get(f"{args.c4}/health").json()
    check("Component 4 is up", c4_health.get("status") in {"ok", "degraded"}, str(c4_health))
    health = client.get(f"{args.c5}/health")
    check("Component 5 health is 200", health.status_code == 200, str(health.json()))
    check("local search is ready", health.json()["search"] == "ok")

    upstream_events = client.get(f"{args.c4}/api/events?limit=1").json()
    print(f"  Component 4 head event: {upstream_events['items'][0]['id'] if upstream_events['items'] else 'none'}")

    if args.wait_for_index:
        print("\n== waiting for indexing ==")
        wait_for_index(client, args.c5, args.wait_for_index, args.index_timeout)

    state = status(client, args.c5)
    print("\n== index status ==")
    print(json.dumps(state, indent=2))
    check("the model identity is the frozen one",
          state["model_id"].startswith("siglip-base-p16-224@7fd15f06"))
    check("embeddings are 768-D float32",
          state["embedding_dim"] == 768 and state["embedding_dtype"] == "float32")
    check("the historical backfill completed", state["backfill_complete"] is True)
    check("events were discovered", state["known_events"] > 0, f"{state['known_events']} known")
    check("events are searchable", state["searchable_events"] > 0,
          f"{state['searchable_events']} searchable")

    print("\n== text search ==")
    for query in QUERIES:
        body = search_text(client, args.c5, query, top_k=5)
        top = body["results"][0] if body["results"] else None
        detail = (
            f"{len(body['results'])} results in {body['elapsed_ms']}ms; "
            f"top={top['object_class']}@{top['score']:.4f}" if top else "no results"
        )
        check(f"query '{query}' returned results", bool(body["results"]), detail)
        ids = [item["event_id"] for item in body["results"]]
        check(f"query '{query}' returned one card per event", len(ids) == len(set(ids)))

    print("\n== filters ==")
    facets = client.get(f"{args.c5}/api/index/facets").json()
    print(f"  cameras: {[(c['camera_id'], c['camera_name'], c['count']) for c in facets['cameras']]}")
    print(f"  classes: {facets['object_classes']}  directions: {facets['directions']}")
    if facets["cameras"]:
        camera = facets["cameras"][0]
        filtered = search_text(
            client, args.c5, "anything", top_k=100,
            filters={"camera_id": camera["camera_id"]},
        )
        check(
            "a camera filter restricts candidates",
            filtered["candidate_count"] == camera["count"],
            f"{filtered['candidate_count']} == {camera['count']}",
        )
        check(
            "every filtered result is from that camera",
            all(item["camera_id"] == camera["camera_id"] for item in filtered["results"]),
        )
    if facets["object_classes"]:
        klass = facets["object_classes"][0]
        by_class = search_text(
            client, args.c5, "anything", top_k=100, filters={"object_class": klass}
        )
        check(
            f"a class filter ({klass}) restricts candidates",
            all(item["object_class"] == klass for item in by_class["results"]),
        )
    contradictory = search_text(
        client, args.c5, "anything",
        filters={"object_category": "person", "object_class": "car"},
    )
    check("a contradictory filter returns empty, not an error",
          contradictory["results"] == [] and contradictory["candidate_count"] == 0)

    print("\n== image search with a real crop ==")
    sample = search_text(client, args.c5, "person", top_k=1)
    if sample["results"]:
        event = sample["results"][0]
        crop = client.get(f"{args.c5}{event['crop_url']}")
        check("the crop proxy served a JPEG",
              crop.status_code == 200 and crop.content.startswith(b"\xff\xd8\xff"),
              f"{crop.status_code}, {len(crop.content)} bytes")
        check("the proxy preserved immutable caching",
              crop.headers.get("cache-control") == "private, max-age=31536000, immutable")
        response = client.post(
            f"{args.c5}/api/search/image",
            files={"image": ("crop.jpg", crop.content, "image/jpeg")},
            params={"top_k": 5},
            timeout=60,
        )
        response.raise_for_status()
        body = response.json()
        top = body["results"][0]
        check("a known crop ranks its own event first",
              top["event_id"] == event["event_id"],
              f"top={top['event_id']} score={top['score']:.4f} expected={event['event_id']}")
        print(f"    image search elapsed_ms={body['elapsed_ms']}")

        print("\n== event detail and recording ==")
        detail = client.get(f"{args.c5}{event['detail_url']}").json()
        check("local detail is served", detail["event_id"] == event["event_id"],
              f"index_state={detail['index_state']}")
        recording = client.get(f"{args.c5}{event['recording_url']}")
        body = recording.json()
        check("recording lookup returns 200 with a known state",
              recording.status_code == 200
              and body["status"] in {"AVAILABLE", "NOT_FOUND", "UNAVAILABLE"},
              f"status={body['status']} reason={body['reason']}")
        if body["status"] == "AVAILABLE":
            check("the playback URL is an http(s) URL",
                  body["recording"]["playback_url"].startswith(("http://", "https://")),
                  body["recording"]["playback_url"])

    if args.outage_check:
        print("\n== Component 4 outage ==")
        print("  Stop Component 4 now (docker compose stop analytics), then press Enter.")
        input()
        before = status(client, args.c5)
        health = client.get(f"{args.c5}/health")
        check("health stays 200 during the outage", health.status_code == 200,
              str(health.json()))
        check("health reports degraded, not unavailable",
              health.json()["status"] == "degraded" and health.json()["search"] == "ok")
        outage_search = search_text(client, args.c5, "person", top_k=5)
        check("text search still works", bool(outage_search["results"]))
        image_probe = client.post(
            f"{args.c5}/api/search/image",
            files={"image": ("crop.jpg", _tiny_jpeg(), "image/jpeg")},
            params={"top_k": 3}, timeout=60,
        )
        check("image search still works", image_probe.status_code == 200)
        check("facets still work", client.get(f"{args.c5}/api/index/facets").status_code == 200)
        if outage_search["results"]:
            event_id = outage_search["results"][0]["event_id"]
            check("local detail still works",
                  client.get(f"{args.c5}/api/events/{event_id}").status_code == 200)
            rec = client.get(f"{args.c5}/api/events/{event_id}/recording")
            check("recording reports UNAVAILABLE with a safe reason",
                  rec.status_code == 200 and rec.json()["status"] == "UNAVAILABLE",
                  str(rec.json().get("reason")))

        print("\n  Start Component 4 again, then press Enter.")
        input()
        deadline = time.monotonic() + 180
        recovered = False
        while time.monotonic() < deadline:
            after = status(client, args.c5)
            if after["upstream_state"] == "available":
                recovered = True
                break
            time.sleep(5)
        check("the upstream state recovers automatically", recovered)
        after = status(client, args.c5)
        check("no local vector was lost during the outage",
              after["searchable_events"] >= before["searchable_events"],
              f"{before['searchable_events']} -> {after['searchable_events']}")

    print("\n== summary ==")
    if _failures:
        print(f"{len(_failures)} check(s) FAILED: {_failures}")
        return 1
    print("all checks passed")
    return 0


def _tiny_jpeg() -> bytes:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (64, 48), (90, 90, 90)).save(buffer, format="JPEG")
    return buffer.getvalue()


if __name__ == "__main__":
    raise SystemExit(main())

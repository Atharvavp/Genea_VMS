"""Measured retrieval quality on a user-owned corpus. Reports, rarely asserts.

This tier exists to produce evidence, not to defend a number. The corpus is
small, captured from one deployment, and labelled by Component 4's own detector
- which the Component 4 handoff records can itself be wrong. Nothing here should
be read as a general accuracy claim for the model or the service.

Run `python scripts/capture_eval_set.py` against a live Component 4 first; the
tier skips when the cache is absent, because the images are never committed.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.embeddings.runtime import EmbeddingRuntime

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "tests" / "assets" / "cache"
MANIFEST = ROOT / "tests" / "assets" / "semantic_eval_manifest.json"
MODEL_DIR = Path(os.environ.get("SEMANTIC_MODEL_DIR", "/opt/models/siglip"))

pytestmark = [
    pytest.mark.skipif(
        not MANIFEST.exists(), reason="no evaluation manifest; run capture_eval_set.py"
    ),
    pytest.mark.skipif(
        not (MODEL_DIR / "model.safetensors").is_file(), reason="no model snapshot"
    ),
]


@pytest.fixture(scope="module")
def corpus():
    manifest = json.loads(MANIFEST.read_text())
    events = [
        event
        for event in manifest["events"]
        if (CACHE / event["files"]["crop"]["name"]).is_file()
    ]
    if not events:
        pytest.skip("evaluation images are not cached locally")

    runtime = EmbeddingRuntime(model_dir=MODEL_DIR, torch_threads=2)
    runtime.load()

    crops, frames, labels = [], [], []
    for event in events:
        with Image.open(CACHE / event["files"]["crop"]["name"]) as image:
            crops.append(image.convert("RGB"))
        frame_info = event["files"].get("frame")
        if frame_info and (CACHE / frame_info["name"]).is_file():
            with Image.open(CACHE / frame_info["name"]) as image:
                frames.append(image.convert("RGB"))
        else:
            frames.append(None)
        labels.append(event["object_class"])

    crop_vectors = np.vstack(
        [runtime.embed_images(crops[i : i + 4]) for i in range(0, len(crops), 4)]
    )
    present = [index for index, frame in enumerate(frames) if frame is not None]
    frame_images = [frames[index] for index in present]
    frame_vectors = np.zeros_like(crop_vectors)
    if frame_images:
        stacked = np.vstack(
            [
                runtime.embed_images(frame_images[i : i + 4])
                for i in range(0, len(frame_images), 4)
            ]
        )
        for position, index in enumerate(present):
            frame_vectors[index] = stacked[position]
    has_frame = np.zeros(len(labels), dtype=bool)
    has_frame[present] = True

    return {
        "runtime": runtime,
        "labels": labels,
        "crop": crop_vectors,
        "frame": frame_vectors,
        "has_frame": has_frame,
        "events": events,
    }


def _recall_at_k(scores, labels, label, k=5) -> float:
    order = np.argsort(-scores)[:k]
    return float(any(labels[index] == label for index in order))


def test_recall_at_5_is_reported_for_crop_frame_and_max_fusion(corpus):
    labels = corpus["labels"]
    queries: dict[str, list[str]] = defaultdict(list)
    for event in corpus["events"]:
        for query in event["expected_queries"]:
            if query not in queries[event["object_class"]]:
                queries[event["object_class"]].append(query)

    runtime = corpus["runtime"]
    rows = []
    totals = {"crop": [], "frame": [], "max": []}
    for label, label_queries in sorted(queries.items()):
        for query in label_queries:
            vector = runtime.embed_texts([query])[0]
            crop_scores = corpus["crop"] @ vector
            frame_scores = np.where(
                corpus["has_frame"], corpus["frame"] @ vector, -np.inf
            )
            fused = np.fmax(crop_scores, frame_scores)
            row = {
                "query": query,
                "label": label,
                "crop": _recall_at_k(crop_scores, labels, label),
                "frame": _recall_at_k(frame_scores, labels, label),
                "max": _recall_at_k(fused, labels, label),
            }
            rows.append(row)
            for key in totals:
                totals[key].append(row[key])

    print(
        f"\nCorpus: {len(labels)} user-owned events "
        f"({dict((label, labels.count(label)) for label in sorted(set(labels)))})"
    )
    print(f"{'query':<28}{'label':<10}{'crop':>6}{'frame':>7}{'max':>6}")
    for row in rows:
        print(
            f"{row['query']:<28}{row['label']:<10}"
            f"{row['crop']:>6.0f}{row['frame']:>7.0f}{row['max']:>6.0f}"
        )
    macro = {key: float(np.mean(values)) for key, values in totals.items()}
    print(
        f"\nmacro Recall@5  crop={macro['crop']:.3f}  "
        f"frame={macro['frame']:.3f}  max-fusion={macro['max']:.3f}"
    )
    print(
        "NOTE: labels come from Component 4's detector and are not ground truth; "
        "this corpus is small and from one deployment. Not a general accuracy claim."
    )

    # The only assertion: retrieval is doing something, not that it is good.
    assert macro["max"] > 0.0
    assert len(rows) >= 4


def test_max_fusion_is_never_worse_than_either_representation_alone(corpus):
    """The fusion rule is a maximum, so it dominates by construction."""
    runtime = corpus["runtime"]
    for query in ("person", "bus", "a red truck", "a car"):
        vector = runtime.embed_texts([query])[0]
        crop_scores = corpus["crop"] @ vector
        frame_scores = np.where(corpus["has_frame"], corpus["frame"] @ vector, -np.inf)
        fused = np.fmax(crop_scores, frame_scores)
        assert np.all(fused >= crop_scores - 1e-6)
        assert np.all(fused >= np.where(corpus["has_frame"], frame_scores, -np.inf) - 1e-6)


def test_every_corpus_image_retrieves_itself_or_a_byte_identical_twin(corpus):
    """An exact image query must rank itself, or an image identical to it.

    Component 4 can emit distinct events whose crop images are byte-identical -
    the same object, cropped the same way, on two crossings of the same track.
    Their cosine is exactly 1.0, so which one `argmax` returns is a tie, not a
    ranking error. This corpus contains four such pairs, verified by SHA-256.
    """
    crop = corpus["crop"]
    events = corpus["events"]
    similarity = crop @ crop.T

    ties = 0
    for index in range(len(crop)):
        winner = int(np.argmax(similarity[index]))
        if winner == index:
            continue
        ties += 1
        assert similarity[index, winner] >= 0.999, (
            f"image {index} ranked an unrelated image first "
            f"(cosine {similarity[index, winner]:.4f})"
        )
        # The tie is genuine: the bytes really are the same.
        assert (
            events[index]["files"]["crop"]["sha256"]
            == events[winner]["files"]["crop"]["sha256"]
        ), f"image {index} lost to a different image at cosine 1.0"

    print(
        f"\nself-retrieval: {len(crop) - ties}/{len(crop)} rank themselves first; "
        f"{ties} lost only to a byte-identical duplicate"
    )
    assert np.allclose(np.diag(similarity), 1.0, atol=1e-3)


def test_byte_identical_images_produce_identical_vectors(corpus):
    """Determinism, checked against real duplicate inputs in the corpus."""
    by_digest: dict[str, list[int]] = defaultdict(list)
    for index, event in enumerate(corpus["events"]):
        by_digest[event["files"]["crop"]["sha256"]].append(index)
    duplicates = [group for group in by_digest.values() if len(group) > 1]
    if not duplicates:
        pytest.skip("this corpus contains no duplicate crops")
    for group in duplicates:
        first = corpus["crop"][group[0]]
        for other in group[1:]:
            assert np.allclose(first, corpus["crop"][other], atol=1e-6)
    print(f"\n{len(duplicates)} duplicate crop group(s) embedded identically")

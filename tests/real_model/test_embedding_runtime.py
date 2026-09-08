"""The real frozen checkpoint, loaded offline on CPU inside the built image.

Nothing here is mocked: this is the tier that proves the vectors Component 5
stores are the vectors this model actually produces.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.embeddings.manifest import EMBEDDING_DIM, MANIFEST
from app.embeddings.runtime import EmbeddingRuntime

MODEL_DIR = Path(os.environ.get("SEMANTIC_MODEL_DIR", "/opt/models/siglip"))

pytestmark = pytest.mark.skipif(
    not (MODEL_DIR / "model.safetensors").is_file(),
    reason=f"no model snapshot at {MODEL_DIR}",
)


@pytest.fixture(scope="module")
def runtime() -> EmbeddingRuntime:
    instance = EmbeddingRuntime(model_dir=MODEL_DIR, torch_threads=2)
    instance.load()
    return instance


def _swatch(colour: tuple[int, int, int], size=(224, 224)) -> Image.Image:
    return Image.new("RGB", size, colour)


def _shape(kind: str) -> Image.Image:
    image = Image.new("RGB", (224, 224), (245, 245, 245))
    draw = ImageDraw.Draw(image)
    if kind == "circle":
        draw.ellipse((40, 40, 184, 184), fill=(20, 20, 20))
    else:
        draw.rectangle((40, 40, 184, 184), fill=(20, 20, 20))
    return image


def test_every_manifest_asset_verifies_inside_the_image():
    verified = MANIFEST.verify(MODEL_DIR)
    assert set(verified) == {asset.name for asset in MANIFEST.assets}
    assert verified["model.safetensors"] == MANIFEST.weights_sha256


def test_the_model_loads_with_the_hub_forced_offline(runtime):
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
    assert runtime.ready
    assert runtime.model_id == MANIFEST.model_id


def test_text_and_image_towers_share_one_768_dimension_space(runtime):
    image_vectors = runtime.embed_images([_swatch((200, 30, 30))])
    text_vectors = runtime.embed_texts(["a red photograph"])
    assert image_vectors.shape == (1, EMBEDDING_DIM) == text_vectors.shape
    assert image_vectors.dtype == text_vectors.dtype == np.float32
    assert np.isfinite(image_vectors).all() and np.isfinite(text_vectors).all()
    for vectors in (image_vectors, text_vectors):
        assert abs(float(np.linalg.norm(vectors[0])) - 1.0) < 1e-3
    # A cosine between the two towers is defined, and is a similarity - never a
    # probability.
    cosine = float(image_vectors[0] @ text_vectors[0])
    assert -1.0 <= cosine <= 1.0


def test_repeated_inference_is_stable(runtime):
    image = _swatch((30, 90, 200))
    first = runtime.embed_images([image])
    second = runtime.embed_images([image])
    assert np.allclose(first, second, atol=1e-5)
    assert float(first[0] @ second[0]) > 0.9999

    text_a = runtime.embed_texts(["a blue photograph"])
    text_b = runtime.embed_texts(["a blue photograph"])
    assert np.allclose(text_a, text_b, atol=1e-5)


def test_batching_does_not_change_a_vector(runtime):
    images = [_swatch((200, 30, 30)), _swatch((30, 200, 30)), _shape("circle")]
    single = np.vstack([runtime.embed_images([image]) for image in images])
    batched = runtime.embed_images(images)
    assert np.allclose(single, batched, atol=1e-4)


def test_an_image_is_closer_to_itself_than_to_an_unrelated_fixture(runtime):
    red, green, circle, square = (
        _swatch((210, 20, 20)), _swatch((20, 190, 60)), _shape("circle"), _shape("square")
    )
    vectors = runtime.embed_images([red, green, circle, square])
    similarity = vectors @ vectors.T
    for index in range(len(vectors)):
        assert similarity[index, index] > 0.999
        others = np.delete(similarity[index], index)
        assert similarity[index, index] > others.max()


def test_colour_queries_rank_the_matching_swatch_first(runtime):
    swatches = {
        "red": _swatch((205, 25, 25)),
        "green": _swatch((25, 175, 55)),
        "blue": _swatch((30, 60, 200)),
        "yellow": _swatch((235, 220, 40)),
    }
    names = list(swatches)
    image_vectors = runtime.embed_images([swatches[name] for name in names])
    for name in names:
        query = runtime.embed_texts([f"a solid {name} colour image"])[0]
        ranked = names[int(np.argmax(image_vectors @ query))]
        assert ranked == name, f"'{name}' query ranked '{ranked}' first"


def test_text_tokenisation_uses_fixed_64_token_padding(runtime):
    short = runtime.embed_texts(["bus"])
    padded_equivalent = runtime.embed_texts(["bus", "a very long unrelated sentence " * 4])
    # Padding to a fixed length means a query's vector does not depend on what
    # else happened to be in the batch.
    assert np.allclose(short[0], padded_equivalent[0], atol=1e-4)


def test_an_empty_batch_returns_an_empty_matrix(runtime):
    assert runtime.embed_images([]).shape == (0, EMBEDDING_DIM)
    assert runtime.embed_texts([]).shape == (0, EMBEDDING_DIM)


def test_inference_writes_no_file_beneath_the_model_or_application_directory(runtime):
    watched = [MODEL_DIR, Path("/srv/semantic/app"), Path("/opt/venv/lib")]
    before = {
        path: {p: p.stat().st_mtime for p in path.rglob("*") if p.is_file()}
        for path in watched
        if path.is_dir()
    }
    runtime.embed_images([_swatch((10, 10, 10))])
    runtime.embed_texts(["a truck"])
    for path, snapshot in before.items():
        after = {p: p.stat().st_mtime for p in path.rglob("*") if p.is_file()}
        assert set(after) == set(snapshot), f"files changed under {path}"
        assert after == snapshot, f"a file was modified under {path}"


def test_measured_cpu_latency_is_reported(runtime):
    image = _swatch((120, 120, 120))
    runtime.embed_images([image])  # warm
    started = time.monotonic()
    runtime.embed_images([image])
    warm_single_ms = (time.monotonic() - started) * 1000

    batch = [_swatch((i * 30 % 255, 60, 200)) for i in range(4)]
    started = time.monotonic()
    runtime.embed_images(batch)
    batch_ms = (time.monotonic() - started) * 1000

    started = time.monotonic()
    runtime.embed_texts(["a person", "a bus", "a red car", "a truck"])
    text_ms = (time.monotonic() - started) * 1000

    print(
        f"\nreal-model latency: warm_image={warm_single_ms:.1f}ms "
        f"batch4={batch_ms:.1f}ms ({batch_ms / 4:.1f}ms/image) "
        f"text_x4={text_ms:.1f}ms"
    )
    assert warm_single_ms > 0 and batch_ms > 0


def test_degenerate_small_images_are_embedded_from_the_correct_axes(runtime):
    """Regression: the processor's channel inference is ambiguous for tiny crops.

    A 1x1 image raised ValueError and a 3-pixel-tall image was silently read as
    channels-first, producing a vector computed from transposed data. Component
    4 crops of distant objects really are this small, so the runtime states the
    channel layout explicitly.
    """
    tiny = Image.new("RGB", (1, 1), (10, 20, 30))
    vectors = runtime.embed_images([tiny])
    assert vectors.shape == (1, EMBEDDING_DIM)
    assert np.isfinite(vectors).all()

    # A 3-row image is the shape that used to be misread.
    thin = Image.new("RGB", (5, 3), (10, 20, 30))
    for x in range(5):
        thin.putpixel((x, 0), (250, 0, 0))
    thin_vector = runtime.embed_images([thin])
    assert thin_vector.shape == (1, EMBEDDING_DIM)
    assert abs(float(np.linalg.norm(thin_vector[0])) - 1.0) < 1e-3

    # Orientation is respected: the transpose of that image is a different
    # picture and must not produce the same vector.
    transposed = thin.transpose(Image.Transpose.ROTATE_90)
    other = runtime.embed_images([transposed])
    assert float(thin_vector[0] @ other[0]) < 0.999


def test_a_batch_of_mixed_sizes_embeds_every_row(runtime):
    images = [
        Image.new("RGB", (1, 1), (200, 10, 10)),
        Image.new("RGB", (7, 3), (10, 200, 10)),
        Image.new("RGB", (224, 224), (10, 10, 200)),
        Image.new("RGB", (640, 480), (200, 200, 10)),
    ]
    vectors = runtime.embed_images(images)
    assert vectors.shape == (4, EMBEDDING_DIM)
    assert np.isfinite(vectors).all()
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-3)

"""Static assertions over the dashboard and the isolation boundary."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "static"
APP = ROOT / "app"

HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")


def test_the_dashboard_has_no_build_step_and_no_external_asset():
    for asset in (HTML, JS, CSS):
        assert "http://" not in asset.replace("http://www.w3.org", "")
        assert "https://" not in asset
    assert "<script src=\"/static/app.js\"" in HTML
    assert "cdn" not in HTML.lower()


def test_external_strings_are_never_inserted_as_markup():
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert forbidden not in JS, f"{forbidden} must not be used"
    assert "eval(" not in JS
    assert "new Function" not in JS


def test_stale_responses_cannot_overwrite_a_newer_search():
    assert "AbortController" in JS
    assert "++state.generation" in JS
    assert "generation !== state.generation" in JS


def test_a_recording_url_is_opened_with_noopener():
    assert 'window.open(payload.recording.playback_url, "_blank", "noopener,noreferrer")' in JS


def test_every_control_has_a_label():
    ids = set(re.findall(r'<(?:input|select)[^>]*\bid="([^"]+)"', HTML))
    labelled = set(re.findall(r'<label[^>]*\bfor="([^"]+)"', HTML))
    wrapped = {"mode"}  # radio inputs are wrapped in their own <label>
    unlabelled = {
        control for control in ids - labelled
        if control not in wrapped and "image-preview" not in control
    }
    assert not unlabelled, f"controls without a label: {sorted(unlabelled)}"


def test_the_page_declares_a_mobile_viewport_and_a_skip_link():
    assert 'name="viewport"' in HTML
    assert 'class="skip"' in HTML
    assert "@media (max-width: 430px)" in CSS
    assert ":focus-visible" in CSS


def test_the_ui_states_required_by_the_plan_exist():
    for marker in ("status-search", "status-index", "status-upstream",
                   "message", "summary", "results", "drawer"):
        assert f'id="{marker}"' in HTML
    assert "Image unavailable" in JS  # broken-image placeholder
    assert "No indexed events match" in JS  # empty state
    assert "to index" in JS  # partial-index progress


def test_scores_are_never_presented_as_probabilities():
    lowered = (HTML + JS).lower()
    assert "probability" not in lowered
    assert "confidence that" not in lowered
    assert "cosine similarities" in HTML.lower()


def _code_only(path: Path) -> str:
    """Source with comments and docstrings removed.

    The boundary tests must judge what the service DOES, not what its
    documentation explains: the modules that enforce the isolation rules are
    exactly the ones that have to name the things they refuse to touch.
    """
    import io
    import tokenize

    if path.suffix != ".py":
        return path.read_text(encoding="utf-8")
    kept: list[str] = []
    previous_type = tokenize.INDENT
    with path.open("rb") as handle:
        for token in tokenize.tokenize(handle.readline):
            if token.type == tokenize.COMMENT:
                continue
            if token.type == tokenize.STRING and previous_type in (
                tokenize.INDENT, tokenize.NEWLINE, tokenize.NL, tokenize.ENCODING,
                tokenize.DEDENT,
            ):
                continue  # a docstring
            if token.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT):
                previous_type = token.type
            elif token.type in (tokenize.NL, tokenize.NEWLINE):
                previous_type = token.type
            kept.append(token.string)
    return " ".join(kept)


@pytest.mark.parametrize(
    "forbidden",
    ["rtsp://", "rtsps://", "webrtc", "mediamtx", "9997", "docker.sock",
     "/recordings", "gstreamer", "ultralytics", "faiss", "qdrant", "openai"],
)
def test_no_component_2_to_4_integration_exists_anywhere_in_the_app(forbidden):
    """Component 5 reaches Component 4's public HTTP API and nothing else."""
    offenders = []
    for path in APP.rglob("*"):
        if path.suffix not in {".py", ".js", ".html", ".css", ".sql"}:
            continue
        if forbidden in _code_only(path).lower():
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"'{forbidden}' appears in executable code in {offenders}"


def test_the_only_sqlite_use_is_component_5s_own_store():
    importers = [
        str(path.relative_to(ROOT))
        for path in APP.rglob("*.py")
        if re.search(r"^\s*import sqlite3", path.read_text(encoding="utf-8"), re.M)
    ]
    assert sorted(importers) == [
        "app/persistence/database.py",
        "app/persistence/repositories.py",
    ]


def test_the_outbound_http_surface_is_only_the_component4_adapter():
    """Only one module may build an outbound request."""
    callers = []
    for path in APP.rglob("*.py"):
        source = _code_only(path)
        if "httpx" in source and "build_request" in source or ".send(" in source:
            callers.append(str(path.relative_to(ROOT)))
    assert callers == ["app/integrations/component4.py"]


def test_no_module_imports_another_components_code():
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "from analytics" not in text
        assert "import analytics" not in text
        assert "vms" not in text.lower().replace("vms_camera_id", "").replace(
            "vms camera id", ""
        ) or "vms_camera" in text

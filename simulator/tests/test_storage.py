"""Source storage: uploads, generated filenames and path containment."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from app.services.source_storage import SourceError, SourceStorage, UploadTooLarge


class _Upload:
    """Minimal stand-in for Starlette's UploadFile."""

    def __init__(self, filename: str, data: bytes) -> None:
        self.filename = filename
        self._stream = io.BytesIO(data)

    async def read(self, size: int) -> bytes:
        return self._stream.read(size)


async def test_upload_is_staged_then_promoted_to_generated_name(storage: SourceStorage) -> None:
    staged, filename = await storage.stage_upload(_Upload("parking.mp4", b"data" * 100))
    assert staged.exists()
    assert filename == "parking.mp4"

    path, stored_filename = storage.promote(staged, "cam_1234", filename)
    assert not staged.exists()
    assert path.parent == storage.upload_dir
    assert stored_filename.startswith("cam_1234_")
    assert stored_filename.endswith(".mp4")
    # The client-supplied name is never used as the storage path.
    assert stored_filename != "parking.mp4"


async def test_upload_filename_is_not_used_as_a_path(storage: SourceStorage) -> None:
    staged, filename = await storage.stage_upload(_Upload("../../evil.mp4", b"xyz"))
    assert filename == "evil.mp4"
    _, stored = storage.promote(staged, "cam_1", filename)
    assert ".." not in stored
    assert (storage.upload_dir / stored).resolve().parent == storage.upload_dir.resolve()


@pytest.mark.parametrize("filename", ["notes.txt", "archive.zip", "noextension", "x.mp4.exe"])
async def test_unsupported_extensions_are_rejected(
    storage: SourceStorage, filename: str
) -> None:
    with pytest.raises(SourceError):
        await storage.stage_upload(_Upload(filename, b"data"))


async def test_oversized_upload_is_rejected_and_cleaned_up(storage: SourceStorage) -> None:
    storage.max_upload_bytes = 1024
    with pytest.raises(UploadTooLarge):
        await storage.stage_upload(_Upload("big.mp4", b"x" * 5000))
    assert list(storage.temp_dir.iterdir()) == []


async def test_empty_upload_is_rejected(storage: SourceStorage) -> None:
    with pytest.raises(SourceError):
        await storage.stage_upload(_Upload("empty.mp4", b""))
    assert list(storage.temp_dir.iterdir()) == []


def test_local_source_inside_mount_resolves(storage: SourceStorage) -> None:
    target = storage.local_root / "parking.mp4"
    target.write_bytes(b"video")
    assert storage.resolve_local("parking.mp4") == target.resolve()
    assert storage.resolve_local(str(target)) == target.resolve()

    nested = storage.local_root / "wing-a"
    nested.mkdir()
    (nested / "lobby.mp4").write_bytes(b"video")
    assert storage.resolve_local("wing-a/lobby.mp4").name == "lobby.mp4"


@pytest.mark.parametrize(
    "candidate",
    ["../../etc/passwd", "/etc/hosts", "..", "subdir/../../outside.mp4", ""],
)
def test_paths_outside_the_mount_are_rejected(storage: SourceStorage, candidate: str) -> None:
    with pytest.raises(SourceError):
        storage.resolve_local(candidate)


def test_symlink_escaping_the_mount_is_rejected(storage: SourceStorage, tmp_path: Path) -> None:
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"video")
    link = storage.local_root / "sneaky.mp4"
    link.symlink_to(outside)
    with pytest.raises(SourceError):
        storage.resolve_local("sneaky.mp4")


def test_missing_local_file_is_rejected(storage: SourceStorage) -> None:
    with pytest.raises(SourceError, match="No such file"):
        storage.resolve_local("nope.mp4")


def test_list_local_sources_only_lists_video_files(storage: SourceStorage) -> None:
    (storage.local_root / "a.mp4").write_bytes(b"v")
    (storage.local_root / "README.md").write_text("hi")
    sub = storage.local_root / "sub"
    sub.mkdir()
    (sub / "b.mkv").write_bytes(b"v")
    assert storage.list_local_sources() == ["a.mp4", "sub/b.mkv"]


def test_delete_stored_only_touches_the_upload_directory(
    storage: SourceStorage, tmp_path: Path
) -> None:
    stored = storage.upload_dir / "cam_1_abc.mp4"
    stored.write_bytes(b"v")
    storage.delete_stored("cam_1_abc.mp4")
    assert not stored.exists()

    outsider = tmp_path / "keep-me.mp4"
    outsider.write_bytes(b"v")
    storage.delete_stored("../../keep-me.mp4")
    storage.delete_stored(None)
    assert outsider.exists()

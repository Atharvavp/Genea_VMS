"""Storage for camera source videos.

Two kinds of source are supported:

* ``upload`` - the file arrives over HTTP, is streamed to a staging file under
  ``<data>/tmp`` and, once ffprobe accepts it, moved into ``<data>/videos``
  under a generated name. The client-supplied filename is never used as a path.
* ``local``  - the file already exists inside the read-only mounted source
  directory. Arbitrary host paths are rejected.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import BinaryIO, Protocol

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".m4v", ".webm"}
_CHUNK_SIZE = 1024 * 1024


class SourceError(Exception):
    """The provided source file is unusable (bad path, bad type, too large)."""


class UploadTooLarge(SourceError):
    pass


class _AsyncUpload(Protocol):
    filename: str | None

    async def read(self, size: int) -> bytes: ...


class SourceStorage:
    def __init__(
        self,
        upload_dir: Path,
        local_root: Path,
        temp_dir: Path,
        max_upload_bytes: int,
    ) -> None:
        self.upload_dir = Path(upload_dir)
        self.local_root = Path(local_root)
        self.temp_dir = Path(temp_dir)
        self.max_upload_bytes = max_upload_bytes

    def ensure_directories(self) -> None:
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    # --- uploads --------------------------------------------------------

    async def stage_upload(self, upload: _AsyncUpload) -> tuple[Path, str]:
        """Stream an upload to a staging file. Returns (staged path, filename)."""
        original_filename = Path(upload.filename or "upload").name
        extension = self._safe_extension(original_filename)
        self.ensure_directories()
        staged = self.temp_dir / f"staging_{uuid.uuid4().hex}{extension}"

        written = 0
        try:
            with staged.open("wb") as handle:
                while True:
                    chunk = await upload.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > self.max_upload_bytes:
                        raise UploadTooLarge(
                            "Uploaded file exceeds the maximum size of "
                            f"{self.max_upload_bytes} bytes."
                        )
                    handle.write(chunk)
        except BaseException:
            self.discard(staged)
            raise

        if written == 0:
            self.discard(staged)
            raise SourceError("The uploaded file is empty.")
        return staged, original_filename

    def stage_stream(self, stream: BinaryIO, original_filename: str) -> tuple[Path, str]:
        """Synchronous variant of :meth:`stage_upload` (used by tests/tools)."""
        filename = Path(original_filename or "upload").name
        extension = self._safe_extension(filename)
        self.ensure_directories()
        staged = self.temp_dir / f"staging_{uuid.uuid4().hex}{extension}"
        written = 0
        try:
            with staged.open("wb") as handle:
                while chunk := stream.read(_CHUNK_SIZE):
                    written += len(chunk)
                    if written > self.max_upload_bytes:
                        raise UploadTooLarge("Uploaded file exceeds the maximum size.")
                    handle.write(chunk)
        except BaseException:
            self.discard(staged)
            raise
        if written == 0:
            self.discard(staged)
            raise SourceError("The uploaded file is empty.")
        return staged, filename

    def promote(self, staged_path: Path, camera_id: str, original_filename: str) -> tuple[Path, str]:
        """Move a validated staging file into permanent storage."""
        extension = self._safe_extension(original_filename)
        stored_filename = f"{camera_id}_{uuid.uuid4().hex}{extension}"
        self.ensure_directories()
        destination = self.upload_dir / stored_filename
        shutil.move(str(staged_path), str(destination))
        return destination, stored_filename

    def discard(self, path: Path | None) -> None:
        """Remove a staging file, ignoring failures."""
        if path is None:
            return
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("staging_cleanup_failed path=%s error=%s", path, exc)

    def delete_stored(self, stored_filename: str | None) -> None:
        """Delete a camera-owned uploaded file. Never touches mounted sources."""
        if not stored_filename:
            return
        target = self.upload_dir / Path(stored_filename).name
        try:
            resolved = target.resolve()
            if resolved.parent != self.upload_dir.resolve():
                logger.warning("refusing_delete_outside_upload_dir file=%s", stored_filename)
                return
            resolved.unlink(missing_ok=True)
            logger.info("uploaded_source_deleted file=%s", stored_filename)
        except OSError as exc:
            logger.warning("uploaded_source_delete_failed file=%s error=%s", stored_filename, exc)

    # --- mounted local sources ------------------------------------------

    def resolve_local(self, local_path: str) -> Path:
        """Resolve a mounted-source path, rejecting anything outside the root."""
        candidate = (local_path or "").strip()
        if not candidate:
            raise SourceError("A local source path is required.")

        root = self.local_root.resolve()
        raw = Path(candidate)
        target = (raw if raw.is_absolute() else root / raw)
        try:
            resolved = target.resolve()
        except OSError as exc:
            raise SourceError("The local source path could not be resolved.") from exc

        if resolved != root and root not in resolved.parents:
            raise SourceError(
                "Local sources must live inside the mounted source directory "
                f"({self.local_root}). Arbitrary host paths are not allowed."
            )
        if not resolved.is_file():
            raise SourceError(
                f"No such file in the mounted source directory: '{candidate}'."
            )
        self._safe_extension(resolved.name)
        return resolved

    def list_local_sources(self) -> list[str]:
        """Relative names of usable files in the mounted source directory."""
        root = self.local_root
        if not root.is_dir():
            return []
        names: list[str] = []
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in ALLOWED_EXTENSIONS:
                names.append(str(path.relative_to(root)))
        return names

    # --- helpers --------------------------------------------------------

    @staticmethod
    def _safe_extension(filename: str) -> str:
        extension = os.path.splitext(filename)[1].lower()
        if extension not in ALLOWED_EXTENSIONS:
            raise SourceError(
                f"Unsupported file type '{extension or filename}'. Allowed: "
                + ", ".join(sorted(ALLOWED_EXTENSIONS))
            )
        return extension

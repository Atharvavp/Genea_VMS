"""Spawns and supervises one FFmpeg publisher process per camera."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable

from app.domain.models import utcnow

logger = logging.getLogger(__name__)


class ProcessError(Exception):
    """Base class for publisher process failures."""


class FFmpegUnavailable(ProcessError):
    """The ffmpeg binary is missing from the image/PATH."""


class AlreadyRunning(ProcessError):
    """A publisher is already registered for this camera."""


class StartupFailed(ProcessError):
    """FFmpeg exited during the startup grace period."""

    def __init__(self, returncode: int | None, stderr_tail: list[str]) -> None:
        super().__init__(format_stderr_tail(stderr_tail) or f"ffmpeg exited with code {returncode}")
        self.returncode = returncode
        self.stderr_tail = stderr_tail


@dataclass
class ProcessExit:
    """Reported to the owner when a publisher process ends."""

    camera_id: str
    returncode: int | None
    stderr_tail: list[str]
    requested: bool


@dataclass
class ManagedProcess:
    camera_id: str
    command: list[str]
    process: asyncio.subprocess.Process
    stderr_tail: deque[str]
    started_at: datetime = field(default_factory=utcnow)
    stop_requested: bool = False
    supervisor: asyncio.Task | None = None
    stderr_reader: asyncio.Task | None = None

    @property
    def pid(self) -> int:
        return self.process.pid


ExitCallback = Callable[[ProcessExit], Awaitable[None]]


class FFmpegProcessManager:
    """Owns the live FFmpeg children. Knows nothing about persistence."""

    def __init__(
        self,
        startup_grace_seconds: float = 2.0,
        stop_timeout_seconds: float = 5.0,
        stderr_tail_lines: int = 50,
    ) -> None:
        self._startup_grace = startup_grace_seconds
        self._stop_timeout = stop_timeout_seconds
        self._tail_lines = stderr_tail_lines
        self._processes: dict[str, ManagedProcess] = {}
        self._registry_lock = asyncio.Lock()
        self._on_exit: ExitCallback | None = None

    def set_exit_callback(self, callback: ExitCallback) -> None:
        self._on_exit = callback

    # --- queries --------------------------------------------------------

    def is_running(self, camera_id: str) -> bool:
        managed = self._processes.get(camera_id)
        return managed is not None and managed.process.returncode is None

    def get(self, camera_id: str) -> ManagedProcess | None:
        return self._processes.get(camera_id)

    def running_camera_ids(self) -> list[str]:
        return list(self._processes)

    # --- lifecycle ------------------------------------------------------

    async def start(self, camera_id: str, command: list[str]) -> ManagedProcess:
        """Spawn FFmpeg and confirm it survives the startup grace period."""
        async with self._registry_lock:
            if camera_id in self._processes:
                raise AlreadyRunning(f"A publisher is already running for {camera_id}.")
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as exc:
                raise FFmpegUnavailable(
                    f"'{command[0]}' was not found. Install FFmpeg in the simulator image."
                ) from exc
            managed = ManagedProcess(
                camera_id=camera_id,
                command=command,
                process=process,
                stderr_tail=deque(maxlen=self._tail_lines),
            )
            # Registered before the grace period so a concurrent start cannot
            # spawn a second publisher for the same camera.
            self._processes[camera_id] = managed

        managed.stderr_reader = asyncio.create_task(
            self._read_stderr(managed), name=f"ffmpeg-stderr-{camera_id}"
        )

        exited = await self._wait_for_early_exit(managed)
        if exited:
            await self._drain_stderr(managed)
            async with self._registry_lock:
                if self._processes.get(camera_id) is managed:
                    del self._processes[camera_id]
            tail = list(managed.stderr_tail)
            logger.warning(
                "ffmpeg_start_failed camera_id=%s returncode=%s stderr=%s",
                camera_id,
                managed.process.returncode,
                format_stderr_tail(tail),
            )
            raise StartupFailed(managed.process.returncode, tail)

        managed.started_at = utcnow()
        managed.supervisor = asyncio.create_task(
            self._supervise(managed), name=f"ffmpeg-supervisor-{camera_id}"
        )
        logger.info("ffmpeg_started camera_id=%s pid=%s", camera_id, managed.pid)
        return managed

    async def stop(self, camera_id: str) -> bool:
        """Terminate the publisher for a camera. Returns True if one was running."""
        managed = self._processes.get(camera_id)
        if managed is None:
            return False

        managed.stop_requested = True
        if managed.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                managed.process.terminate()
            try:
                await asyncio.wait_for(managed.process.wait(), timeout=self._stop_timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "ffmpeg_force_kill camera_id=%s pid=%s", camera_id, managed.pid
                )
                with contextlib.suppress(ProcessLookupError):
                    managed.process.kill()
                await managed.process.wait()

        if managed.supervisor is not None:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(
                    asyncio.shield(managed.supervisor), timeout=self._stop_timeout + 2
                )
        else:
            await self._drain_stderr(managed)

        async with self._registry_lock:
            if self._processes.get(camera_id) is managed:
                del self._processes[camera_id]

        logger.info(
            "ffmpeg_stopped camera_id=%s returncode=%s",
            camera_id,
            managed.process.returncode,
        )
        return True

    async def stop_all(self) -> None:
        """Stop every publisher; used on application shutdown."""
        camera_ids = list(self._processes)
        if not camera_ids:
            return
        logger.info("stopping_all_publishers count=%d", len(camera_ids))
        await asyncio.gather(
            *(self.stop(camera_id) for camera_id in camera_ids), return_exceptions=True
        )

    # --- internals ------------------------------------------------------

    async def _wait_for_early_exit(self, managed: ManagedProcess) -> bool:
        """True if the process died within the startup grace period."""
        try:
            await asyncio.wait_for(managed.process.wait(), timeout=self._startup_grace)
        except asyncio.TimeoutError:
            return False
        return True

    async def _read_stderr(self, managed: ManagedProcess) -> None:
        stream = managed.process.stderr
        if stream is None:  # pragma: no cover - defensive
            return
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    managed.stderr_tail.append(text)
        except (asyncio.CancelledError, ValueError):  # pragma: no cover - defensive
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("stderr_reader_error camera_id=%s error=%s", managed.camera_id, exc)

    async def _drain_stderr(self, managed: ManagedProcess) -> None:
        if managed.stderr_reader is None:
            return
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(managed.stderr_reader), timeout=2.0)

    async def _supervise(self, managed: ManagedProcess) -> None:
        returncode = await managed.process.wait()
        await self._drain_stderr(managed)
        async with self._registry_lock:
            if self._processes.get(managed.camera_id) is managed:
                del self._processes[managed.camera_id]

        logger.info(
            "ffmpeg_exit camera_id=%s returncode=%s requested=%s",
            managed.camera_id,
            returncode,
            managed.stop_requested,
        )
        if self._on_exit is None:
            return
        event = ProcessExit(
            camera_id=managed.camera_id,
            returncode=returncode,
            stderr_tail=list(managed.stderr_tail),
            requested=managed.stop_requested,
        )
        try:
            await self._on_exit(event)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(
                "process_exit_callback_failed camera_id=%s error=%s", managed.camera_id, exc
            )


def format_stderr_tail(lines: list[str], max_chars: int = 500) -> str:
    """Condense FFmpeg stderr into a short, user-facing message."""
    text = " | ".join(line.strip() for line in lines if line.strip())
    if len(text) > max_chars:
        text = "..." + text[-max_chars:]
    return text

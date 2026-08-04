"""Cross-process lock protecting one coherent processing configuration."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
from typing import BinaryIO, Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = PROJECT_ROOT / ".runtime" / "processor.lock"


class ProcessBusyError(RuntimeError):
    """Another processing or configuration write operation owns the lock."""


class ProcessLock:
    def __init__(self, path: Path = LOCK_PATH) -> None:
        self.path = Path(path)
        self._stream: BinaryIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(
                    stream.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
        except (OSError, BlockingIOError) as exc:
            stream.close()
            raise ProcessBusyError(
                "处理器正在运行，当前不能保存配置或启动第二个任务"
            ) from exc
        self._stream = stream

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
            self._stream = None

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def processor_is_running(path: Path = LOCK_PATH) -> bool:
    lock = ProcessLock(path)
    try:
        lock.acquire()
    except ProcessBusyError:
        return True
    else:
        lock.release()
        return False


@contextmanager
def processing_lock(path: Path = LOCK_PATH) -> Iterator[None]:
    with ProcessLock(path):
        yield


__all__ = [
    "LOCK_PATH",
    "ProcessBusyError",
    "ProcessLock",
    "processing_lock",
    "processor_is_running",
]

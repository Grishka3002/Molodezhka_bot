from __future__ import annotations

from pathlib import Path


class RuntimeLock:
    def __init__(self, path: Path, title: str) -> None:
        self.path = path
        self.title = title
        self.file = None

    def __enter__(self) -> "RuntimeLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0)
        try:
            self._lock()
        except OSError as error:
            self.file.close()
            self.file = None
            raise RuntimeError(
                f"{self.title} уже запущен. Закройте старый терминал или нажмите Ctrl+C в предыдущем запуске."
            ) from error
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.file is None:
            return
        try:
            self.file.seek(0)
            self._unlock()
        finally:
            self.file.close()
            self.file = None

    def _lock(self) -> None:
        if self.file is None:
            return
        try:
            import msvcrt

            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except ImportError:
            # The project runs on Windows, but keep a simple fallback for other systems.
            import fcntl

            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self) -> None:
        if self.file is None:
            return
        try:
            import msvcrt

            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        except ImportError:
            import fcntl

            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)

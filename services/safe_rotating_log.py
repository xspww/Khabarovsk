from __future__ import annotations

import logging
import os
import threading
import time
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Optional


class ProcessSafeRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler variant that tolerates Windows locks from sibling processes."""

    _process_locks: Dict[str, threading.Lock] = {}
    _process_locks_guard = threading.Lock()

    def __init__(self, filename: str, *args: Any, lock_timeout: float = 1.0, **kwargs: Any):
        kwargs["delay"] = True
        super().__init__(filename, *args, **kwargs)
        self._lock_timeout = max(0.05, float(lock_timeout or 1.0))
        self._lock_filename = self.baseFilename + ".lock"
        with self._process_locks_guard:
            self._process_lock = self._process_locks.setdefault(self._lock_filename, threading.Lock())

    def _acquire_interprocess_lock(self) -> Optional[Any]:
        lock_stream = None
        try:
            lock_stream = open(self._lock_filename, "a+b")
            if lock_stream.seek(0, os.SEEK_END) == 0:
                lock_stream.write(b"\0")
                lock_stream.flush()
            deadline = time.monotonic() + self._lock_timeout
            if os.name == "nt":
                import msvcrt

                while True:
                    try:
                        lock_stream.seek(0)
                        msvcrt.locking(lock_stream.fileno(), msvcrt.LK_NBLCK, 1)
                        return lock_stream
                    except OSError:
                        if time.monotonic() >= deadline:
                            lock_stream.close()
                            return None
                        time.sleep(0.02)

            import fcntl

            while True:
                try:
                    fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return lock_stream
                except OSError:
                    if time.monotonic() >= deadline:
                        lock_stream.close()
                        return None
                    time.sleep(0.02)
        except Exception:
            try:
                if lock_stream:
                    lock_stream.close()
            except Exception:
                pass
            return None

    def _release_interprocess_lock(self, lock_stream: Any) -> None:
        if not lock_stream:
            return
        try:
            if os.name == "nt":
                import msvcrt

                lock_stream.seek(0)
                msvcrt.locking(lock_stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            lock_stream.close()
        except Exception:
            pass

    def _close_stream(self) -> None:
        if self.stream:
            try:
                self.flush()
            except Exception:
                pass
            try:
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def _ensure_stream(self) -> None:
        if self.stream is None:
            self.stream = self._open()

    def emit(self, record: logging.LogRecord) -> None:
        lock_stream = None
        with self._process_lock:
            try:
                lock_stream = self._acquire_interprocess_lock()
                self._ensure_stream()
                if lock_stream is not None:
                    try:
                        if self.shouldRollover(record):
                            try:
                                self.doRollover()
                            except OSError:
                                self._close_stream()
                                self._ensure_stream()
                    except OSError:
                        self._close_stream()
                        self._ensure_stream()
                logging.FileHandler.emit(self, record)
            except Exception:
                self.handleError(record)
            finally:
                self._close_stream()
                self._release_interprocess_lock(lock_stream)

    def handleError(self, record: logging.LogRecord) -> None:
        return

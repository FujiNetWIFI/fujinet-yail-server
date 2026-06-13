"""Shared runtime state for the admin web UI.

A single module-level ServerStats instance collects connection, session,
and image-conversion information from the TCP server threads, and a ring
buffer logging handler keeps recent log lines in memory. Everything here
is advisory/observational: the YAIL wire protocol path never depends on it.
"""
import logging
import threading
import time
from collections import deque

LOG_BUFFER_SIZE = 500
RECENT_IMAGES_SIZE = 50


class RingBufferHandler(logging.Handler):
    """Keep the most recent formatted log records in memory."""

    def __init__(self, capacity: int = LOG_BUFFER_SIZE):
        super().__init__()
        self.records: deque[dict] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts": record.created,
                "level": record.levelname,
                "name": record.name,
                "message": record.getMessage(),
            }
            with self._lock:
                self.records.append(entry)
        except Exception:
            self.handleError(record)

    def tail(self, n: int = 200) -> list[dict]:
        with self._lock:
            return list(self.records)[-n:]


class ServerStats:
    """Thread-safe counters and session registry for the web UI."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.total_connections = 0
        self.counters = {
            "images_served": 0,
            "image_failures": 0,
            "searches": 0,
            "generations": 0,
            "generation_failures": 0,
        }
        self.sessions: dict[int, dict] = {}
        self.recent_images: deque[dict] = deque(maxlen=RECENT_IMAGES_SIZE)

    # ----- sessions -----------------------------------------------------

    def session_started(self, thread_id: int, address: str) -> None:
        with self._lock:
            self.total_connections += 1
            self.sessions[thread_id] = {
                "id": thread_id,
                "address": address,
                "connected_at": time.time(),
                "mode": None,
                "gfx_mode": None,
                "last_command": None,
                "last_activity": time.time(),
                "images_sent": 0,
            }

    def session_ended(self, thread_id: int) -> None:
        with self._lock:
            self.sessions.pop(thread_id, None)

    def session_update(self, thread_id: int, **fields) -> None:
        with self._lock:
            session = self.sessions.get(thread_id)
            if session is not None:
                session.update(fields)
                session["last_activity"] = time.time()

    # ----- counters and image events ------------------------------------

    def incr(self, counter: str, by: int = 1) -> None:
        with self._lock:
            if counter in self.counters:
                self.counters[counter] += by

    def image_event(self, thread_id: int, source: str, target: str,
                    gfx_mode: int, ok: bool, duration: float) -> None:
        with self._lock:
            self.counters["images_served" if ok else "image_failures"] += 1
            session = self.sessions.get(thread_id)
            if ok and session is not None:
                session["images_sent"] += 1
            self.recent_images.append({
                "ts": time.time(),
                "client": thread_id,
                "source": source,
                "target": target,
                "gfx_mode": gfx_mode,
                "ok": ok,
                "duration_ms": int(duration * 1000),
            })

    # ----- snapshot ------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "started_at": self.started_at,
                "uptime_seconds": int(time.time() - self.started_at),
                "total_connections": self.total_connections,
                "active_connections": len(self.sessions),
                "counters": dict(self.counters),
                "sessions": [dict(s) for s in self.sessions.values()],
                "recent_images": list(self.recent_images),
            }


STATS = ServerStats()
LOG_BUFFER = RingBufferHandler()


def install_log_buffer() -> None:
    """Attach the ring buffer to the root logger (idempotent)."""
    root = logging.getLogger()
    if LOG_BUFFER not in root.handlers:
        LOG_BUFFER.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(LOG_BUFFER)

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from core.cancellation import CancelCheck, OperationCancelled

_ACTIVE_STATUSES = {"running", "cancelling"}


class BackgroundJob:
    """Runs one operation at a time in a background thread, capturing log
    lines from the given loggers so a frontend can poll progress, with
    cooperative cancellation. Starting a job while one is already active is
    a no-op (matches "ignore a double-click on Refresh").
    """

    def __init__(self, logger_names: list[str]):
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {
            "status": "idle",
            "lines": [],
            "progress": {},
            "error": None,
        }
        self._cancel_event = threading.Event()
        self.result: Any = None

        handler = self._make_handler()
        for name in logger_names:
            logging.getLogger(name).addHandler(handler)

    def _make_handler(self) -> logging.Handler:
        lock = self._lock
        state = self._state

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                # A record carrying a "progress" extra updates that
                # playlist's entry in place instead of appending a line, so
                # per-page fetch updates render as a moving bar rather than
                # a growing wall of near-identical log lines.
                progress = getattr(record, "progress", None)
                with lock:
                    if progress is not None:
                        state["progress"][progress["id"]] = progress
                    else:
                        state["lines"].append(self.format(record))

        handler = _Handler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        return handler

    def start(self, target: Callable[[CancelCheck], Any]) -> bool:
        """target receives a cancel_check callable and returns the job's
        result. Returns False (and does nothing) if a job is already
        running/cancelling."""
        with self._lock:
            if self._state["status"] in _ACTIVE_STATUSES:
                return False
            self._state["status"] = "running"
            self._state["lines"] = []
            self._state["progress"] = {}
            self._state["error"] = None
        self._cancel_event.clear()
        self.result = None

        def run() -> None:
            try:
                self.result = target(self._cancel_event.is_set)
                with self._lock:
                    self._state["status"] = "done"
            except OperationCancelled:
                with self._lock:
                    self._state["status"] = "cancelled"
            except Exception as exc:
                logging.getLogger("background_job").exception("background job failed")
                with self._lock:
                    self._state["status"] = "error"
                    self._state["error"] = str(exc)

        threading.Thread(target=run, daemon=True).start()
        return True

    def cancel(self) -> None:
        with self._lock:
            if self._state["status"] == "running":
                self._state["status"] = "cancelling"
        self._cancel_event.set()

    def status(self) -> dict:
        with self._lock:
            state = dict(self._state)
            state["progress"] = dict(state["progress"])
            return state

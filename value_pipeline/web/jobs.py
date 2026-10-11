"""One background job at a time (a screen, review or watch), logged to a file.

Jobs cannot be cancelled: a half-finished review is worth nothing. Closing the
browser does not stop them.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path


class JobBusy(RuntimeError):
    """Another job is still running."""


class JobRunner:
    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self._lock = threading.Lock()
        self._current: dict | None = None
        self._mark_interrupted()

    def _mark_interrupted(self) -> None:
        """A job still 'running' in the index belonged to a server that was stopped mid-job."""
        if not self._index.exists():
            return
        jobs = self._read()
        for j in jobs:
            if j["status"] == "running":
                j.update(status="interrupted", message="the website was stopped while this job ran")
        self._index.write_text(json.dumps(jobs, indent=1), encoding="utf-8")

    @property
    def _index(self) -> Path:
        return self.log_dir / "jobs.json"

    def _read(self) -> list[dict]:
        return json.loads(self._index.read_text(encoding="utf-8")) if self._index.exists() else []

    def _write(self, job: dict) -> None:
        jobs = [j for j in self._read() if j["name"] != job["name"]]
        jobs.insert(0, job)
        self._index.write_text(json.dumps(jobs[:200], indent=1), encoding="utf-8")

    def current(self) -> dict | None:
        return dict(self._current) if self._current else None

    def history(self, limit: int = 20) -> list[dict]:
        return self._read()[:limit]

    def log_text(self, name: str) -> str | None:
        """A job's log by name; None for an unknown name or one that tries to leave the log folder."""
        if not name.startswith("job_") or "/" in name or ".." in name:
            return None
        f = self.log_dir / f"{name}.log"
        return f.read_text(encoding="utf-8", errors="replace") if f.exists() else None

    def start(self, kind: str, func: Callable[[], object]) -> dict:
        with self._lock:
            if self._current is not None:
                raise JobBusy(f"A {self._current['kind']} job is already running; wait for it to finish.")
            self.log_dir.mkdir(parents=True, exist_ok=True)
            name = f"job_{datetime.now():%Y%m%d_%H%M%S_%f}_{kind}"
            job = {"name": name, "kind": kind, "started": datetime.now().isoformat(timespec="seconds"),
                   "ended": None, "status": "running", "message": "", "log": f"{name}.log"}
            self._current = job
            self._write(job)
        threading.Thread(target=self._run, args=(job, func), daemon=True).start()
        return dict(job)

    def _run(self, job: dict, func: Callable[[], object]) -> None:
        handler = None
        try:
            with (self.log_dir / job["log"]).open("w", encoding="utf-8", buffering=1) as out:
                handler = logging.StreamHandler(out)
                handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
                logging.getLogger().addHandler(handler)
                try:
                    with contextlib.redirect_stdout(out):
                        func()
                    job["status"] = "done"
                except SystemExit as exc:      # the pipeline's refusals: cost cap, cancelled, nothing to do
                    job["status"], job["message"] = "failed", str(exc)
                    out.write(f"\nStopped: {exc}\n")
                except Exception as exc:  # noqa: BLE001 - any failure is reported on the page
                    job["status"], job["message"] = "failed", f"{type(exc).__name__}: {exc}"
                    out.write(traceback.format_exc())
        finally:
            if handler is not None:
                logging.getLogger().removeHandler(handler)
            job["ended"] = datetime.now().isoformat(timespec="seconds")
            with self._lock:
                self._write(job)
                self._current = None

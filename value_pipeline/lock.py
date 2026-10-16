"""One review at a time, across processes: the scheduled run and the website share the data folder.

A second review started while one runs would spend the agents' cost twice and
write into the same report folder. The lock is an ``flock`` on a file in the
data folder, so it is released by the OS when the holder exits, crashes or is
killed, and never goes stale.
"""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager

from value_pipeline.config import Paths


class Locked(RuntimeError):
    """Another process holds the lock."""


def _path(paths: Paths, name: str):
    paths.root.mkdir(parents=True, exist_ok=True)
    return paths.root / f".{name}.lock"


@contextmanager
def exclusive(paths: Paths, name: str) -> Iterator[None]:
    """Hold the named lock for the block; raise ``Locked`` at once if someone else has it."""
    with _path(paths, name).open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise Locked(f"another {name} is running") from exc
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def held(paths: Paths, name: str) -> bool:
    """Whether some process holds the named lock right now (a probe; it does not take the lock)."""
    try:
        with exclusive(paths, name):
            return False
    except Locked:
        return True

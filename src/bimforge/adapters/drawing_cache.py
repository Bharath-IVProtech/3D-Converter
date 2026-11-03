"""Remembers big drawings once read, so opening the same file again takes seconds instead of minutes.

An entry is matched by the file's path, size and modified time, so a changed file is read afresh. It
is only ever read from the user's own cache folder. Failing to read or write the cache is never an
error: the drawing is simply read from the file.
"""

from __future__ import annotations

import hashlib
import os
import pickle
import tempfile
from pathlib import Path

from bimforge.drawing.primitives import Drawing2D

READER_VERSION = 3  # raise when the reader changes what it produces, so old entries are ignored
MIN_PRIMITIVES = 100_000  # smaller drawings read quickly enough already
KEEP = 3  # entries are large, so only the most recent are kept


def _folder() -> Path:
    override = os.environ.get("BIMFORGE_CACHE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "BIMForge" / "drawing-cache"


def _entry(path: Path, variant: str) -> Path:
    stat = path.stat()
    key = f"{READER_VERSION}|{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{variant}"
    return _folder() / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.drawing"


def load(path: Path, variant: str = "") -> Drawing2D | None:
    try:
        entry = _entry(path, variant)
        if not entry.is_file():
            return None
        drawing = pickle.loads(entry.read_bytes())
        entry.touch()
        return drawing if isinstance(drawing, Drawing2D) else None
    except Exception:  # noqa: BLE001 - a damaged entry just means reading the file again
        return None


def save(path: Path, variant: str, drawing: Drawing2D) -> None:
    if len(drawing.primitives) < MIN_PRIMITIVES:
        return
    try:
        entry = _entry(path, variant)
        entry.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=entry.parent, suffix=".tmp", delete=False) as fh:
            pickle.dump(drawing, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(fh.name, entry)
        entries = sorted(entry.parent.glob("*.drawing"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in entries[KEEP:]:
            old.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - e.g. a full disk: the import itself has still worked
        return

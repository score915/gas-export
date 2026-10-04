"""Shared helpers: dates, config, safe filesystem names. No network imports."""

import datetime
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path

WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def local_today(now=None):
    """Local machine date at command start as yyyyMMdd."""
    day = (now or datetime.datetime.now()).date()
    return day.strftime("%Y%m%d")


def safe_name(value, taken=None):
    """Sanitize a path segment: strip traversal/invalid chars and Windows
    reserved names; if ``taken`` (a set of already-used lowercase names) is
    given, disambiguate case-insensitive collisions with ``_2``, ``_3``, ..."""
    name = unicodedata.normalize("NFKC", str(value or "untitled"))
    name = _INVALID_CHARS.sub("_", name)
    name = name.rstrip(". ").strip().lstrip(".") or "untitled"
    name = name[:120]
    if name.split(".")[0].upper() in WINDOWS_RESERVED:
        name = f"_{name}"
    if taken is not None:
        candidate, n = name, 2
        while candidate.lower() in taken:
            candidate = f"{name}_{n}"
            n += 1
        taken.add(candidate.lower())
        name = candidate
    return name


def load_config(path):
    """Load a JSON config file; returns {} when path is None."""
    if path is None:
        return {}
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"config file not found: {p}")
    with p.open(encoding="utf-8-sig") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"config root must be an object: {p}")
    return data


def resolve_path(value, base=None):
    """Resolve a config/CLI path relative to ``base`` (or cwd) unless absolute."""
    if value is None:
        return None
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = Path(base or os.getcwd()) / p
    return p


def write_text_atomic(path, text):
    """Replace one UTF-8 file only after its complete contents are written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                         dir=path.parent, prefix=".gas-export-",
                                         suffix=".tmp", delete=False) as fh:
            temporary = Path(fh.name)
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def write_json(path, data):
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

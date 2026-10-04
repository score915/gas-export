"""`gas-export export`: dump Apps Script project sources by Script ID.

Uses the Apps Script API (script.projects.readonly; projects.get and
projects.getContent). IDs come only from --script-id or an --ids JSON file
(schema compatible with `discover` output) -- there is no hardcoded folder
restriction. Output is never cleaned: same-day reruns may leave stale files.
"""

import datetime
import json
import re
from pathlib import Path

from .common import local_today, safe_name, write_json, write_text_atomic
from .errors import error_message, explain_error

_SCRIPT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_PROJECT_URL_ID_RE = re.compile(r"/projects/([A-Za-z0-9_-]+)")


def parse_script_id(value):
    """Normalize a JSON entry (string or object) to a Script ID."""
    if isinstance(value, str):
        text = value
    elif isinstance(value, dict):
        text = str(value.get("scriptId") or value.get("id") or value.get("url") or "")
    else:
        text = ""
    m = _PROJECT_URL_ID_RE.search(text)
    sid = m.group(1) if m else text.strip()
    if not _SCRIPT_ID_RE.match(sid):
        raise ValueError(f"invalid Script ID: {text!r}")
    return sid


def load_entries(source):
    """Load the --ids JSON array or build entries from a single --script-id.

    Returns a list of dicts with at least ``scriptId``, deduplicated in
    input order. Raises ValueError on malformed input.
    """
    if isinstance(source, (str, Path)):
        data = json.loads(Path(source).read_text(encoding="utf-8-sig"))
        if not isinstance(data, list):
            raise ValueError("--ids JSON root must be an array")
        raw = data
    else:  # iterable of raw entries (e.g. a single id)
        raw = list(source)
    entries, seen = [], set()
    for value in raw:
        sid = parse_script_id(value)
        if sid in seen:
            continue
        seen.add(sid)
        entries.append({**value, "scriptId": sid} if isinstance(value, dict)
                       else {"scriptId": sid})
    return entries


RESERVED_PROJECT_FILES = {"_project.json", "_content.json"}
DEFAULT_OUTPUT_ROOTS = {
    "shared_projects": "Apps Scrip共有済み",
    "shared_drive": "共有ドライブ",
    "my_drive": "マイドライブ",
}
_UNKNOWN_SHARED_DRIVE = "不明な共有ドライブ"


def project_dir_name(file_name, taken):
    """Sanitized project directory name, unique within its parent folder."""
    return safe_name(file_name, taken)


def normalized_output_roots(roots=None):
    """Output category labels with defaults; blank configured labels fail."""
    merged = dict(DEFAULT_OUTPUT_ROOTS)
    merged.update(roots or {})
    for key, value in merged.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"output root label {key} must be non-empty")
        merged[key] = value.strip()
    return merged


def _folder_path(entry):
    """Validated ancestor folder names from a discover manifest row."""
    path = entry.get("folderPath")
    if path is None:
        return [entry["folderName"]] if entry.get("folderName") else []
    if (not isinstance(path, list)
            or any(not isinstance(p, str) or not p.strip() for p in path)):
        raise ValueError("folderPath must be a list of non-empty names")
    return path


def project_parent_segments(entry, roots):
    """Location segments above the per-project directory.

    Shared-list rows use the configured Apps Script shared root. Folder rows
    use the configured My Drive/shared-drive roots, then the discovered
    ancestor folder path; the Apps Script title/file name is appended later.
    """
    is_folder_row = bool(entry.get("fileId") or entry.get("folderId")
                         or entry.get("folderName") or entry.get("folderPath"))
    if entry.get("source") == "shared" or not is_folder_row:
        return [safe_name(roots["shared_projects"])]

    drive_kind = entry.get("driveKind")
    if drive_kind is None:
        drive_kind = "shared" if (entry.get("driveId") or entry.get("driveName")) else "my"
    path = [safe_name(p) for p in _folder_path(entry)]
    if drive_kind == "shared":
        return [safe_name(roots["shared_drive"]),
                safe_name(entry.get("driveName") or _UNKNOWN_SHARED_DRIVE),
                *path]
    if drive_kind == "my":
        return [safe_name(roots["my_drive"]), *path]
    raise ValueError(f"unknown driveKind: {drive_kind!r}")


def write_script_id_marker(project_dir, script_id):
    """Create the empty marker file whose name is exactly the Script ID.

    An existing zero-byte marker is reused. A non-empty file or directory at
    that path is left untouched and reported as an error instead of being
    silently truncated/replaced.
    """
    marker = Path(project_dir) / script_id
    if marker.exists():
        if marker.is_file() and marker.stat().st_size == 0:
            return marker
        raise ValueError("Script ID marker path is already occupied")
    marker.touch()
    return marker


def stable_project_directory(parent, file_name, script_id, taken):
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    for directory in parent.iterdir():
        if directory.is_dir():
            meta_file = directory / "_project.json"
            try:
                metadata = json.loads(meta_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                metadata = {}
            if metadata.get("scriptId") == script_id or (directory / script_id).exists():
                markers = [p.name for p in directory.iterdir() if p.is_file() and _SCRIPT_ID_RE.fullmatch(p.name)]
                if any(marker != script_id for marker in markers):
                    raise ValueError("multiple ID markers in existing project directory")
                taken.add(directory.name.lower())
                return directory
    taken.update(p.name.lower() for p in parent.iterdir())
    return parent / project_dir_name(file_name, taken)


def unique_file_name(name, ext, used):
    """Full `name.ext` unique within a project dir (case-insensitive),
    never clashing with _project.json / _content.json."""
    stem = safe_name(name)
    candidate, n = f"{stem}{ext}", 2
    while candidate.lower() in used or candidate.lower() in RESERVED_PROJECT_FILES:
        candidate = f"{stem}_{n}{ext}"
        n += 1
    used.add(candidate.lower())
    return candidate


def extension_for(file_type):
    return {
        "SERVER_JS": ".gs",
        "HTML": ".html",
        "JSON": ".json",
    }.get(str(file_type or "").upper(), ".txt")


_COMMENT_RE = re.compile(r"/\*[\s\S]*?\*/|^\s*//.*$", re.M)


def _without_comments(source):
    return _COMMENT_RE.sub("", str(source or "")).strip()


def is_empty_starter_function(source):
    """True for an empty file or the default `function myFunction() {}` stub."""
    code = re.sub(r"\s+", "", _without_comments(source))
    return code == "" or re.fullmatch(r"functionmyFunction\(\)\{\};?", code) is not None


def has_substantive_project_content(files):
    """Port of the prototype's empty-project check (JSON config excluded)."""
    for f in files or []:
        ftype = str(f.get("type") or "").upper()
        if ftype == "JSON":
            continue
        if ftype == "SERVER_JS":
            if not is_empty_starter_function(f.get("source")):
                return True
        elif _without_comments(f.get("source")) != "":
            return True
    return False


def export_projects(script_api, entries, date_dir, log=None, output_roots=None, log_dir=None, include_empty=False):
    """Export each entry into ``date_dir`` (the <out>/<yyyyMMdd> folder).

    ``script_api`` is a googleapiclient Apps Script service or test double.
    Writes per-project dirs + _project.json/_content.json and a log-directory
    ``_dump-index.json``. Returns the summary dict.
    """
    log = log or (lambda m: print(m))
    roots = normalized_output_roots(output_roots)
    date_dir = Path(date_dir)
    date_dir.mkdir(parents=True, exist_ok=True)
    # Case-insensitive collision guards keyed by each parent directory, so
    # same-named files in different Drive folders can keep the same leaf.
    taken_dirs = {}

    success, skipped, failures = [], [], []
    for i, entry in enumerate(entries):
        sid = entry["scriptId"]
        label = entry.get("fileName") or sid
        try:
            metadata = script_api.projects().get(scriptId=sid).execute(num_retries=3)
            if entry.get("fileId") and metadata.get("parentId") and metadata["parentId"] != entry["fileId"]:
                raise ValueError("bound project parentId does not match the Drive fileId; rediscover the Script ID")
            content = script_api.projects().getContent(scriptId=sid).execute(num_retries=3)
            files = content.get("files") or []
            if not include_empty and not has_substantive_project_content(files):
                skipped.append({"scriptId": sid, "title": metadata.get("title"),
                                "fileName": entry.get("fileName"),
                                "reason": "empty-project"})
                log(f"[{i + 1}/{len(entries)}] {label}: 空プロジェクトのため除外")
                continue

            file_name = entry.get("fileName") or metadata.get("title") or sid
            parents = project_parent_segments(entry, roots)
            parent_key = tuple(p.lower() for p in parents)
            taken = taken_dirs.setdefault(parent_key, set())
            project_dir = stable_project_directory(date_dir.joinpath(*parents), file_name, sid, taken)
            project_dir.mkdir(parents=True, exist_ok=True)
            write_script_id_marker(project_dir, sid)

            source_drive_file = None
            if entry.get("fileId"):
                source_drive_file = {
                    "id": entry["fileId"], "name": file_name,
                    "mimeType": entry.get("mimeType"),
                    "folderId": entry.get("folderId"),
                    "folderName": entry.get("folderName"),
                    "folderPath": _folder_path(entry),
                    "driveKind": entry.get("driveKind"),
                    "driveId": entry.get("driveId"),
                    "driveName": entry.get("driveName"),
                }
            write_json(project_dir / "_project.json",
                       {**metadata, "sourceDriveFile": source_drive_file})
            write_json(project_dir / "_content.json", content)
            taken_files = {sid.lower()}
            for f in files:
                fname = unique_file_name(f.get("name"),
                                         extension_for(f.get("type")), taken_files)
                write_text_atomic(project_dir / fname, f.get("source") or "")
            success.append({"scriptId": sid, "title": metadata.get("title"),
                            "sourceDriveFile": source_drive_file,
                            "outputDirectory": str(project_dir),
                            "files": len(files)})
            log(f"[{i + 1}/{len(entries)}] {label}: {len(files)}ファイル")
        except Exception as exc:  # noqa: BLE001 - record and continue
            failures.append({"scriptId": sid, "fileName": entry.get("fileName"),
                             "error": str(exc), "cause": explain_error(exc)[0], "action": explain_error(exc)[1]})
            log(f"[{i + 1}/{len(entries)}] {label}: " + error_message(exc))

    summary = {
        "generatedAt": datetime.datetime.now().isoformat(timespec="seconds"),
        "date": date_dir.name,
        "inputCount": len(entries),
        "successCount": len(success),
        "skippedCount": len(skipped),
        "failureCount": len(failures),
        "success": success,
        "skipped": skipped,
        "failures": failures,
    }
    log_dir = Path(log_dir) if log_dir is not None else date_dir.parent / "logs" / date_dir.name
    write_json(log_dir / "_dump-index.json", summary)
    return summary


def cmd_export(args, log=None):
    """Entry for `gas-export export`. Returns process exit code."""
    log = log or (lambda m: print(m))
    # Date is fixed at command start -- before any OAuth/consent delay.
    date_dir = Path(args.out) / local_today()
    raw = [args.script_id] if args.script_id else None
    entries = load_entries(raw if raw else args.ids)
    log(f"保存対象のScript ID数: {len(entries)}")

    from . import auth as auth_mod  # lazy

    creds = auth_mod.load_credentials(
        args.credentials,
        args.token,
        [auth_mod.SCOPE_SCRIPT_PROJECTS_READONLY],
        account=getattr(args, "google_account", None),
    )
    script_api = auth_mod.build_service("script", "v1", creds)
    summary = export_projects(
        script_api, entries, date_dir, log=log,
        output_roots=getattr(args, "output_roots", None),
        log_dir=Path(getattr(args, "logs", Path(args.out).parent / "logs")) / date_dir.name,
        include_empty=getattr(args, "include_empty", False))
    log(f"保存{summary['successCount']}件 / "
        f"空プロジェクト除外{summary['skippedCount']}件 / 失敗{summary['failureCount']}件"
        f" -> {date_dir}")
    return 1 if summary["failureCount"] else 0

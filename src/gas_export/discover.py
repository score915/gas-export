"""`gas-export discover`: collect bound Apps Script IDs under Drive folders.

Two auth tracks (both sensitive):
  * Drive API v3 via OAuth (drive.readonly) to list direct-child
    Sheets/Docs/Slides/Forms and resolve folder/drive names for --folder.
  * A Playwright-controlled visible Chrome (persistent profile, or a
    Playwright storage_state JSON via --browser-state) that opens the
    Apps Script editor menu for each file and reads the script.google.com
    URL.

WARNING: the UI path is NOT guaranteed read-only. Opening the Apps Script
menu on a file without a bound script may create a blank bound project.
Do NOT run discovery on live data until verified on safe test files.
"""

import datetime
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse, urljoin

from .common import local_today, write_json
from .errors import error_message, explain_error
import unicodedata

SUPPORTED_MIME_TYPES = {
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.presentation",
    "application/vnd.google-apps.form",
}
FORM_MIME = "application/vnd.google-apps.form"

MUTATION_WARNING = (
    "注意: フォルダ探索はApps Scriptメニューを開きます。GASのないファイルに空のプロジェクトが作成される可能性があります。"
)

_PROJECT_RE = re.compile(r"/home/projects/([A-Za-z0-9_-]+)")
_DOC_RE = re.compile(r"/d/([A-Za-z0-9_-]+)/edit")
_FOLDER_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


def validate_folder_id(folder_id):
    """Reject anything that is not a plain Google Drive ID -- a folder ID is
    interpolated into the Drive query string, so it must never carry query
    syntax. Returns the ID unchanged when valid."""
    if not _FOLDER_ID_RE.fullmatch(str(folder_id or "")):
        raise ValueError(f"invalid Drive folder ID: {folder_id!r}")
    return folder_id


def script_id_from_url(url):
    """Script ID from a script.google.com URL, else None (allowlisted host)."""
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return None
    if parsed.hostname != "script.google.com":
        return None
    m = _PROJECT_RE.search(parsed.path) or _DOC_RE.search(parsed.path)
    return m.group(1) if m else None


FOLDER_MIME = "application/vnd.google-apps.folder"
_FOLDER_METADATA_FIELDS = "id,name,mimeType,parents,driveId"
_MAX_FOLDER_DEPTH = 50


def _folder_metadata(drive, folder_id):
    """Validated folder metadata; callers always get back the requested ID."""
    meta = (drive.files()
            .get(fileId=folder_id,
                 fields=_FOLDER_METADATA_FIELDS,
                 supportsAllDrives=True)
            .execute(num_retries=3))
    if meta.get("id") != folder_id:
        raise ValueError("folder metadata id mismatch")
    if meta.get("mimeType") != FOLDER_MIME:
        raise ValueError(f"not a folder: {meta.get('mimeType')!r}")
    name = (meta.get("name") or "").strip()
    if not name:
        raise ValueError("folder has no name")
    parents = meta.get("parents") or []
    if not isinstance(parents, list) or not all(
            isinstance(p, str) and validate_folder_id(p) for p in parents):
        raise ValueError("folder metadata has invalid parents")
    return {**meta, "name": name, "parents": parents}


def get_shared_drive_name(drive, drive_id):
    """Validated shared-drive display name.

    A shared drive's root FILE often has only the localized synthetic name
    ``ドライブ``/``Drive``; the real display name lives on the Drive resource
    and requires the drive.readonly scope for ``drives().get``.
    """
    drive_id = validate_folder_id(drive_id)
    meta = (drive.drives()
            .get(driveId=drive_id, fields="id,name")
            .execute(num_retries=3))
    if meta.get("id") != drive_id:
        raise ValueError("shared drive metadata id mismatch")
    name = (meta.get("name") or "").strip()
    if not name:
        raise ValueError("shared drive has no name")
    return name


def get_folder_location(drive, folder_id):
    """Resolve a folder's canonical name, ancestor path and drive location.

    Returns ``name`` (the requested folder), ``folderPath`` (ancestor names
    from top-level down through the requested folder, excluding the synthetic
    My Drive/shared-drive root), ``driveKind`` (``"shared"`` or ``"my"``), and
    for shared drives ``driveId``/``driveName``. Every metadata response is
    validated; ambiguous multi-parent or cyclic chains fail closed.
    """
    folder_id = validate_folder_id(folder_id)
    chain, seen, drive_id = [], set(), None
    current = folder_id
    for _ in range(_MAX_FOLDER_DEPTH):
        if current in seen:
            raise ValueError("folder parent chain contains a cycle")
        seen.add(current)
        meta = _folder_metadata(drive, current)
        chain.append(meta)
        drive_id = drive_id or meta.get("driveId")
        parents = meta["parents"]
        if not parents:
            break
        if len(parents) != 1:
            raise ValueError("folder has ambiguous multiple parents")
        current = parents[0]
    else:
        raise ValueError("folder parent chain is too deep")

    # chain is requested-folder -> ... -> drive root. The final item is the
    # synthetic root (localized "ドライブ"/"My Drive"), so its name is replaced
    # by the fixed location root + optional real shared-drive name.
    folder_path = [m["name"] for m in reversed(chain[:-1])]
    location = {"name": chain[0]["name"], "folderPath": folder_path}
    if drive_id:
        location.update({"driveKind": "shared", "driveId": drive_id,
                         "driveName": get_shared_drive_name(drive, drive_id)})
    else:
        location["driveKind"] = "my"
    return location


def get_folder_name(drive, folder_id):
    """Canonical Drive name for a folder ID (kept for narrow callers/tests)."""
    return get_folder_location(drive, folder_id)["name"]


def list_direct_files(drive, folder, page_size=1000):
    """Direct-child supported files of one Drive folder, following pagination.

    ``drive`` is a googleapiclient Drive service (or a test double exposing
    ``files().list(**kwargs).execute(num_retries=3)``).
    """
    folder_id = validate_folder_id(folder["id"])
    files = []
    page_token = None
    while True:
        response = (
            drive.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                pageSize=page_size,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                fields="nextPageToken,files(id,name,mimeType,webViewLink)",
            )
            .execute(num_retries=3)
        )
        files.extend(response.get("files") or [])
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    folder_path = folder.get("folderPath")
    if folder_path is None:
        folder_path = [folder["name"]]
    location = {
        "folderId": folder["id"],
        "folderName": folder["name"],
        "folderPath": list(folder_path),
        "driveKind": folder.get("driveKind")
        or ("shared" if folder.get("driveId") else "my"),
        "source": "folder",
    }
    if folder.get("driveId"):
        location["driveId"] = folder["driveId"]
    if folder.get("driveName"):
        location["driveName"] = folder["driveName"]
    return [
        {**f, **location}
        for f in files
        if f.get("mimeType") in SUPPORTED_MIME_TYPES
    ]


def _click_apps_script(page, mime_type, timeout_menu=30_000):
    """Open the Apps Script editor for the loaded file page.

    ``timeout_menu`` bounds the wait for the top-level menu
    (Forms "More" / other files' "Extensions") -- the caller gives the
    first candidate a much longer window so the user can complete an
    interactive sign-in + 2FA in the visible window. The Apps Script
    item wait stays at 10s. Raises if the menu entry never appears --
    caller records a failure (fail closed: never assume 'no bound
    script').
    """
    timeout_item = 10_000
    if mime_type == FORM_MIME:
        more = page.get_by_role("button", name=re.compile(r"その他|More", re.I)).first
        more.wait_for(state="visible", timeout=timeout_menu)
        more.click()
        item = (
            page.locator('[role="menuitem"]:visible')
            .filter(has_text=re.compile(r"スクリプト エディタ|Script editor|Apps Script", re.I))
            .first
        )
        item.wait_for(state="visible", timeout=timeout_item)
        item.click()
        return
    extensions = page.get_by_text(re.compile(r"^(拡張機能|Extensions)$", re.I)).first
    extensions.wait_for(state="visible", timeout=timeout_menu)
    extensions.click()
    item = (
        page.locator('[role="menuitem"]:visible')
        .filter(has_text=re.compile(r"Apps Script|スクリプト エディタ|Script editor", re.I))
        .first
    )
    item.wait_for(state="visible", timeout=timeout_item)
    item.click()


LOGIN_HOSTS = {"accounts.google.com", "accounts.google.co.jp"}

SHARED_URL = "https://script.google.com/home/shared"
SHARED_COUNT_RE = re.compile(r"(\d+)\s*個のプロジェクトを表示しています")
SHARED_ID_RE = re.compile(r"[A-Za-z0-9_-]{20,}")
SHARED_MENU_TIMEOUT_MS = 60_000
SHARED_MAX_SCROLLS = 100
FIRST_LOGIN_MENU_TIMEOUT_MS = 600_000


def _shared_summary_count(page, wait_ms):
    """Visible 'N個のプロジェクトを表示しています' count from the shared
    page. Raises (fail closed) if the text never appears or doesn't parse."""
    loc = page.get_by_text(SHARED_COUNT_RE).first
    loc.wait_for(state="visible", timeout=wait_ms)
    m = SHARED_COUNT_RE.search(loc.inner_text() or "")
    if not m:
        raise ValueError("shared project summary count not found")
    return int(m.group(1))


SHARED_ROW_JS = """() => Array.from(
  document.querySelectorAll('[data-script-id]')).map(row => ({
    id: row.getAttribute('data-script-id'),
    name: (row.querySelector('[aria-label*="Apps Script"]')?.textContent
        || row.querySelector('[jsname="DeDuO"]')?.textContent
        || '').trim().replace(/\\s+/g, ' ')}))"""


def _collect_shared_rows(page):
    """{scriptId: title} currently rendered in the shared list.

    Row-relative extraction: each row's OWN querySelector supplies the
    title, so unrelated aria-labeled nodes elsewhere in the DOM can
    never misalign a title to a wrong Script ID. Fails closed on any
    invalid ID, missing title, or duplicate ID with a conflicting
    title -- never silently skips.
    """
    rows = {}
    for item in page.evaluate(SHARED_ROW_JS):
        sid = (item.get("id") or "")
        name = (item.get("name") or "").strip()
        if not SHARED_ID_RE.fullmatch(sid) or not name:
            raise ValueError(
                "malformed shared row: ID or title missing/invalid "
                "(sanitized)")
        if sid in rows and rows[sid] != name:
            raise ValueError(
                "conflicting titles for one shared Script ID "
                "(sanitized)")
        rows[sid] = name
    return rows


def _scroll_shared(page):
    """Scroll the nearest scrollable ancestor of the shared rows (the
    probed row ancestor had scrollHeight>clientHeight). Falls back to
    window scroll. Caller then waits via page.wait_for_timeout."""
    page.evaluate(
        """() => {
          const els = document.querySelectorAll('[data-script-id]');
          if (els.length) {
            let a = els[els.length - 1];
            while (a && a.parentElement) {
              a = a.parentElement;
              if (a.scrollHeight > a.clientHeight + 5) {
                a.scrollTop = a.scrollHeight;
                return;
              }
            }
          }
          window.scrollTo(0, document.body.scrollHeight);
        }""")


def collect_shared_projects(page, menu_wait_ms=SHARED_MENU_TIMEOUT_MS,
                            log=None):
    """Scrape the 'shared with me' Apps Script list (READ-ONLY UI: one
    navigation to /home/shared, no project or editor clicks).

    Returns {scriptId: title}. Fails closed when the accumulated unique
    ID count does not equal the displayed summary count after bounded
    scrolling -- never reports partial results as complete.
    """
    log = log or (lambda m: print(m))
    page.goto(SHARED_URL, wait_until="domcontentloaded", timeout=60_000)
    wait_ms = (FIRST_LOGIN_MENU_TIMEOUT_MS if _is_login_url(page.url)
               else menu_wait_ms)
    expected = _shared_summary_count(page, wait_ms)
    log(f"  shared list displays {expected} project(s)")
    rows = {}
    for _ in range(SHARED_MAX_SCROLLS):
        rows.update(_collect_shared_rows(page))
        if len(rows) >= expected:
            break
        _scroll_shared(page)
        page.wait_for_timeout(500)
    if len(rows) != expected:
        raise RuntimeError(
            f"shared list count mismatch: displayed {expected}, "
            f"collected {len(rows)} unique IDs (sanitized)")
    return rows


LOGIN_INSTRUCTION = (
    "  必要なら、開いた取得用ChromeでGoogleログインと2段階認証を完了してください（初回は最大10分待機）。確認コードはChrome内で入力してください。"
)


def _is_login_url(url):
    try:
        return (urlparse(url or "").hostname or "") in LOGIN_HOSTS
    except ValueError:
        return False


def _host_category(url):
    """Bucket a tab URL for sanitized diagnostics -- never emits the URL,
    path, query, Script ID, file name or account info."""
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return "blank"
    if host in LOGIN_HOSTS:
        return "login"
    if host == "script.google.com":
        return "script"
    if host.endswith("google.com") or host.endswith("google.co.jp"):
        return "google"
    return "other"


class ProjectSelectionError(ValueError):
    def __init__(self, message, choices=None):
        super().__init__(message)
        self.choices = choices or []


def selection_link(page, expected_title):
    """Choose an existing project by exact normalized title; never a create URL."""
    links = page.locator(".selection a[href]").evaluate_all(
        "els => els.map(a => ({text: a.textContent.trim(), href: a.href}))")
    def normalized(value):
        return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value or "")).strip()
    valid = []
    for link in links:
        url = urljoin(page.url, link["href"])
        parsed = urlparse(url)
        if parsed.scheme == "https" and parsed.hostname == "script.google.com" and _DOC_RE.search(parsed.path):
            valid.append({**link, "href": url})
    override = urlparse(expected_title or "")
    if override.scheme == "https" and override.hostname == "script.google.com" and _DOC_RE.search(override.path):
        matches = [link for link in valid if _DOC_RE.search(urlparse(link["href"]).path).group(1) == _DOC_RE.search(override.path).group(1)]
    else:
        matches = [link for link in valid if normalized(link["text"]) == normalized(expected_title)]
    if len(matches) == 1:
        return matches[0]["href"]
    choices = [{"title": link["text"], "editorLink": urlparse(link["href"])._replace(query="", fragment="").geturl()} for link in valid]
    raise ProjectSelectionError("project-selection: 対象名に一致する既存プロジェクトが1件ではありません", choices=choices)


def _wait_for_script_page(context, source_page, baseline,
                          deadline_ms=30_000, sleep=None, expected_title=None):
    """Return {id, url} once a script.google.com page appears.

    Only pages opened AFTER ``baseline`` (the tab set recorded before the
    menu click) are considered -- a stale script tab from a previous
    candidate must never leak its Script ID into this row. The source page
    itself may also navigate to the editor. Timeout errors report only
    sanitized per-host-category tab counts for diagnosis.
    """
    import time

    deadline = time.monotonic() + deadline_ms / 1000
    selected_pages = set()
    while time.monotonic() < deadline:
        for page in [source_page, *[p for p in context.pages if p is not source_page]]:
            if page in baseline and page is not source_page:
                continue
            if urlparse(page.url).path.rstrip("/").endswith("/select"):
                page.wait_for_load_state("domcontentloaded")
                destination = selection_link(page, expected_title)
                page.goto(destination, wait_until="domcontentloaded", timeout=30_000)
                selected_pages.add(page)
            sid = script_id_from_url(page.url)
            if page in selected_pages and not _PROJECT_RE.search(urlparse(page.url).path):
                sid = None
            if sid:
                return {"id": sid, "url": urlparse(page.url)._replace(query="", fragment="").geturl()}
        # Default wait goes through page.wait_for_timeout: blocking
        # time.sleep stalls Playwright's sync event dispatch, so a newly
        # opened editor tab would never appear in context.pages (proven
        # by an offline reproducer). Injected sleep is for fast fakes.
        if sleep is not None:
            sleep(0.5)
        else:
            source_page.wait_for_timeout(500)
    seen = [source_page, *[p for p in context.pages
                           if p is not source_page and p not in baseline]]
    counts = {}
    for page in seen:
        cat = _host_category(getattr(page, "url", ""))
        counts[cat] = counts.get(cat, 0) + 1
    raise TimeoutError(
        f"Apps Script page did not open within {deadline_ms // 1000}s "
        f"(tabs seen: {len(seen)}; "
        + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) + ")")


def run_discovery(candidates, context, out_file, existing=None, log=None):
    """Visit each candidate in ``context``; save progress after every success.

    Returns (rows, failures). Any error -- including a missing Apps Script
    menu entry -- is a failure, never an implicit 'no script'.
    """
    log = log or (lambda m: print(m))
    rows = dict(existing or {})
    failures = []
    first_pending = True
    for i, f in enumerate(candidates):
        if f["id"] in rows:
            log(f"[{i + 1}/{len(candidates)}] ID取得済み: {f.get('name')}")
            continue
        is_first_attempt = first_pending
        first_pending = False
        log(f"[{i + 1}/{len(candidates)}] 確認中: {f.get('name')}")
        baseline = set(context.pages)
        source_page = context.new_page()
        stop = False
        try:
            source_page.goto(f["webViewLink"], wait_until="domcontentloaded", timeout=60_000)
            if is_first_attempt:
                # Give the visible window up to 10 minutes so the user can
                # complete an interactive sign-in + 2FA once; later
                # candidates reuse the signed-in profile at the normal
                # 30s menu wait.
                log(LOGIN_INSTRUCTION)
            _click_apps_script(source_page, f.get("mimeType"),
                               timeout_menu=(FIRST_LOGIN_MENU_TIMEOUT_MS
                                             if is_first_attempt else 30_000))
            # Script-editor tab wait is always 30s (user decision: no
            # extended first-candidate window here -- only the menu wait
            # is longer on the first candidate, for sign-in/2FA).
            script = _wait_for_script_page(context, source_page, baseline,
                                           deadline_ms=30_000, expected_title=f.get("projectTitle") or f.get("name"))
            row = {
                "fileId": f["id"],
                "fileName": f.get("name"),
                "mimeType": f.get("mimeType"),
                "folderId": f.get("folderId"),
                "folderName": f.get("folderName"),
                "scriptId": script["id"],
                "selectionOverride": f.get("projectTitle"),
                "url": script["url"],
            }
            for key in ("folderPath", "driveKind", "driveId", "driveName",
                        "source"):
                if f.get(key) is not None:
                    row[key] = f[key]
            rows[f["id"]] = row
            write_json(out_file, sorted(rows.values(),
                                        key=lambda r: (r.get("folderName") or "",
                                                       r.get("fileName") or "")))
            log(f"  Script ID: {script['id']}")
        except Exception as exc:  # noqa: BLE001 - record and continue
            auth_timeout = _is_login_url(getattr(source_page, "url", ""))
            failures.append({"fileId": f["id"], "fileName": f.get("name"),
                             "projectChoices": getattr(exc, "choices", []),
                             "kind": ("authentication-timeout" if auth_timeout
                                      else "failure"),
                             "error": str(exc), "cause": explain_error(exc)[0], "action": explain_error(exc)[1]})
            log("  " + error_message(exc))
            if auth_timeout or (is_first_attempt and not isinstance(exc, ProjectSelectionError)):
                # Stop instead of closing this page and opening the next
                # candidate -- a sign-in/2FA in progress would be reset by
                # the close/reopen cycle. Report and let the browser close.
                log("  stopping: sign-in required or first candidate timed "
                    "out; rerun after signing in so later candidates are "
                    "not reset")
                stop = True
        finally:
            # Close EVERY tab opened for this candidate (source page, any
            # script editor tab, strays) so no page can leak state or a
            # stale Script ID into the next candidate.
            for page in context.pages:
                if page in baseline:
                    continue
                try:
                    if not page.is_closed():
                        page.close()
                except Exception:
                    pass
        if stop:
            break
    write_json(out_file, sorted(rows.values(),
                                key=lambda r: (r.get("folderName") or "",
                                               r.get("fileName") or "")))
    return list(rows.values()), failures


def launch_browser(profile_dir=None, storage_state=None, headless=False):
    """Visible Chrome via Playwright (lazy import).

    ``profile_dir`` keeps a persistent signed-in profile under the user
    config dir; ``storage_state`` (e.g. temp/auth.json shape) seeds a fresh
    context instead. Requires locally installed Chrome + Playwright; Google
    sign-in may be blocked in some environments.
    """
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = None
    try:
        if profile_dir:
            Path(profile_dir).mkdir(parents=True, exist_ok=True)
            context = pw.chromium.launch_persistent_context(
                str(profile_dir), channel="chrome", headless=headless
            )
            return pw, None, context
        browser = pw.chromium.launch(channel="chrome", headless=headless)
        kwargs = {"storage_state": str(storage_state)} if storage_state else {}
        return pw, browser, browser.new_context(**kwargs)
    except BaseException:
        if browser is not None:
            browser.close()
        pw.stop()
        raise


def _read_id_rows(path, key="fileId"):
    """Rows of a same-day snapshot keyed by ``key``.

    Missing file -> {}. An EXISTING file that is unreadable, non-list,
    contains rows missing ``key``, or has duplicate keys raises
    ValueError so callers can fail closed instead of overwriting the
    last complete manifest.
    """
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"unreadable snapshot {path.name}") from exc
    if not isinstance(data, list):
        raise ValueError(f"non-list snapshot {path.name}")
    rows = {}
    for r in data:
        if not isinstance(r, dict) or not r.get(key):
            raise ValueError(f"snapshot row missing {key} in {path.name}")
        if r[key] in rows:
            raise ValueError(f"duplicate {key} in {path.name}")
        rows[r[key]] = r
    return rows


def cmd_discover(args, log=None):
    """Entry for `gas-export discover`. Returns process exit code."""
    log = log or (lambda m: print(m))
    want_folders = bool(args.folders)
    want_shared = bool(getattr(args, "shared", False))
    # The mutation warning applies to folder-mode UI menu navigation only;
    # a shared-only run is a read-only list scrape and never opens files.
    if want_folders:
        print(MUTATION_WARNING, file=sys.stderr)
    out_root = Path(getattr(args, "logs", args.out)) / local_today()
    out_root.mkdir(parents=True, exist_ok=True)
    out_file = out_root / "script-ids.json"            # merged, for export
    folder_snap = out_root / "folder-script-ids.json"  # private per-source
    shared_snap = out_root / "shared-script-ids.json"
    index_file = out_root / "_discovery-index.json"
    args.manifest_path = out_file

    failures = []

    # ---- folder phase: Drive listing needs OAuth only when folders given
    candidates = []
    if want_folders:
        from . import auth as auth_mod  # lazy

        creds = auth_mod.load_credentials(
            args.credentials,
            args.token,
            [auth_mod.SCOPE_DRIVE_READONLY],
            account=getattr(args, "google_account", None),
        )
        drive = auth_mod.build_service("drive", "v3", creds)
        for folder in args.folders:
            try:
                # Canonical Drive location wins over the CLI/config label:
                # real folder path and shared-drive name determine output.
                folder = {**folder,
                          **get_folder_location(drive, folder["id"])}
                files = list_direct_files(drive, folder)
            except Exception as exc:  # noqa: BLE001
                failures.append({"source": "folder",
                                 "folderId": folder["id"],
                                 "folderName": folder["name"],
                                 "error": str(exc), "cause": explain_error(exc)[0], "action": explain_error(exc)[1]})
                log(f"{folder['name']}: " + error_message(exc))
                continue
            log(f"{folder['name']}: {len(files)} 件の対象ファイル")
            candidates.extend(files)

    for candidate in candidates:
        candidate["projectTitle"] = getattr(args, "project_selections", {}).get(candidate["id"])

    # ---- folder resume: same-day folder snapshot, or a validated legacy
    # merged manifest whose EVERY row has fileId+folderId (bootstrap).
    existing = {}
    if want_folders:
        candidate_ids = {f["id"] for f in candidates}
        if folder_snap.is_file():
            try:
                existing = _read_id_rows(folder_snap, "fileId")
            except ValueError as exc:
                failures.append({"source": "folder-snapshot",
                                 "error": str(exc), "cause": explain_error(exc)[0], "action": explain_error(exc)[1]})
                log(f"folder snapshot invalid: {exc}")
        elif out_file.is_file():
            # Bootstrap: adopt the legacy merged manifest as the folder
            # snapshot ONLY when every row has fileId+folderId (validated,
            # no re-association). Invalid/unrelated shapes are left alone.
            try:
                raw = json.loads(out_file.read_text(encoding="utf-8"))
                if (isinstance(raw, list)
                        and all(isinstance(r, dict) and r.get("fileId")
                                and r.get("folderId") for r in raw)):
                    write_json(folder_snap, raw)
                    existing = _read_id_rows(folder_snap, "fileId")
                    log("bootstrapped legacy script-ids.json as folder snapshot")
            except (OSError, ValueError) as exc:
                log(f"legacy manifest not adopted as folder snapshot: {exc}")
        for fid in list(existing):
            if fid not in candidate_ids or existing[fid].get("selectionOverride") != getattr(args, "project_selections", {}).get(fid):
                del existing[fid]  # rows for removed files must not linger
        if existing:
            log(f"取得済みIDを再利用: {len(existing)}件")
        # Canonicalize resumed rows against CURRENT candidate metadata while
        # preserving the proven scriptId/url identity.
        by_id = {f["id"]: f for f in candidates}
        for file_id, row in existing.items():
            cand = by_id[file_id]
            row.update({
                "fileName": cand.get("name"),
                "mimeType": cand.get("mimeType"),
                "folderId": cand.get("folderId"),
                "folderName": cand.get("folderName"),
            })
            for key in ("folderPath", "driveKind", "driveId", "driveName",
                        "source"):
                if cand.get(key) is not None:
                    row[key] = cand[key]
                else:
                    row.pop(key, None)

    # ---- browser phases (folder UI navigation + read-only shared list)
    folder_rows = []
    shared_rows = {}
    need_browser = want_folders or want_shared
    pw = browser = context = None
    try:
        if need_browser:
            pw, browser, context = launch_browser(
                profile_dir=args.browser_profile,
                storage_state=args.browser_state,
            )
            # Node-parity keeper tab held until context.close().
            keeper_page = context.new_page()  # noqa: F841
            if want_folders:
                folder_rows, ui_failures = run_discovery(
                    candidates, context, folder_snap,
                    existing=existing, log=log)
                failures.extend(ui_failures)
            if want_shared:
                log("shared: scraping /home/shared (read-only UI)")
                shared_page = context.new_page()
                try:
                    shared_rows = collect_shared_projects(
                        shared_page, log=log)
                finally:
                    try:
                        shared_page.close()
                    except Exception:
                        pass
                write_json(shared_snap, [
                    {"scriptId": sid, "fileName": title,
                     "url": f"https://script.google.com/home/projects/{sid}/edit",
                     "source": "shared"}
                    for sid, title in sorted(shared_rows.items())])
    except Exception as exc:  # noqa: BLE001 - browser/login/scrape failure
        log("ブラウザ処理: " + error_message(exc))
        if want_shared and not shared_rows:
            failures.append({"source": "shared", "error": str(exc), "cause": explain_error(exc)[0], "action": explain_error(exc)[1]})
        else:
            failures.append({"stage": "browser", "error": str(exc), "cause": explain_error(exc)[0], "action": explain_error(exc)[1]})
    finally:
        try:
            if context is not None:
                context.close()
        finally:
            if browser is not None:
                browser.close()
            if pw is not None:
                pw.stop()

    # ---- merged manifest: the root script-ids.json is only written on a
    # COMPLETE run -- ANY phase failure (folder metadata/list/UI, shared
    # scrape, unreadable snapshot) leaves the last complete root
    # untouched rather than replacing it with a partial result.
    merged_rows = None
    if failures:
        log("失敗があるため、最後に成功したscript-ids.jsonは維持します。")
    else:
        try:
            merged = {}
            if want_shared:
                for row in _read_id_rows(shared_snap, "scriptId").values():
                    merged[row["scriptId"]] = row
            # folder records win on scriptId collision so the bound-file
            # path keeps the real Drive folder layout.
            if want_folders:
                for row in _read_id_rows(folder_snap, "fileId").values():
                    merged[row["scriptId"]] = row
            merged_rows = sorted(
                merged.values(),
                key=lambda r: (r.get("folderName") or "",
                               r.get("fileName") or ""))
            write_json(out_file, merged_rows)
        except ValueError as exc:
            failures.append({"stage": "merge", "error": str(exc), "cause": explain_error(exc)[0], "action": explain_error(exc)[1]})
            log(f"merge failed; root manifest preserved: {exc}")

    if merged_rows is None:
        # On failure report the count of the last complete root (None if
        # none exists/unreadable), never the partial run's count.
        try:
            prior = _read_id_rows(out_file, "scriptId")
            root_count = len(prior)
        except ValueError:
            root_count = None

    write_json(index_file, {
        "generatedAt": datetime.datetime.now().isoformat(timespec="seconds"),
        "date": out_root.name,
        "folderCount": len(args.folders),
        "candidateCount": len(candidates),
        "folderCollectedCount": len(folder_rows) if want_folders else None,
        "sharedRequested": want_shared,
        "sharedCollectedCount": len(shared_rows) if want_shared else None,
        "collectedCount": (len(merged_rows) if merged_rows is not None
                           else root_count),
        "failureCount": len(failures),
        "failures": failures,
        "output": str(out_file),
    })
    log(f"ID収集: 対象ファイル{len(candidates)}件 / 失敗{len(failures)}件。ID一覧: {out_file}")
    return 1 if failures else 0

"""Offline stdlib tests for gas_export. No network, no google/playwright deps."""

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gas_export import cli, common, discover, exporter  # noqa: E402

SID_A = "A" * 44
SID_B = "B" * 44
SID_BAD = "short"


class FakeRequest:
    def __init__(self, data):
        self._data = data

    def execute(self, num_retries=0):
        return self._data


class FakeDriveFiles:
    """pages: list responses per folder query; metadata: per-file get data."""

    def __init__(self, pages, metadata=None):
        self.pages = list(pages)
        self.metadata = metadata or {}
        self.calls = 0

    def list(self, **kwargs):
        self.calls += 1
        token = kwargs.get("pageToken")
        idx = 0 if token is None else int(token)
        return FakeRequest(self.pages[idx])

    def get(self, **kwargs):
        self.calls += 1
        self.last_get = kwargs
        fid = kwargs["fileId"]
        return FakeRequest(self.metadata.get(
            fid, {"id": fid, "name": "RealFolder", "parents": [],
                  "mimeType": "application/vnd.google-apps.folder"}))


class FakeDriveDrives:
    def __init__(self, names=None):
        self.names = names or {}
        self.calls = 0

    def get(self, **kwargs):
        self.calls += 1
        self.last_get = kwargs
        did = kwargs["driveId"]
        return FakeRequest({"id": did, "name": self.names[did]})


class FakeDrive:
    def __init__(self, pages, metadata=None, drive_names=None):
        self._files = FakeDriveFiles(pages, metadata)
        self._drives = FakeDriveDrives(drive_names)

    def files(self):
        return self._files

    def drives(self):
        return self._drives


class FakeProjects:
    def __init__(self, metadata, contents, errors=None):
        self.metadata, self.contents, self.errors = metadata, contents, errors or {}

    def get(self, scriptId):
        if scriptId in self.errors:
            raise RuntimeError(self.errors[scriptId])
        return FakeRequest(self.metadata[scriptId])

    def getContent(self, scriptId):
        if scriptId in self.errors:
            raise RuntimeError(self.errors[scriptId])
        return FakeRequest(self.contents[scriptId])


class FakeScriptApi:
    def __init__(self, metadata, contents, errors=None):
        self._projects = FakeProjects(metadata, contents, errors)

    def projects(self):
        return self._projects


def gs_file(name="Code", source="function main() { doWork(); }"):
    return {"name": name, "type": "SERVER_JS", "source": source}


class TestCommon(unittest.TestCase):
    def test_local_today_format(self):
        self.assertEqual(common.local_today(datetime(2026, 10, 4)), "20261004")
        self.assertRegex(common.local_today(), r"^\d{8}$")

    def test_safe_name_traversal_and_reserved(self):
        cleaned = common.safe_name("..\\..\\evil")
        self.assertNotIn("\\", cleaned)
        self.assertNotIn("/", cleaned)
        self.assertNotEqual(cleaned, "..")
        self.assertFalse(cleaned.startswith("."))
        self.assertNotIn("/", common.safe_name("a/b"))
        self.assertTrue(common.safe_name("CON").startswith("_"))
        self.assertTrue(common.safe_name("aux.txt").startswith("_") or
                        common.safe_name("aux.txt") != "aux.txt")
        self.assertEqual(common.safe_name("name..."), "name")

    def test_safe_name_case_insensitive_collision(self):
        taken = set()
        a = common.safe_name("Report", taken)
        b = common.safe_name("REPORT", taken)
        self.assertEqual(len({a.lower(), b.lower()}), 2)


class TestExportHelpers(unittest.TestCase):
    def test_parse_script_id(self):
        self.assertEqual(exporter.parse_script_id(SID_A), SID_A)
        self.assertEqual(exporter.parse_script_id({"scriptId": SID_B}), SID_B)
        self.assertEqual(
            exporter.parse_script_id({"url": f"https://script.google.com/home/projects/{SID_A}"}),
            SID_A)
        with self.assertRaises(ValueError):
            exporter.parse_script_id(SID_BAD)

    def test_load_entries_dedupe(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "ids.json"
            p.write_text(json.dumps([SID_A, {"scriptId": SID_A},
                                     {"scriptId": SID_B, "fileName": "f"}]),
                         encoding="utf-8")
            entries = exporter.load_entries(p)
        self.assertEqual([e["scriptId"] for e in entries], [SID_A, SID_B])
        self.assertEqual(entries[1]["fileName"], "f")

    def test_empty_starter(self):
        self.assertTrue(exporter.is_empty_starter_function(
            "// comment\nfunction myFunction() {\n}\n"))
        self.assertTrue(exporter.is_empty_starter_function(""))
        self.assertFalse(exporter.is_empty_starter_function("function f(){return 1}"))

    def test_substantive_content(self):
        self.assertFalse(exporter.has_substantive_project_content(
            [{"name": "Code", "type": "SERVER_JS", "source": "function myFunction() {}"},
             {"name": "appsscript", "type": "JSON", "source": "{}"}]))
        self.assertTrue(exporter.has_substantive_project_content(
            [{"name": "page", "type": "HTML", "source": "<b>x</b>"}]))


class TestExportProjects(unittest.TestCase):
    def test_layout_skip_marker_and_index(self):
        entries = [
            {"scriptId": SID_A, "fileName": "calc", "folderName": "team",
             "folderPath": ["parent", "team"], "fileId": "file1",
             "mimeType": "application/vnd.google-apps.spreadsheet",
             "driveKind": "shared", "driveId": "DRIVE1",
             "driveName": "サンプル共有ドライブ", "source": "folder"},
            {"scriptId": SID_B, "source": "shared", "fileName": "shared-p"},
            {"scriptId": "C" * 44},
        ]
        api = FakeScriptApi(
            {SID_A: {"title": "Proj A"}, SID_B: {"title": "B"}, "C" * 44: {"title": "C"}},
            {SID_A: {"files": [gs_file()]}, SID_B: {"files": []}, "C" * 44: {}},
            errors={"C" * 44: "get failed"},
        )
        with tempfile.TemporaryDirectory() as td:
            date_dir = Path(td) / "20261004"
            summary = exporter.export_projects(api, entries, date_dir, log=lambda m: None)
            proj = (date_dir / "共有ドライブ" / "サンプル共有ドライブ"
                    / "parent" / "team" / "calc")
            self.assertTrue((proj / "Code.gs").is_file())
            marker = proj / SID_A
            self.assertTrue(marker.is_file())
            self.assertEqual(marker.read_bytes(), b"")
            self.assertNotIn("__", proj.name)
            meta = json.loads((proj / "_project.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["sourceDriveFile"]["id"], "file1")
            self.assertEqual(meta["sourceDriveFile"]["folderPath"],
                             ["parent", "team"])
            self.assertTrue((proj / "_content.json").is_file())
            index = json.loads((Path(td) / "logs" / "20261004" / "_dump-index.json").read_text(encoding="utf-8"))
            self.assertEqual((index["successCount"], index["skippedCount"],
                              index["failureCount"]), (1, 1, 1))
            self.assertEqual(summary["failures"][0]["scriptId"], "C" * 44)

    def test_marker_path_occupied_fails_without_truncating(self):
        sid = "G" * 44
        api = FakeScriptApi({sid: {"title": "T"}},
                            {sid: {"files": [gs_file()]}})
        with tempfile.TemporaryDirectory() as td:
            proj = (Path(td) / "20261004" / "Apps Scrip共有済み" / "T")
            proj.mkdir(parents=True)
            occupied = proj / sid
            occupied.write_text("keep me", encoding="utf-8")
            summary = exporter.export_projects(
                api, [{"scriptId": sid}], Path(td) / "20261004",
                log=lambda m: None)
            self.assertEqual(summary["failureCount"], 1)
            self.assertEqual(occupied.read_text(encoding="utf-8"), "keep me")
            self.assertFalse((proj / "_content.json").exists())

    def test_same_title_allowed_in_different_locations(self):
        sid1, sid2 = "H" * 44, "I" * 44
        api = FakeScriptApi(
            {sid1: {"title": "T1"}, sid2: {"title": "T2"}},
            {sid1: {"files": [gs_file()]}, sid2: {"files": [gs_file()]}})
        entries = [
            {"scriptId": sid1, "fileName": "same", "fileId": "f1",
             "folderName": "a", "folderPath": ["a"], "driveKind": "my"},
            {"scriptId": sid2, "fileName": "same", "fileId": "f2",
             "folderName": "b", "folderPath": ["b"], "driveKind": "shared",
             "driveId": "D", "driveName": "drive-b"},
        ]
        with tempfile.TemporaryDirectory() as td:
            exporter.export_projects(api, entries, Path(td) / "20261004",
                                     log=lambda m: None)
            self.assertTrue((Path(td) / "20261004" / "マイドライブ" / "a"
                             / "same" / sid1).is_file())
            self.assertTrue((Path(td) / "20261004" / "共有ドライブ"
                             / "drive-b" / "b" / "same" / sid2).is_file())

    def test_source_file_named_like_marker_and_custom_roots(self):
        sid = "J" * 44
        api = FakeScriptApi(
            {sid: {"title": "T"}},
            {sid: {"files": [{"name": sid, "type": "SERVER_JS",
                              "source": "function x(){1}"}]}})
        with tempfile.TemporaryDirectory() as td:
            exporter.export_projects(
                api, [{"scriptId": sid, "fileName": "p", "source": "shared"}],
                Path(td) / "20261004", log=lambda m: None,
                output_roots={"shared_projects": "shared-root"})
            proj = Path(td) / "20261004" / "shared-root" / "p"
            self.assertEqual((proj / sid).read_bytes(), b"")
            self.assertIn("function x", (proj / f"{sid}.gs")
                          .read_text(encoding="utf-8"))

    def test_title_fallback_and_txt_extension(self):
        sid = "D" * 44
        api = FakeScriptApi(
            {sid: {"title": "My Proj"}},
            {sid: {"files": [{"name": "weird", "type": "UNKNOWN", "source": "x"}]}},
        )
        with tempfile.TemporaryDirectory() as td:
            exporter.export_projects(api, [{"scriptId": sid}], Path(td) / "20261004",
                                     log=lambda m: None)
            proj = (Path(td) / "20261004" / "Apps Scrip共有済み"
                    / "My Proj")
            self.assertTrue((proj / "weird.txt").is_file())
            self.assertTrue((proj / sid).is_file())

    def test_long_filename_does_not_embed_script_id(self):
        sid = "E" * 44
        long_name = "x" * 200
        api = FakeScriptApi({sid: {"title": "T"}}, {sid: {"files": [gs_file()]}})
        with tempfile.TemporaryDirectory() as td:
            exporter.export_projects(
                api, [{"scriptId": sid, "fileName": long_name}],
                Path(td) / "20261004", log=lambda m: None)
            proj = next((Path(td) / "20261004" / "Apps Scrip共有済み")
                        .iterdir())
            self.assertNotIn(sid, proj.name)
            self.assertLessEqual(len(proj.name), 120)
            self.assertEqual((proj / sid).read_bytes(), b"")

    def test_metadata_filenames_reserved_and_dup_stems(self):
        sid = "F" * 44
        files = [
            {"name": "_project", "type": "JSON", "source": "{}"},
            {"name": "Code", "type": "SERVER_JS", "source": "function a(){1}"},
            {"name": "code", "type": "SERVER_JS", "source": "function b(){2}"},
        ]
        api = FakeScriptApi({sid: {"title": "T"}}, {sid: {"files": files}})
        with tempfile.TemporaryDirectory() as td:
            exporter.export_projects(api, [{"scriptId": sid}],
                                     Path(td) / "20261004", log=lambda m: None)
            proj = next((Path(td) / "20261004" / "Apps Scrip共有済み")
                        .iterdir())
            names = {p.name for p in proj.iterdir()}
            self.assertIn("_project.json", names)          # metadata file intact
            meta = json.loads((proj / "_project.json").read_text(encoding="utf-8"))
            self.assertEqual(meta.get("title"), "T")       # not overwritten by source
            self.assertIn("_project_2.json", names)        # source file displaced
            self.assertEqual(len([n for n in names if n.lower().endswith(".gs")]), 2)


class TestDiscovery(unittest.TestCase):
    def test_list_direct_files_pagination_and_filter(self):
        pages = [
            {"files": [{"id": "1", "name": "a", "mimeType": discover.FORM_MIME,
                        "webViewLink": "u1"},
                       {"id": "2", "name": "skipme", "mimeType": "image/png",
                        "webViewLink": "u2"}],
             "nextPageToken": "1"},
            {"files": [{"id": "3", "name": "b",
                        "mimeType": "application/vnd.google-apps.spreadsheet",
                        "webViewLink": "u3"}]},
        ]
        drive = FakeDrive(pages)
        folder = {"id": "F", "name": "folder", "folderPath": ["p", "folder"],
                  "driveKind": "shared", "driveId": "D1",
                  "driveName": "shared-drive"}
        rows = discover.list_direct_files(drive, folder)
        self.assertEqual([r["id"] for r in rows], ["1", "3"])
        self.assertEqual(rows[0]["folderName"], "folder")
        self.assertEqual(rows[0]["folderPath"], ["p", "folder"])
        self.assertEqual(rows[0]["driveName"], "shared-drive")
        self.assertEqual(rows[0]["source"], "folder")
        self.assertEqual(drive._files.calls, 2)

    def test_get_folder_name_exact_args_and_fail_closed(self):
        drive = FakeDrive([{"files": []}])
        self.assertEqual(discover.get_folder_name(drive, "AbC_123-x"),
                         "RealFolder")
        self.assertEqual(drive._files.last_get,
                         {"fileId": "AbC_123-x",
                          "fields": "id,name,mimeType,parents,driveId",
                          "supportsAllDrives": True})
        with self.assertRaises(ValueError):
            discover.get_folder_name(drive, "bad'id")

        class BadFiles(FakeDriveFiles):
            def get(self, **kwargs):
                return FakeRequest(self.err)

        for meta in ({"id": "OTHER", "name": "n",
                      "mimeType": "application/vnd.google-apps.folder"},
                     {"id": "F", "name": "n", "mimeType": "image/png"},
                     {"id": "F", "name": "  ",
                      "mimeType": "application/vnd.google-apps.folder"}):
            bad = FakeDrive([])
            bad._files = BadFiles([])
            bad._files.err = meta
            with self.assertRaises(ValueError):
                discover.get_folder_name(bad, "F")

    def test_folder_location_shared_drive_hierarchy(self):
        metadata = {
            "F": {"id": "F", "name": "サンプル対象フォルダ", "parents": ["ROOT"],
                  "driveId": "D1",
                  "mimeType": "application/vnd.google-apps.folder"},
            "ROOT": {"id": "ROOT", "name": "ドライブ", "parents": [],
                     "driveId": "D1",
                     "mimeType": "application/vnd.google-apps.folder"},
        }
        drive = FakeDrive([], metadata=metadata,
                          drive_names={"D1": "サンプル共有ドライブ"})
        loc = discover.get_folder_location(drive, "F")
        self.assertEqual(loc["name"], "サンプル対象フォルダ")
        self.assertEqual(loc["folderPath"], ["サンプル対象フォルダ"])
        self.assertEqual(loc["driveKind"], "shared")
        self.assertEqual(loc["driveName"], "サンプル共有ドライブ")
        self.assertEqual(drive._drives.last_get,
                         {"driveId": "D1", "fields": "id,name"})

    def test_folder_location_my_drive_nested_path(self):
        folder = "application/vnd.google-apps.folder"
        metadata = {
            "F": {"id": "F", "name": "target", "parents": ["P"],
                  "mimeType": folder},
            "P": {"id": "P", "name": "parent", "parents": ["R"],
                  "mimeType": folder},
            "R": {"id": "R", "name": "マイドライブ", "parents": [],
                  "mimeType": folder},
        }
        loc = discover.get_folder_location(FakeDrive([], metadata=metadata), "F")
        self.assertEqual(loc["folderPath"], ["parent", "target"])
        self.assertEqual(loc["driveKind"], "my")
        self.assertNotIn("driveId", loc)

    def test_folder_location_ambiguous_parents_fail_closed(self):
        metadata = {"F": {"id": "F", "name": "target",
                          "parents": ["P1", "P2"],
                          "mimeType": "application/vnd.google-apps.folder"}}
        with self.assertRaises(ValueError):
            discover.get_folder_location(FakeDrive([], metadata=metadata), "F")

    def test_folder_id_validation_before_query(self):
        """Injection-shaped or empty folder IDs are rejected BEFORE any
        Drive call; a valid ID produces the exact lead-specified query."""
        drive = FakeDrive([{"files": []}])
        for bad in ["abc' or trashed = false", "", "id with space",
                    "id;drop", "id\"x"]:
            with self.assertRaises(ValueError):
                discover.list_direct_files(drive, {"id": bad, "name": "n"})
        self.assertEqual(drive._files.calls, 0)

        captured = {}

        class RecFiles(FakeDriveFiles):
            def list(self, **kwargs):
                captured.update(kwargs)
                return super().list(**kwargs)

        rec = FakeDrive([{"files": []}])
        rec._files = RecFiles(rec._files.pages)
        discover.list_direct_files(rec, {"id": "AbC_123-x", "name": "n"})
        self.assertEqual(captured["q"],
                         "'AbC_123-x' in parents and trashed = false")

    def test_script_id_from_url_allowlist(self):
        self.assertEqual(
            discover.script_id_from_url(f"https://script.google.com/home/projects/{SID_A}"),
            SID_A)
        self.assertEqual(
            discover.script_id_from_url(f"https://script.google.com/d/{SID_B}/edit"),
            SID_B)
        self.assertIsNone(discover.script_id_from_url("https://evil.example.com/x"))
        self.assertIsNone(discover.script_id_from_url("not a url"))

    def test_run_discovery_success_and_fail_closed(self):
        class FakePage:
            def __init__(self, context):
                self.context = context
                self.url = "about:blank"
                self._closed = False

            def goto(self, url, **kw):
                self.url = url
                if "fail" in url:
                    raise TimeoutError("menu missing")
                self.context.script_url = (
                    f"https://script.google.com/home/projects/{SID_A}")
                self.url = self.context.script_url

            def is_closed(self):
                return self._closed

            def close(self):
                self._closed = True

        class FakeContext:
            def __init__(self):
                self.script_url = None
                self._pages = []

            def new_page(self):
                p = FakePage(self)
                self._pages.append(p)
                return p

            @property
            def pages(self):
                return self._pages

        candidates = [
            {"id": "ok", "name": "f1", "mimeType": discover.FORM_MIME,
             "webViewLink": "https://docs.google.com/ok",
             "folderId": "F", "folderName": "fold"},
            {"id": "bad", "name": "f2", "mimeType": discover.FORM_MIME,
             "webViewLink": "https://docs.google.com/fail",
             "folderId": "F", "folderName": "fold"},
        ]
        # stub out the UI click + wait so no Playwright is needed
        orig_click, orig_wait = discover._click_apps_script, discover._wait_for_script_page
        discover._click_apps_script = lambda page, mime, **kw: None
        discover._wait_for_script_page = lambda ctx, page, baseline, **kw: {
            "id": discover.script_id_from_url(page.url),
            "url": page.url} if discover.script_id_from_url(page.url) else (_ for _ in ()).throw(
                TimeoutError("no script page"))
        try:
            with tempfile.TemporaryDirectory() as td:
                out = Path(td) / "script-ids.json"
                rows, failures = discover.run_discovery(
                    candidates, FakeContext(), out, log=lambda m: None)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["scriptId"], SID_A)
                self.assertEqual(rows[0]["fileId"], "ok")
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0]["fileId"], "bad")
                saved = json.loads(out.read_text(encoding="utf-8"))
                self.assertEqual(saved[0]["fileId"], "ok")
        finally:
            discover._click_apps_script, discover._wait_for_script_page = orig_click, orig_wait

    def test_wait_ignores_stale_script_tab(self):
        """A script tab left open from a previous candidate must not leak
        its ID into the next candidate."""
        SID_OLD = "O" * 44
        SID_NEW = "N" * 44

        class P:
            def __init__(self, url):
                self.url = url

        stale = P(f"https://script.google.com/home/projects/{SID_OLD}")
        fresh = P(f"https://script.google.com/home/projects/{SID_NEW}")

        class Ctx:
            def __init__(self):
                self._pages = [stale, fresh]

            @property
            def pages(self):
                return self._pages

        src = P("https://docs.google.com/doc")
        ctx = Ctx()
        # baseline captured before the click contains only the stale tab
        result = discover._wait_for_script_page(ctx, src, {stale},
                                                sleep=lambda s: None)
        self.assertEqual(result["id"], SID_NEW)
        # source-page navigation is also accepted
        src2 = P(f"https://script.google.com/home/projects/{SID_OLD}")
        ctx._pages = [src2]
        self.assertEqual(
            discover._wait_for_script_page(ctx, src2, set(),
                                           sleep=lambda s: None)["id"], SID_OLD)

    def test_closes_all_tabs_opened_for_candidate(self):
        opened = []

        class P:
            def __init__(self, url="about:blank"):
                self.url = url
                self.closed = False

            def goto(self, url, **kw):
                self.url = url

            def is_closed(self):
                return self.closed

            def close(self):
                self.closed = True

        class Ctx:
            def __init__(self):
                self.keeper = P()
                self._pages = [self.keeper]

            def new_page(self):
                p = P()
                self._pages.append(p)
                opened.append(p)
                # simulate the editor opening in a second new tab
                t = P(f"https://script.google.com/home/projects/{SID_A}")
                self._pages.append(t)
                opened.append(t)
                return p

            @property
            def pages(self):
                return self._pages

        orig_click, orig_wait = discover._click_apps_script, discover._wait_for_script_page
        discover._click_apps_script = lambda page, mime, **kw: None
        discover._wait_for_script_page = lambda ctx, page, baseline, **kw: {
            "id": SID_A, "url": "u"}
        try:
            with tempfile.TemporaryDirectory() as td:
                ctx = Ctx()
                rows, failures = discover.run_discovery(
                    [{"id": "f1", "name": "n",
                      "webViewLink": "https://docs.google.com/x",
                      "mimeType": "application/vnd.google-apps.document",
                      "folderId": "F", "folderName": "fold"}],
                    ctx, Path(td) / "ids.json", log=lambda m: None)
                self.assertFalse(failures)
                self.assertTrue(all(p.closed for p in opened))
                self.assertFalse(ctx.keeper.closed)  # pre-existing tabs untouched
        finally:
            discover._click_apps_script, discover._wait_for_script_page = orig_click, orig_wait


    def test_first_candidate_gets_10min_others_30s(self):
        """First non-cached candidate: timeout_menu=600000 for interactive
        sign-in + 2FA; subsequent candidates use the normal 30s wait."""
        calls = []

        class P:
            def __init__(self):
                self.url = "about:blank"

            def goto(self, url, **kw):
                self.url = url

            def is_closed(self):
                return True

            def close(self):
                pass

        class Ctx:
            @property
            def pages(self):
                return []

            def new_page(self):
                return P()

        cands = [{"id": "a", "name": "f1", "webViewLink": "https://x/1"},
                 {"id": "b", "name": "f2", "webViewLink": "https://x/2"},
                 {"id": "c", "name": "f3", "webViewLink": "https://x/3"}]
        orig_click, orig_wait = (discover._click_apps_script,
                                 discover._wait_for_script_page)
        discover._click_apps_script = (
            lambda page, mime, **kw: calls.append(kw.get("timeout_menu")))
        discover._wait_for_script_page = lambda ctx, page, baseline, **kw: {
            "id": SID_A, "url": "u"}
        try:
            with tempfile.TemporaryDirectory() as td:
                rows, failures = discover.run_discovery(
                    cands, Ctx(), Path(td) / "ids.json", log=lambda m: None)
                self.assertFalse(failures)
                self.assertEqual(len(rows), 3)
                self.assertEqual(calls, [600_000, 30_000, 30_000])
        finally:
            (discover._click_apps_script,
             discover._wait_for_script_page) = orig_click, orig_wait

    def test_login_timeout_stops_before_second_candidate(self):
        """accounts.google.com failure on candidate 1: classified
        authentication-timeout and the loop stops -- candidate 2's page
        is never opened (no 2FA reset cycle)."""
        opened = []

        class P:
            def __init__(self, url="about:blank"):
                self.url = url
                self.closed = False

            def goto(self, url, **kw):
                self.url = "https://accounts.google.com/signin/v2/x"

            def is_closed(self):
                return self.closed

            def close(self):
                self.closed = True

        class Ctx:
            def __init__(self):
                self._pages = []

            @property
            def pages(self):
                return self._pages

            def new_page(self):
                p = P()
                self._pages.append(p)
                opened.append(p)
                return p

        cands = [{"id": "a", "webViewLink": "https://docs/1"},
                 {"id": "b", "webViewLink": "https://docs/2"}]
        orig_click = discover._click_apps_script
        discover._click_apps_script = (
            lambda page, mime, **kw: (_ for _ in ()).throw(
                TimeoutError("menu timeout")))
        try:
            with tempfile.TemporaryDirectory() as td:
                rows, failures = discover.run_discovery(
                    cands, Ctx(), Path(td) / "ids.json", log=lambda m: None)
                self.assertEqual(len(opened), 1)  # candidate 2 never opened
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0]["kind"], "authentication-timeout")
        finally:
            discover._click_apps_script = orig_click

    def test_first_candidate_nonlogin_timeout_also_stops(self):
        """Even without a login URL, a failed FIRST candidate stops the
        loop (user is not trapped in the close/reopen reset cycle);
        failures on later candidates do not stop the loop."""
        opened = []

        class P:
            def __init__(self):
                self.url = "https://docs.google.com/doc"

            def goto(self, url, **kw):
                self.url = url

            def is_closed(self):
                return False

            def close(self):
                pass

        class Ctx:
            @property
            def pages(self):
                return []

            def new_page(self):
                p = P()
                opened.append(p)
                return p

        cands = [{"id": "a", "webViewLink": "https://docs/1"},
                 {"id": "b", "webViewLink": "https://docs/2"}]
        orig_click = discover._click_apps_script
        discover._click_apps_script = (
            lambda page, mime, **kw: (_ for _ in ()).throw(
                TimeoutError("menu timeout")))
        try:
            with tempfile.TemporaryDirectory() as td:
                rows, failures = discover.run_discovery(
                    cands, Ctx(), Path(td) / "ids.json", log=lambda m: None)
                self.assertEqual(len(opened), 1)
                self.assertEqual(failures[0]["kind"], "failure")
        finally:
            discover._click_apps_script = orig_click


    def test_script_wait_always_30s(self):
        """Script-page wait is uniformly 30_000 ms for every candidate
        (user decision; the long first-candidate window applies only to
        the menu wait, not to this wait)."""
        deadlines = []

        class P:
            def __init__(self):
                self.url = "about:blank"

            def goto(self, url, **kw):
                self.url = url

            def is_closed(self):
                return True

            def close(self):
                pass

        class Ctx:
            @property
            def pages(self):
                return []

            def new_page(self):
                return P()

        cands = [{"id": "a", "webViewLink": "https://x/1"},
                 {"id": "b", "webViewLink": "https://x/2"}]
        # direct default is also 30s
        import inspect
        self.assertEqual(
            inspect.signature(discover._wait_for_script_page)
            .parameters["deadline_ms"].default, 30_000)
        orig_click, orig_wait = (discover._click_apps_script,
                                 discover._wait_for_script_page)
        discover._click_apps_script = lambda page, mime, **kw: None
        discover._wait_for_script_page = (
            lambda ctx, page, baseline, **kw:
            (deadlines.append(kw.get("deadline_ms")), {"id": SID_A, "url": "u"})[1])
        try:
            with tempfile.TemporaryDirectory() as td:
                rows, failures = discover.run_discovery(
                    cands, Ctx(), Path(td) / "ids.json", log=lambda m: None)
                self.assertFalse(failures)
                self.assertEqual(deadlines, [30_000, 30_000])
        finally:
            (discover._click_apps_script,
             discover._wait_for_script_page) = orig_click, orig_wait

    def test_script_wait_timeout_message_is_sanitized(self):
        """Timeout error reports only host categories + tab counts: no
        URL path, query, Script ID, file name or account text."""
        class P:
            def __init__(self, url):
                self.url = url

        src = P("https://docs.google.com/document/d/FILESECRET123/edit?x=1")
        login = P("https://accounts.google.com/signin/v2/challenge?e=user%40example.com")
        blank = P("about:blank")

        class Ctx:
            @property
            def pages(self):
                return [login, blank]

        with self.assertRaises(TimeoutError) as cm:
            discover._wait_for_script_page(Ctx(), src, set(),
                                           deadline_ms=1,
                                           sleep=lambda s: None)
        msg = str(cm.exception)
        for leaked in ("FILESECRET123", "user%40example.com", "signin",
                       "/document/", "edit?x", "challenge"):
            self.assertNotIn(leaked, msg)
        self.assertIn("tabs seen: 3", msg)
        self.assertIn("login=1", msg)
        self.assertIn("blank=1", msg)
        self.assertIn("google=1", msg)


    def test_keeper_and_baseline_tabs_survive_cleanup(self):
        """Tabs that existed before a candidate (keeper tab, or an
        unrelated pre-existing script tab) are never closed by the
        per-candidate cleanup and can never supply a Script ID."""
        class P:
            def __init__(self, url="about:blank"):
                self.url = url
                self.closed = False

            def goto(self, url, **kw):
                self.url = url

            def is_closed(self):
                return self.closed

            def close(self):
                self.closed = True

        keeper = P()
        stale_script = P(
            f"https://script.google.com/home/projects/{'O'*44}")

        class Ctx:
            def __init__(self):
                self._pages = [keeper, stale_script]

            @property
            def pages(self):
                return self._pages

            def new_page(self):
                p = P()
                self._pages.append(p)
                return p

        orig_click, orig_wait = (discover._click_apps_script,
                                 discover._wait_for_script_page)
        discover._click_apps_script = lambda page, mime, **kw: None
        discover._wait_for_script_page = (
            lambda ctx, page, baseline, **kw:
            (_ for _ in ()).throw(TimeoutError("no script page")))
        try:
            with tempfile.TemporaryDirectory() as td:
                rows, failures = discover.run_discovery(
                    [{"id": "f1", "webViewLink": "https://docs/x"}],
                    Ctx(), Path(td) / "ids.json", log=lambda m: None)
                self.assertEqual(len(failures), 1)
                self.assertFalse(keeper.closed)
                self.assertFalse(stale_script.closed)
                self.assertFalse(rows)  # baseline script tab never used
        finally:
            (discover._click_apps_script,
             discover._wait_for_script_page) = orig_click, orig_wait


    def test_wait_uses_page_wait_for_timeout_by_default(self):
        """Without injected sleep, the poll loop must drive Playwright's
        event dispatch via source_page.wait_for_timeout -- a blocking
        time.sleep stalls context.pages updates (proven root cause)."""
        calls = []

        class P:
            def __init__(self):
                self.url = "about:blank"

            def wait_for_timeout(self, ms):
                calls.append(ms)

        class Ctx:
            @property
            def pages(self):
                return []

        with self.assertRaises(TimeoutError):
            discover._wait_for_script_page(Ctx(), P(), set(),
                                           deadline_ms=50)
        self.assertTrue(calls)
        self.assertTrue(all(ms == 500 for ms in calls))

        # injected fast sleep still honored (no wait_for_timeout calls)
        calls.clear()
        with self.assertRaises(TimeoutError):
            discover._wait_for_script_page(Ctx(), P(), set(),
                                           deadline_ms=1,
                                           sleep=lambda s: calls.append("x"))
        self.assertTrue(calls and all(c == "x" for c in calls))


class TestAuthHelpers(unittest.TestCase):
    def test_token_scope_precheck(self):
        from gas_export import auth
        with tempfile.TemporaryDirectory() as td:
            tok = Path(td) / "t.json"
            self.assertFalse(auth.credentials_token_has_scopes(
                tok, ["scope.a"]))
            tok.write_text(json.dumps({"scopes": ["scope.a", "scope.b"]}),
                           encoding="utf-8")
            self.assertTrue(auth.credentials_token_has_scopes(tok, ["scope.a"]))
            self.assertFalse(auth.credentials_token_has_scopes(tok, ["scope.c"]))
            tok.write_text(json.dumps({"scope": "scope.a scope.c"}), encoding="utf-8")
            self.assertTrue(auth.credentials_token_has_scopes(tok, ["scope.c"]))

    def test_export_date_fixed_before_auth(self):
        """local_today must be evaluated before any OAuth/consent delay."""
        calls = []
        orig_today = exporter.local_today
        exporter.local_today = lambda now=None: (
            calls.append("today"), orig_today(now))[1]
        import gas_export.auth as auth_mod
        orig_load, orig_build = auth_mod.load_credentials, auth_mod.build_service
        auth_mod.load_credentials = lambda *a, **k: calls.append("auth") or object()
        auth_mod.build_service = lambda *a, **k: FakeScriptApi(
            {SID_A: {"title": "T"}}, {SID_A: {"files": [gs_file()]}})
        try:
            args = type("A", (), {"script_id": SID_A, "ids": None,
                                  "credentials": "c", "token": "t",
                                  "out": tempfile.mkdtemp()})
            with tempfile.TemporaryDirectory() as td:
                args.out = td
                rc = exporter.cmd_export(args, log=lambda m: None)
                self.assertEqual(rc, 0)
            self.assertEqual(calls[0], "today")
            self.assertIn("auth", calls)
        finally:
            exporter.local_today = orig_today
            auth_mod.load_credentials, auth_mod.build_service = orig_load, orig_build

    def test_discover_resume_filters_stale_rows(self):
        """script-ids.json rows for files no longer in the folders must not
        be kept on resume (they would export stale IDs)."""
        import gas_export.auth as auth_mod
        kept_file = {"id": "keep1", "name": "keep",
                     "mimeType": "application/vnd.google-apps.document",
                     "webViewLink": "https://docs.google.com/keep"}
        pages = [{"files": [kept_file]}]
        date_dir = None
        with tempfile.TemporaryDirectory() as td:
            # pre-seed today's script-ids.json with a row for a removed file
            date_dir = Path(td) / common.local_today()
            date_dir.mkdir(parents=True)
            (date_dir / "script-ids.json").write_text(json.dumps([
                {"fileId": "keep1", "fileName": "stale-old-name",
                 "scriptId": SID_A,
                 "folderId": "F", "folderName": "stale-alias"},
                {"fileId": "gone1", "fileName": "removed", "scriptId": SID_B,
                 "folderId": "F", "folderName": "fold"},
            ]), encoding="utf-8")

            class P:
                def __init__(self):
                    self.url = "about:blank"

                def is_closed(self):
                    return True

                def close(self):
                    pass

            class Ctx:
                def new_page(self):
                    return P()

                @property
                def pages(self):
                    return []

                def close(self):
                    pass

            orig = (auth_mod.load_credentials, auth_mod.build_service,
                    discover.launch_browser, discover._click_apps_script,
                    discover._wait_for_script_page)
            auth_mod.load_credentials = lambda *a, **k: object()
            auth_mod.build_service = lambda *a, **k: FakeDrive(pages)
            discover.launch_browser = lambda **k: (None, None, Ctx())
            discover._click_apps_script = lambda page, mime, **kw: None
            discover._wait_for_script_page = lambda ctx, page, baseline, **kw: {
                "id": SID_A, "url": "u"}
            try:
                args = type("A", (), {
                    # CLI label is an alias; the real Drive folder name
                    # (FakeDrive.get -> 'RealFolder') must win in output
                    "folders": [{"name": "alias-label", "id": "F"}],
                    "credentials": "c", "token": "t", "out": td,
                    "browser_profile": None, "browser_state": None})
                rc = discover.cmd_discover(args, log=lambda m: None)
                rows = json.loads((date_dir / "script-ids.json")
                                  .read_text(encoding="utf-8"))
                self.assertEqual([r["fileId"] for r in rows], ["keep1"])
                # resumed row canonicalized from CURRENT candidate metadata
                self.assertEqual(rows[0]["folderName"], "RealFolder")
                self.assertEqual(rows[0]["fileName"], "keep")
                self.assertEqual(rows[0]["scriptId"], SID_A)  # preserved
                index = json.loads((date_dir / "_discovery-index.json")
                                   .read_text(encoding="utf-8"))
                self.assertEqual(index["collectedCount"], 1)
                self.assertEqual(rc, 0)
            finally:
                (auth_mod.load_credentials, auth_mod.build_service,
                 discover.launch_browser, discover._click_apps_script,
                 discover._wait_for_script_page) = orig


class TestCli(unittest.TestCase):
    def test_mutually_exclusive_and_required(self):
        parser = cli.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["export", "--credentials", "c.json"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["export", "--script-id", SID_A, "--ids", "x.json",
                               "--credentials", "c.json"])
        ns = parser.parse_args(["export", "--script-id", SID_A,
                                "--credentials", "c.json"])
        self.assertEqual(ns.script_id, SID_A)

    def test_folder_spec(self):
        parser = cli.build_parser()
        ns = parser.parse_args(["discover", "--folder", "team=1ABC",
                                "--credentials", "c.json"])
        self.assertEqual(ns.folder, [{"name": "team", "id": "1ABC"}])
        with self.assertRaises(SystemExit):
            parser.parse_args(["discover", "--folder", "noid", "--credentials", "c"])

    def test_config_merge_and_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config.local.json"
            cfg.write_text(json.dumps({
                "credentials": "credentials.json",
                "folders": [{"name": "fold", "id": "FID"}],
                "outputRoots": {"sharedProjects": "共有GAS"},
            }), encoding="utf-8")
            ns = cli.build_parser().parse_args(
                ["discover", "--config", str(cfg)])
            self.assertIsNone(cli._resolve(ns, common.load_config(cfg), Path(td)))
            self.assertTrue(str(ns.credentials).endswith("credentials.json"))
            self.assertEqual(ns.folders, [{"name": "fold", "id": "FID"}])
            self.assertIn("browser-profile", str(ns.browser_profile))
            self.assertIn("token.drive", str(ns.token))
            self.assertEqual(ns.out, Path(td) / "output")
            self.assertEqual(ns.output_roots["shared_projects"], "共有GAS")
            self.assertEqual(ns.output_roots["shared_drive"], "共有ドライブ")

    def test_distinct_token_caches_per_command(self):
        """driveToken/scriptToken must not cross-pollinate: a Drive-scoped
        token would later be rejected by export's scope check."""
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config.local.json"
            cfg.write_text(json.dumps({
                "credentials": "credentials.json",
                "driveToken": "dtok.json",
                "scriptToken": "stok.json",
                "folders": [{"name": "f", "id": "FID"}],
            }), encoding="utf-8")
            conf = common.load_config(cfg)
            d = cli.build_parser().parse_args(["discover", "--config", str(cfg)])
            e = cli.build_parser().parse_args(
                ["export", "--script-id", SID_A, "--config", str(cfg)])
            cli._resolve(d, conf, Path(td))
            cli._resolve(e, conf, Path(td))
            self.assertTrue(str(d.token).endswith("dtok.json"))
            self.assertTrue(str(e.token).endswith("stok.json"))
            # --token overrides only the command-specific cache
            d2 = cli.build_parser().parse_args(
                ["discover", "--config", str(cfg), "--token", "custom.json"])
            cli._resolve(d2, conf, Path(td))
            self.assertTrue(str(d2.token).endswith("custom.json"))
            # defaults without config keys are distinct working-directory files
            d3 = cli.build_parser().parse_args(
                ["discover", "--credentials", "c.json", "--folder", "a=b"])
            e3 = cli.build_parser().parse_args(
                ["export", "--credentials", "c.json", "--script-id", SID_A])
            cli._resolve(d3, {}, None)
            cli._resolve(e3, {}, None)
            self.assertNotEqual(str(d3.token), str(e3.token))

    def test_missing_credentials_rejected(self):
        ns = cli.build_parser().parse_args(["discover", "--folder", "a=b"])
        self.assertEqual(cli._resolve(ns, {}, None), 2)

    def test_invalid_folder_id_rejected_in_resolve(self):
        ns = cli.build_parser().parse_args(
            ["discover", "--credentials", "c.json",
             "--folder", "fold=abc' or trashed = false"])
        self.assertEqual(cli._resolve(ns, {}, None), 2)


class _Loc:
    """Minimal Playwright locator double."""

    def __init__(self, el):
        self._el = el

    @property
    def first(self):
        return self

    def wait_for(self, **kw):
        return None

    def get_attribute(self, k):
        return self._el["attrs"].get(k)

    def inner_text(self):
        return self._el.get("text", "")


class _LocQuery:
    def __init__(self, els):
        self._els = els

    def all(self):
        return [_Loc(e) for e in self._els]


class FakeSharedPage:
    """Fake /home/shared page. elements = list of (scriptId, title);
    initial_shown/grow_by simulate lazy rendering on scroll."""

    def __init__(self, elements, summary=None, initial_shown=None,
                 grow_by=None, extra_global=None):
        self._elements = [
            {"attrs": {"data-script-id": s, "aria-label": t}, "text": t,
             "name": t}
            for s, t in elements]
        self.extra_global = extra_global or []
        self._summary = summary or f"{len(elements)}個のプロジェクトを表示しています"
        self._shown = (len(elements) if initial_shown is None
                       else initial_shown)
        self._grow = grow_by or 0
        self.url = "about:blank"
        self.scrolls = 0
        self.timeouts = []
        self.gotos = []
        self.closed = False

    def goto(self, url, **kw):
        self.url = url
        self.gotos.append(url)

    def close(self):
        self.closed = True

    def get_by_text(self, pattern):
        summary = self._summary

        class L:
            @property
            def first(self):
                return self

            def wait_for(self, **kw):
                return None

            def inner_text(self):
                return summary
        return L()

    def locator(self, sel):
        shown = self._elements[:self._shown]
        if sel == "[data-script-id]":
            return _LocQuery(shown)
        if sel == '[aria-label*="Apps Script"]':
            # global list also contains unrelated labeled nodes to prove
            # row-relative extraction can never misalign titles
            return _LocQuery(shown + self.extra_global)
        return _LocQuery([])

    def evaluate(self, js):
        if "map(row" in js:  # row extraction: only row-relative titles
            return [{"id": e["attrs"]["data-script-id"],
                     "name": e.get("name", e["text"])}
                    for e in self._elements[:self._shown]]
        self.scrolls += 1
        self._shown = min(len(self._elements), self._shown + self._grow)

    def wait_for_timeout(self, ms):
        self.timeouts.append(ms)


class _SharedCtx:
    """Browser-context double: first new_page() is the keeper, the
    second is handed to collect_shared_projects."""

    def __init__(self, shared_page):
        self._shared = shared_page
        self.pages_created = []

    @property
    def pages(self):
        return self.pages_created

    def new_page(self):
        p = (self._shared if len(self.pages_created) == 1
             else FakeSharedPage([]))
        self.pages_created.append(p)
        return p

    def close(self):
        pass


SID_19 = [f"{chr(65 + i)}" * 44 for i in range(19)]


class TestShared(unittest.TestCase):
    def test_shared_scrape_collects_19(self):
        elements = [(sid, f"proj-{i}") for i, sid in enumerate(SID_19)]
        page = FakeSharedPage(elements)
        rows = discover.collect_shared_projects(page, log=lambda m: None)
        self.assertEqual(len(rows), 19)
        self.assertEqual(page.gotos,
                         ["https://script.google.com/home/shared"])
        for i, sid in enumerate(SID_19):
            self.assertEqual(rows[sid], f"proj-{i}")

    def test_shared_scrape_scroll_accumulates(self):
        elements = [(sid, f"p{i}") for i, sid in enumerate(SID_19)]
        page = FakeSharedPage(elements, initial_shown=5, grow_by=4)
        rows = discover.collect_shared_projects(page, log=lambda m: None)
        self.assertEqual(len(rows), 19)
        self.assertGreater(page.scrolls, 0)
        # event-driven wait after every scroll (no blocking sleep)
        self.assertTrue(all(ms == 500 for ms in page.timeouts))
        self.assertTrue(page.timeouts)

    def test_shared_scrape_count_mismatch_fail_closed(self):
        elements = [(sid, f"p{i}") for i, sid in enumerate(SID_19[:3])]
        page = FakeSharedPage(elements, summary="5個のプロジェクトを表示しています")
        with self.assertRaises(RuntimeError) as cm:
            discover.collect_shared_projects(page, log=lambda m: None)
        self.assertIn("count mismatch", str(cm.exception))
        for sid in SID_19:
            self.assertNotIn(sid, str(cm.exception))

    def test_shared_row_relative_titles_not_misaligned(self):
        """Unrelated global [aria-label*="Apps Script"] nodes cannot
        shift titles onto the wrong Script ID: extraction is
        row-relative via each row's own querySelector."""
        elements = [(sid, f"t-{i}") for i, sid in enumerate(SID_19[:3])]
        junk = [{"attrs": {"aria-label": "UNRELATED HEADER"},
                 "text": "UNRELATED HEADER"}]
        page = FakeSharedPage(elements, extra_global=junk)
        rows = discover._collect_shared_rows(page)
        self.assertEqual(rows, {sid: f"t-{i}"
                                for i, sid in enumerate(SID_19[:3])})

    def test_shared_missing_title_fail_closed(self):
        elements = [(SID_19[0], "ok"), (SID_19[1], "")]
        page = FakeSharedPage(elements)
        with self.assertRaises(ValueError):
            discover._collect_shared_rows(page)

    def test_shared_duplicate_conflicting_title_fail_closed(self):
        elements = [(SID_19[0], "t-a"), (SID_19[0], "t-b")]
        page = FakeSharedPage(elements)
        with self.assertRaises(ValueError):
            discover._collect_shared_rows(page)

    def test_shared_manifests_go_to_logs_not_output(self):
        import gas_export.discover as dsc
        from unittest.mock import patch
        page = FakeSharedPage([(s, f"t-{i}") for i, s in enumerate(SID_19)])
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "output"
            logs = Path(td) / "logs"
            with patch.object(dsc, "launch_browser", return_value=(None, None, _SharedCtx(page))):
                rc = dsc.cmd_discover(self._shared_args(str(output), logs=logs), log=lambda m: None)
            self.assertEqual(rc, 0)
            self.assertTrue((logs / common.local_today() / "script-ids.json").is_file())
            self.assertTrue((logs / common.local_today() / "_discovery-index.json").is_file())
            self.assertFalse((output / common.local_today()).exists())

    def _shared_args(self, td, **kw):
        base = {"folders": [], "shared": True, "credentials": None,
                "token": None, "out": td, "browser_profile": None,
                "browser_state": None}
        base.update(kw)
        return type("A", (), base)

    def test_shared_only_no_drive_oauth(self):
        """Shared-only discover must not touch Drive OAuth/service at all
        (auth module is only imported inside the folder phase)."""
        import gas_export.discover as dsc
        page = FakeSharedPage([(s, f"t-{i}") for i, s in enumerate(SID_19)])
        orig_launch = dsc.launch_browser
        dsc.launch_browser = lambda **k: (None, None, _SharedCtx(page))
        try:
            with tempfile.TemporaryDirectory() as td:
                rc = dsc.cmd_discover(self._shared_args(td),
                                      log=lambda m: None)
                self.assertEqual(rc, 0)
                date_dir = Path(td) / common.local_today()
                merged = json.loads((date_dir / "script-ids.json")
                                    .read_text(encoding="utf-8"))
                self.assertEqual(len(merged), 19)
                for row, sid in zip(sorted(merged, key=lambda r: r["scriptId"]),
                                    sorted(SID_19)):
                    self.assertEqual(row["scriptId"], sid)
                    self.assertEqual(row["source"], "shared")
                    self.assertNotIn("fileId", row)
                    self.assertNotIn("folderName", row)
                    self.assertTrue(row["fileName"])
                snap = json.loads((date_dir / "shared-script-ids.json")
                                  .read_text(encoding="utf-8"))
                self.assertEqual(len(snap), 19)
                self.assertFalse((date_dir / "folder-script-ids.json").exists())
                index = json.loads((date_dir / "_discovery-index.json")
                                   .read_text(encoding="utf-8"))
                self.assertEqual(index["sharedCollectedCount"], 19)
                self.assertEqual(index["collectedCount"], 19)
        finally:
            dsc.launch_browser = orig_launch

    def test_shared_only_does_not_reuse_folder_layout(self):
        """Disabled folder discovery must not leak cached folder records."""
        import gas_export.discover as dsc
        dup = SID_19[0]
        page = FakeSharedPage([(dup, "shared-title"), (SID_19[1], "other")])
        orig_launch = dsc.launch_browser
        dsc.launch_browser = lambda **k: (None, None, _SharedCtx(page))
        try:
            with tempfile.TemporaryDirectory() as td:
                date_dir = Path(td) / common.local_today()
                date_dir.mkdir(parents=True)
                (date_dir / "folder-script-ids.json").write_text(json.dumps([
                    {"fileId": "FID", "folderId": "F", "folderName": "Real",
                     "fileName": "doc", "scriptId": dup, "url": "u"},
                ]), encoding="utf-8")
                rc = dsc.cmd_discover(self._shared_args(td),
                                      log=lambda m: None)
                self.assertEqual(rc, 0)
                merged = json.loads((date_dir / "script-ids.json")
                                    .read_text(encoding="utf-8"))
                self.assertEqual(len(merged), 2)
                by_sid = {r["scriptId"]: r for r in merged}
                self.assertNotIn("fileId", by_sid[dup])
                self.assertNotIn("fileId", by_sid[SID_19[1]])
        finally:
            dsc.launch_browser = orig_launch

    def test_shared_failure_does_not_clobber(self):
        """On shared scrape failure the prior shared snapshot and the
        merged root manifest are left untouched; exit nonzero."""
        import gas_export.discover as dsc
        page = FakeSharedPage([], summary="5個のプロジェクトを表示しています")
        orig_launch = dsc.launch_browser
        dsc.launch_browser = lambda **k: (None, None, _SharedCtx(page))
        try:
            with tempfile.TemporaryDirectory() as td:
                date_dir = Path(td) / common.local_today()
                date_dir.mkdir(parents=True)
                prior_shared = [{"scriptId": SID_19[0], "fileName": "old",
                                 "source": "shared"}]
                prior_root = [{"scriptId": SID_19[1], "fileName": "root",
                               "source": "shared"}]
                (date_dir / "shared-script-ids.json").write_text(
                    json.dumps(prior_shared), encoding="utf-8")
                (date_dir / "script-ids.json").write_text(
                    json.dumps(prior_root), encoding="utf-8")
                rc = dsc.cmd_discover(self._shared_args(td),
                                      log=lambda m: None)
                self.assertEqual(rc, 1)
                self.assertEqual(json.loads(
                    (date_dir / "shared-script-ids.json")
                    .read_text(encoding="utf-8")), prior_shared)
                # last complete root manifest preserved verbatim
                self.assertEqual(json.loads(
                    (date_dir / "script-ids.json")
                    .read_text(encoding="utf-8")), prior_root)
                index = json.loads((date_dir / "_discovery-index.json")
                                   .read_text(encoding="utf-8"))
                # on failure the index reports the LAST COMPLETE root
                # count, not the partial run's
                self.assertEqual(index["collectedCount"], 1)
        finally:
            dsc.launch_browser = orig_launch

    def test_shared_only_ignores_inactive_corrupt_folder_snapshot(self):
        """An inactive snapshot must not block the requested shared run."""
        import gas_export.discover as dsc
        page = FakeSharedPage([(s, f"t{i}") for i, s in enumerate(SID_19)])
        orig_launch = dsc.launch_browser
        dsc.launch_browser = lambda **k: (None, None, _SharedCtx(page))
        try:
            with tempfile.TemporaryDirectory() as td:
                date_dir = Path(td) / common.local_today()
                date_dir.mkdir(parents=True)
                (date_dir / "folder-script-ids.json").write_text(
                    "{not json", encoding="utf-8")
                prior_root = [{"scriptId": SID_19[0], "fileName": "root",
                               "source": "shared"}]
                (date_dir / "script-ids.json").write_text(
                    json.dumps(prior_root), encoding="utf-8")
                rc = dsc.cmd_discover(self._shared_args(td),
                                      log=lambda m: None)
                self.assertEqual(rc, 0)
                self.assertEqual(len(json.loads(
                    (date_dir / "script-ids.json")
                    .read_text(encoding="utf-8"))), len(SID_19))
                index = json.loads((date_dir / "_discovery-index.json")
                                   .read_text(encoding="utf-8"))
                self.assertFalse(index["failures"])
        finally:
            dsc.launch_browser = orig_launch

    def test_folder_failure_preserves_root(self):
        """Folder-phase failure + shared success must NOT overwrite the
        last complete root manifest."""
        import gas_export.auth as auth_mod
        import gas_export.discover as dsc
        page = FakeSharedPage([(SID_19[0], "shared-a")])
        orig = (auth_mod.load_credentials, auth_mod.build_service,
                dsc.launch_browser, dsc.get_folder_location)
        auth_mod.load_credentials = lambda *a, **k: object()
        auth_mod.build_service = lambda *a, **k: object()
        dsc.get_folder_location = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("folder metadata failed"))
        dsc.launch_browser = lambda **k: (None, None, _SharedCtx(page))
        try:
            with tempfile.TemporaryDirectory() as td:
                date_dir = Path(td) / common.local_today()
                date_dir.mkdir(parents=True)
                prior_root = [{"scriptId": SID_19[1], "fileName": "root",
                               "source": "shared"}]
                (date_dir / "script-ids.json").write_text(
                    json.dumps(prior_root), encoding="utf-8")
                args = self._shared_args(
                    td, folders=[{"name": "f", "id": "F"}], shared=True,
                    credentials="c", token="t")
                rc = dsc.cmd_discover(args, log=lambda m: None)
                self.assertEqual(rc, 1)
                self.assertEqual(json.loads(
                    (date_dir / "script-ids.json")
                    .read_text(encoding="utf-8")), prior_root)
                index = json.loads((date_dir / "_discovery-index.json")
                                   .read_text(encoding="utf-8"))
                self.assertEqual(index["collectedCount"], 1)
                self.assertEqual(index["sharedCollectedCount"], 1)
        finally:
            (auth_mod.load_credentials, auth_mod.build_service,
             dsc.launch_browser, dsc.get_folder_location) = orig

    def test_shared_only_no_mutation_warning(self):
        """The blank-project mutation warning must NOT print for a
        shared-only run (read-only list scrape); it must print for a
        folder run."""
        import io
        import contextlib
        import gas_export.discover as dsc
        page = FakeSharedPage([(SID_19[0], "a")])
        orig_launch = dsc.launch_browser
        dsc.launch_browser = lambda **k: (None, None, _SharedCtx(page))
        try:
            with tempfile.TemporaryDirectory() as td:
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    dsc.cmd_discover(self._shared_args(td),
                                     log=lambda m: None)
                self.assertNotIn("MUTATION", err.getvalue())
                self.assertNotIn("blank bound project", err.getvalue())
        finally:
            dsc.launch_browser = orig_launch

    def test_shared_only_does_not_adopt_legacy_folder_manifest(self):
        """Only explicitly requested sources enter the current manifest."""
        import gas_export.discover as dsc
        legacy = [{"fileId": "F1", "folderId": "FD", "folderName": "Real",
                   "fileName": "d1", "scriptId": SID_19[0], "url": "u"}]
        page = FakeSharedPage([(SID_19[1], "shared-one")])
        orig_launch = dsc.launch_browser
        dsc.launch_browser = lambda **k: (None, None, _SharedCtx(page))
        try:
            with tempfile.TemporaryDirectory() as td:
                date_dir = Path(td) / common.local_today()
                date_dir.mkdir(parents=True)
                (date_dir / "script-ids.json").write_text(
                    json.dumps(legacy), encoding="utf-8")
                rc = dsc.cmd_discover(self._shared_args(td),
                                      log=lambda m: None)
                self.assertEqual(rc, 0)
                self.assertFalse((date_dir / "folder-script-ids.json").exists())
                merged = json.loads((date_dir / "script-ids.json")
                                    .read_text(encoding="utf-8"))
                self.assertEqual({r["scriptId"] for r in merged},
                                 {SID_19[1]})
        finally:
            dsc.launch_browser = orig_launch

    def test_export_shared_only_root_path(self):
        """Shared rows export under the configured Apps Script shared root
        with the Script ID stored as an empty marker file."""
        import gas_export.auth as auth_mod
        sid = SID_19[0]
        api = FakeScriptApi({sid: {"title": "T"}},
                            {sid: {"files": [gs_file()]}})
        orig_load, orig_build = (auth_mod.load_credentials,
                                 auth_mod.build_service)
        auth_mod.load_credentials = lambda *a, **k: object()
        auth_mod.build_service = lambda *a, **k: api
        try:
            with tempfile.TemporaryDirectory() as td:
                date_dir = Path(td) / common.local_today()
                date_dir.mkdir(parents=True)
                ids = date_dir / "script-ids.json"
                ids.write_text(json.dumps([
                    {"scriptId": sid, "fileName": "my-proj",
                     "source": "shared"}]), encoding="utf-8")
                args = type("A", (), {"script_id": None, "ids": str(ids),
                                      "credentials": "c", "token": "t",
                                      "out": td})
                rc = exporter.cmd_export(args, log=lambda m: None)
                self.assertEqual(rc, 0)
                proj = date_dir / "Apps Scrip共有済み" / "my-proj"
                self.assertTrue((proj / "Code.gs").exists())
                self.assertEqual((proj / sid).read_bytes(), b"")
        finally:
            auth_mod.load_credentials, auth_mod.build_service = (
                orig_load, orig_build)


if __name__ == "__main__":
    unittest.main()

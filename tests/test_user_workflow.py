import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_gas_export import FakeScriptApi, FakeDrive, FakeSharedPage, _SharedCtx, gs_file, SID_A, SID_B
from gas_export import cli, discover, exporter


class UserWorkflowTests(unittest.TestCase):
    def config(self, td):
        root = Path(td)
        (root / "credentials.json").write_text("{}", encoding="utf-8")
        path = root / "config.local.json"
        path.write_text(json.dumps({"credentials": "credentials.json", "shared": True,
                                    "folders": [], "out": "sources", "logs": "reports",
                                    "driveToken": "drive.json", "scriptToken": "script.json"}), encoding="utf-8")
        return path

    def test_check_has_no_google_calls_or_output(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.config(td)
            with patch("gas_export.discover.cmd_discover") as remote, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["check", "--config", str(cfg)]), 0)
                remote.assert_not_called()
            self.assertFalse((Path(td) / "sources").exists())
            self.assertFalse((Path(td) / "reports").exists())

    def test_run_passes_actual_manifest_and_separate_script_token(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.config(td)
            actual = Path(td) / "reports" / "20260101" / "script-ids.json"
            def discovery(args, log):
                self.assertEqual(args.token, Path(td) / "drive.json")
                args.manifest_path = actual
                return 0
            def export(args, log):
                self.assertEqual(args.ids, actual)
                self.assertEqual(args.token, Path(td) / "script.json")
                self.assertEqual(args.out, Path(td) / "sources")
                return 0
            with patch("gas_export.discover.cmd_discover", side_effect=discovery), patch("gas_export.exporter.cmd_export", side_effect=export) as save, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["--config", str(cfg)]), 0)
                save.assert_called_once()

    def test_failed_discovery_never_exports_stale_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.config(td)
            with patch("gas_export.discover.cmd_discover", return_value=1), patch("gas_export.exporter.cmd_export") as save, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["run", "--config", str(cfg)]), 1)
                save.assert_not_called()

    def test_malformed_json_has_location_and_advice_without_traceback(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "bad.json"
            cfg.write_text('{"shared": true,}', encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                self.assertEqual(cli.main(["check", "--config", str(cfg)]), 2)
            self.assertIn("行目", output.getvalue())
            self.assertIn("対処", output.getvalue())
            self.assertNotIn("Traceback", output.getvalue())

    def test_auth_failure_has_advice_and_private_diagnostic_log(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.config(td)
            output = io.StringIO()
            with patch("gas_export.discover.cmd_discover", side_effect=RuntimeError("invalid_grant")), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(output):
                self.assertEqual(cli.main(["run", "--config", str(cfg)]), 1)
            self.assertIn("認証", output.getvalue())
            self.assertIn("対処", output.getvalue())
            self.assertNotIn("Traceback", output.getvalue())
            log = next((Path(td) / "reports").rglob("*.log"))
            self.assertIn("invalid_grant", log.read_text(encoding="utf-8"))

    def test_config_typos_and_string_booleans_rejected(self):
        for cfg in ({"shared": "false"}, {"folers": []}, {"folders": [{}]}):
            with self.subTest(cfg=cfg), self.assertRaises(ValueError):
                cli.validate_config(cfg)

    def test_same_name_partial_rerun_does_not_overwrite_other_project(self):
        api = FakeScriptApi({SID_A: {"scriptId": SID_A, "title": "same"}, SID_B: {"scriptId": SID_B, "title": "same"}},
                            {SID_A: {"files": [gs_file(source="function a(){}") ]}, SID_B: {"files": [gs_file(source="function b(){}") ]}})
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "20260101"
            first = exporter.export_projects(api, [{"scriptId": SID_A}, {"scriptId": SID_B}], output, log=lambda _: None)
            second = exporter.export_projects(api, [{"scriptId": SID_B}], output, log=lambda _: None)
            self.assertEqual(second["success"][0]["outputDirectory"], first["success"][1]["outputDirectory"])
            a = Path(first["success"][0]["outputDirectory"])
            self.assertEqual((a / "Code.gs").read_text(), "function a(){}")
            self.assertFalse((a / SID_B).exists())

    def test_include_empty_preserves_manifest_only_project(self):
        api = FakeScriptApi({SID_A: {"title": "empty"}}, {SID_A: {"files": [{"name": "appsscript", "type": "JSON", "source": "{}"}]}})
        with tempfile.TemporaryDirectory() as td:
            result = exporter.export_projects(api, [{"scriptId": SID_A}], Path(td), include_empty=True, log=lambda _: None)
            self.assertEqual(result["successCount"], 1)
            self.assertEqual((Path(result["success"][0]["outputDirectory"]) / "appsscript.json").read_text(), "{}")


class PublicationRegressionTests(unittest.TestCase):
    def folder_run(self, td, shared=False):
        return type("Args", (), {"folders": [{"name": "folder", "id": "F"}],
                                 "shared": shared, "credentials": "c", "token": "t",
                                 "out": td, "logs": td, "browser_profile": None,
                                 "browser_state": None, "project_selections": {}})()

    def discover_folder(self, td, shared=False):
        page = FakeSharedPage([(SID_A, "shared duplicate")])
        candidates = [{"id": "FILE", "name": "current", "folderId": "F", "folderName": "folder",
                       "mimeType": "application/vnd.google-apps.spreadsheet"}]
        def run(candidates, context, out_file, existing, log):
            self.last_existing = existing
            from gas_export.common import write_json
            row = {"scriptId": SID_A, "fileId": "FILE", "folderId": "F", "fileName": "current"}
            write_json(out_file, [row])
            return [row], []
        with patch("gas_export.auth.load_credentials", return_value=object()), patch("gas_export.auth.build_service", return_value=FakeDrive([])), patch.object(discover, "launch_browser", return_value=(None, None, _SharedCtx(page))), patch.object(discover, "list_direct_files", return_value=candidates), patch.object(discover, "run_discovery", side_effect=run):
            return discover.cmd_discover(self.folder_run(td, shared), log=lambda _: None)

    def test_folder_only_ignores_old_shared_snapshot(self):
        from gas_export.common import local_today, write_json
        with tempfile.TemporaryDirectory() as td:
            day = Path(td) / local_today()
            write_json(day / "shared-script-ids.json", [{"scriptId": SID_B, "source": "shared"}])
            self.assertEqual(self.discover_folder(td), 0)
            rows = json.loads((day / "script-ids.json").read_text(encoding="utf-8"))
            self.assertEqual([r["scriptId"] for r in rows], [SID_A])

    def test_both_sources_deduplicate_with_current_folder_layout(self):
        from gas_export.common import local_today
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(self.discover_folder(td, shared=True), 0)
            rows = json.loads((Path(td) / local_today() / "script-ids.json").read_text(encoding="utf-8"))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["fileId"], "FILE")

    def test_active_corrupt_snapshot_preserves_complete_manifest(self):
        from gas_export.common import local_today, write_json
        with tempfile.TemporaryDirectory() as td:
            day = Path(td) / local_today()
            prior = [{"scriptId": SID_B}]
            write_json(day / "script-ids.json", prior)
            (day / "folder-script-ids.json").write_text("bad json", encoding="utf-8")
            self.assertEqual(self.discover_folder(td), 1)
            self.assertEqual(json.loads((day / "script-ids.json").read_text(encoding="utf-8")), prior)

    def test_removed_selection_override_invalidates_cached_id(self):
        from gas_export.common import local_today, write_json
        with tempfile.TemporaryDirectory() as td:
            write_json(Path(td) / local_today() / "folder-script-ids.json", [
                {"fileId": "FILE", "folderId": "F", "scriptId": SID_A,
                 "selectionOverride": "previous explicit project"}])
            self.assertEqual(self.discover_folder(td), 0)
            self.assertEqual(self.last_existing, {})

    def test_bom_in_manually_edited_id_list(self):
        with tempfile.TemporaryDirectory() as td:
            ids = Path(td) / "ids.json"
            ids.write_text(json.dumps([SID_A]), encoding="utf-8-sig")
            self.assertEqual(exporter.load_entries(ids), [{"scriptId": SID_A}])

    def test_failed_atomic_replace_preserves_previous_file(self):
        from gas_export.common import write_json
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ids.json"
            write_json(path, ["old"])
            with patch("gas_export.common.os.replace", side_effect=OSError("disk failure")), self.assertRaises(OSError):
                write_json(path, ["new"])
            self.assertEqual(json.loads(path.read_text()), ["old"])
            self.assertEqual(list(Path(td).iterdir()), [path])

    def test_wrong_bound_parent_fails_without_saving_source(self):
        api = FakeScriptApi({SID_A: {"parentId": "OTHER"}}, {SID_A: {"files": [gs_file()]}})
        with tempfile.TemporaryDirectory() as td:
            result = exporter.export_projects(api, [{"scriptId": SID_A, "fileId": "FILE"}], Path(td) / "out", log=lambda _: None)
            self.assertEqual(result["failureCount"], 1)
            self.assertFalse(list((Path(td) / "out").rglob("*.gs")))
            self.assertIn("一致", result["failures"][0]["cause"])

    def test_source_line_endings_preserved(self):
        source = "function a() {\r\n  return 1;\r\n}\n"
        api = FakeScriptApi({SID_A: {"title": "source"}}, {SID_A: {"files": [gs_file(source=source)]}})
        with tempfile.TemporaryDirectory() as td:
            result = exporter.export_projects(api, [{"scriptId": SID_A}], Path(td), log=lambda _: None)
            saved = Path(result["success"][0]["outputDirectory"]) / "Code.gs"
            self.assertEqual(saved.read_bytes(), source.encode("utf-8"))

    def test_cli_paths_are_relative_to_working_directory(self):
        parser = cli.build_parser()
        with tempfile.TemporaryDirectory() as td:
            args = parser.parse_args(["discover", "--shared", "--credentials", "cli.json", "--token", "cli-token.json", "--browser-state", "cli-state.json"])
            self.assertIsNone(cli._resolve(args, {}, Path(td)))
            self.assertEqual(args.credentials, Path.cwd() / "cli.json")
            self.assertEqual(args.token, Path.cwd() / "cli-token.json")
            self.assertEqual(args.browser_state, Path.cwd() / "cli-state.json")

    def test_default_caches_stay_next_to_config(self):
        import os
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"APPDATA": str(Path(td) / "unused-appdata")}):
            base = Path(td) / "project"
            args = cli.build_parser().parse_args(["run"])
            self.assertIsNone(cli._resolve(args, {"credentials": "credentials.json", "shared": True}, base))
            self.assertEqual(args.token, base / "token.drive.readonly.json")
            self.assertEqual(args.browser_profile, base / "browser-profile")
            self.assertFalse((Path(td) / "unused-appdata").exists())
            args.command = "run"
            def discovery(args, log):
                args.manifest_path = base / "logs" / "ids.json"
                return 0
            with patch("gas_export.discover.cmd_discover", side_effect=discovery), patch("gas_export.exporter.cmd_export", return_value=0) as save:
                cli.run_all(args, {}, base, lambda _: None)
                self.assertEqual(save.call_args.args[0].token, base / "token.script.json")

    def test_no_config_caches_use_working_directory(self):
        args = cli.build_parser().parse_args(["discover", "--shared"])
        self.assertIsNone(cli._resolve(args, {}, None))
        self.assertEqual(args.token, Path.cwd() / "token.drive.readonly.json")
        self.assertEqual(args.browser_profile, Path.cwd() / "browser-profile")

    def test_nested_log_and_source_roots_rejected(self):
        args = type("Args", (), {"credentials": None, "out": Path("output"), "logs": Path("output/logs")})()
        with self.assertRaises(ValueError):
            cli.preflight(args)

    def test_unknown_output_root_key_rejected(self):
        with self.assertRaises(ValueError):
            cli.validate_config({"outputRoots": {"sharedDrive": "typo"}})


class SelectionTests(unittest.TestCase):
    def page(self, links):
        class Locator:
            def evaluate_all(self, _):
                return links
        class Page:
            url = "https://script.google.com/u/0/select"
            def locator(self, _):
                return Locator()
        return Page()

    def test_selection_in_original_tab_waits_for_canonical_id(self):
        page = self.page([{"text": "target", "href": "d/legacy/edit?mid=secret"}])
        page.wait_for_load_state = lambda _: None
        page.goto = lambda *a, **kw: setattr(page, "url", "https://script.google.com/u/0/d/legacy/edit")
        def wait(_):
            page.url = f"https://script.google.com/home/projects/{SID_A}/edit?emtoken=secret"
        context = type("Context", (), {"pages": [page]})()
        result = discover._wait_for_script_page(context, page, set(), expected_title="target", sleep=wait)
        self.assertEqual(result["id"], SID_A)
        self.assertNotIn("secret", result["url"])

    def test_selection_uses_exact_title_and_ignores_create_and_other_hosts(self):
        page = self.page([{"text": "target", "href": "https://example.com/d/wrong/edit"},
                          {"text": "target", "href": "https://script.google.com/u/0/create"},
                          {"text": "other", "href": "d/other/edit"},
                          {"text": "target", "href": "d/right/edit"}])
        self.assertEqual(discover.selection_link(page, "target"), "https://script.google.com/u/0/d/right/edit")

    def test_selection_url_can_disambiguate_duplicate_titles(self):
        page = self.page([{"text": "無題", "href": "d/one/edit?mid=temporary"}, {"text": "無題", "href": "d/two/edit?mid=temporary"}])
        self.assertEqual(discover.selection_link(page, "https://script.google.com/u/0/d/two/edit"), "https://script.google.com/u/0/d/two/edit?mid=temporary")

    def test_choice_diagnostics_do_not_store_temporary_auth_queries(self):
        page = self.page([{"text": "unknown", "href": "d/one/edit?mid=private&emtoken=secret"}])
        with self.assertRaises(discover.ProjectSelectionError) as caught:
            discover.selection_link(page, "target")
        self.assertEqual(caught.exception.choices[0]["editorLink"], "https://script.google.com/u/0/d/one/edit")
        self.assertNotIn("secret", str(caught.exception))

    def test_ambiguous_selection_requires_user_configuration(self):
        page = self.page([{"text": "無題", "href": "d/one/edit"}, {"text": "無題", "href": "d/two/edit"}])
        with self.assertRaises(discover.ProjectSelectionError):
            discover.selection_link(page, "無題")


if __name__ == "__main__":
    unittest.main()

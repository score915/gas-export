"""Windows batch regression tests in disposable workspaces, without Google or pip installs."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows batch entry points")
class BatchTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.cleanup_temp)
        self.project = Path(self.temp.name) / "project with spaces"
        self.project.mkdir()
        shutil.copytree(ROOT / "tools", self.project / "tools")
        for name in ROOT.glob("*.bat"):
            shutil.copy2(name, self.project / name.name)
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(self.project / ".venv")], check=True,
                       capture_output=True, timeout=60)
        (self.project / "config.local.json").write_text('{"logs": "logs"}', encoding="utf-8")
        (self.project / "run.py").write_text(
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "p = Path('calls.json')\n"
            "rows = json.loads(p.read_text()) if p.exists() else []\n"
            "rows.append(sys.argv[1:])\n"
            "p.write_text(json.dumps(rows))\n"
            "sys.exit(7 if os.environ.get('FAKE_CHECK_FAIL') and sys.argv[1] == 'check' else 0)\n",
            encoding="utf-8")

    def cleanup_temp(self):
        # Windows may briefly retain a handle to the just-exited venv executable.
        # Retry only teardown; a persistent filesystem error still fails the test.
        for attempt in range(20):
            try:
                self.temp.cleanup()
                return
            except (PermissionError, NotADirectoryError):
                if attempt == 19:
                    raise
                time.sleep(0.1)

    def batch(self, name, extra_env=None, arguments='', input_text=None):
        env = {**os.environ, "GAS_EXPORT_NO_PAUSE": "1", "PYTHONUTF8": "1", **(extra_env or {})}
        result = subprocess.run(f'cmd.exe /d /s /c ""{self.project / name}" {arguments}"',
                                cwd=self.temp.name, env=env, capture_output=True, timeout=60,
                                input=input_text.encode('utf-8') if input_text is not None else None)
        calls = self.project / "calls.json"
        self.calls = json.loads(calls.read_text()) if calls.exists() else []
        return result

    def test_run_checks_first_and_works_from_another_directory_with_spaces(self):
        result = self.batch("run_export.bat")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([row[0] for row in self.calls], ["check", "run"])
        self.assertTrue(all(row[-2:] == ["--config", "config.local.json"] for row in self.calls))

    def test_failed_preflight_never_starts_export(self):
        result = self.batch("run_export.bat", {"FAKE_CHECK_FAIL": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([row[0] for row in self.calls], ["check"])

    def test_verification_refuses_more_than_four_entries(self):
        log_dir = self.project / "logs"
        log_dir.mkdir()
        (log_dir / "verification-script-ids.json").write_text(json.dumps(["A" * 44] * 5))
        result = self.batch("test_export.bat")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([row[0] for row in self.calls], ["check"])

    def test_verification_accepts_one_entry_and_uses_export_only(self):
        log_dir = self.project / "logs"
        log_dir.mkdir()
        ids = log_dir / "verification-script-ids.json"
        ids.write_text(json.dumps(["A" * 44]))
        result = self.batch("test_export.bat")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([row[0] for row in self.calls], ["check", "export"])
        self.assertEqual(self.calls[1][self.calls[1].index("--ids") + 1], str(ids))

    def test_restore_moves_old_venv_back_and_retains_replaced_environment(self):
        private = self.project / ".gas-export"
        private.mkdir()
        backup = private / "venv-before"
        (self.project / ".venv").rename(backup)
        (self.project / ".venv").mkdir()
        (self.project / ".venv" / "replaced-marker").write_text("keep")
        (private / "environment-backup.json").write_text('{"kind":"venv","path":".gas-export/venv-before"}')
        result = self.batch("restore_env.bat")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.project / ".venv" / "Scripts" / "python.exe").is_file())
        self.assertFalse(backup.exists())
        self.assertEqual(len(list(private.glob("venv-replaced-*/replaced-marker"))), 1)
        self.assertEqual([row[0] for row in self.calls], ["check"])

    def test_restore_rejects_backup_outside_private_directory(self):
        private = self.project / ".gas-export"
        private.mkdir()
        (private / "environment-backup.json").write_text('{"kind":"venv","path":"../outside"}')
        result = self.batch("restore_env.bat")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.project / ".venv" / "Scripts" / "python.exe").is_file())
        self.assertEqual(self.calls, [])
        self.assertIn("管理領域外", result.stdout.decode("utf-8", errors="replace"))

    def test_update_stops_before_install_if_backup_capture_fails(self):
        # This temporary venv deliberately has no pip, so freeze fails.
        result = self.batch("update_env.bat")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.project / ".gas-export" / "environment-backup.json").exists())
        self.assertTrue((self.project / ".venv" / "Scripts" / "python.exe").is_file())

    def cleanup_fixture(self):
        from datetime import date, timedelta
        old = (date.today() - timedelta(days=50)).strftime('%Y%m%d')
        latest = (date.today() - timedelta(days=40)).strftime('%Y%m%d')
        recent = date.today().strftime('%Y%m%d')
        for root in ('output', 'logs', 'logs/verification'):
            for day in (old, latest):
                folder = self.project / root / day
                folder.mkdir(parents=True)
                (folder / 'keep-or-delete.txt').write_text('sample')
        for name in (recent, 'not-a-date', '20260230'):
            (self.project / 'output' / name).mkdir()
        return old, latest, recent

    def test_cleanup_deletes_old_dates_keeps_latest_and_private_files(self):
        old, latest, recent = self.cleanup_fixture()
        token = self.project / 'token.script.json'
        token.write_text('private')
        result = self.batch('delete_old_data.bat', input_text='Y\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for root in ('output', 'logs', 'logs/verification'):
            self.assertFalse((self.project / root / old).exists())
        self.assertFalse((self.project / 'output' / latest).exists())
        self.assertTrue((self.project / 'logs' / latest).exists())
        self.assertTrue((self.project / 'logs/verification' / latest).exists())
        for name in (recent, 'not-a-date', '20260230'):
            self.assertTrue((self.project / 'output' / name).is_dir())
        self.assertEqual(token.read_text(), 'private')
        self.assertEqual(self.calls, [])

    def test_cleanup_preview_and_cancel_do_not_delete(self):
        old, _, _ = self.cleanup_fixture()
        for args, answer in (('-Preview', None), ('', 'N\n')):
            result = self.batch('delete_old_data.bat', arguments=args, input_text=answer)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((self.project / 'output' / old).exists())

    def test_cleanup_rejects_external_root_before_deletion(self):
        old, _, _ = self.cleanup_fixture()
        (self.project / 'config.local.json').write_text('{"out":"../outside", "logs":"logs"}')
        result = self.batch('delete_old_data.bat', input_text='Y\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.project / 'output' / old).exists())

    def test_cleanup_rejects_overlapping_and_protected_roots(self):
        for output, logs in (('output', 'output/logs'), ('src', 'logs'), ('.', 'logs')):
            (self.project / 'config.local.json').write_text(json.dumps({'out':output,'logs':logs}))
            result = self.batch('delete_old_data.bat', input_text='Y\n')
            self.assertNotEqual(result.returncode, 0)

    def test_cleanup_rejects_junction_inside_old_date(self):
        old, _, _ = self.cleanup_fixture()
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        sentinel = outside / 'do-not-delete.txt'
        sentinel.write_text('private')
        junction = self.project / 'output' / old / 'linked'
        created = subprocess.run(f'cmd.exe /d /s /c mklink /J "{junction}" "{outside}"', capture_output=True)
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        result = self.batch('delete_old_data.bat', input_text='Y\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.project / 'output' / old).exists())
        self.assertEqual(sentinel.read_text(), 'private')

    def test_cleanup_obeys_custom_relative_paths_and_keep_days(self):
        from datetime import date, timedelta
        (self.project / 'config.local.json').write_text('{"out":"data/sources", "logs":"data/reports"}')
        yesterday = (date.today() - timedelta(days=1)).strftime('%Y%m%d')
        today = date.today().strftime('%Y%m%d')
        for day in (yesterday, today):
            (self.project / 'data/sources' / day).mkdir(parents=True)
        batch_path = self.project / 'delete_old_data.bat'
        batch_path.write_text(batch_path.read_text().replace('KEEP_DAYS=30', 'KEEP_DAYS=1'))
        result = self.batch('delete_old_data.bat', input_text='Y\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.project / 'data/sources' / yesterday).exists())
        self.assertTrue((self.project / 'data/sources' / today).exists())

    def test_cleanup_keeps_retention_boundary_and_rejects_auth_in_output(self):
        from datetime import date, timedelta
        today = date.today()
        older = (today - timedelta(days=30)).strftime('%Y%m%d')
        boundary = (today - timedelta(days=29)).strftime('%Y%m%d')
        for day in (older, boundary, today.strftime('%Y%m%d')):
            (self.project / 'output' / day).mkdir(parents=True)
        result = self.batch('delete_old_data.bat', input_text='Y\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.project / 'output' / older).exists())
        self.assertTrue((self.project / 'output' / boundary).exists())
        auth_path = self.project / 'output' / older / 'credentials.json'
        auth_path.parent.mkdir()
        auth_path.write_text('private')
        (self.project / 'config.local.json').write_text(json.dumps(
            {'out':'output', 'logs':'logs', 'credentials':f'output/{older}/credentials.json'}))
        result = self.batch('delete_old_data.bat', input_text='Y\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(auth_path.read_text(), 'private')


if __name__ == "__main__":
    unittest.main()

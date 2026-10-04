"""Generate offline-demo export output with a fake Apps Script API.

Writes output/<today>/offline-demo/... — sample data only, no real IDs,
no network. Run: python tests/make_offline_demo.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gas_export import common, exporter  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TODAY = common.local_today()
DEMO = ROOT / "output" / TODAY / "offline-demo"
DEMO_LOGS = ROOT / "logs" / TODAY / "offline-demo"

SID_1 = "DEMOID" + "1" * 38
SID_2 = "DEMOID" + "2" * 38
SID_3 = "DEMOID" + "3" * 38


class Req:
    def __init__(self, data):
        self._data = data

    def execute(self, num_retries=0):
        return self._data


class FakeProjects:
    META = {
        SID_1: {"title": "Demo Expense Form", "parentId": "demoparent1"},
        SID_2: {"title": "Demo Blank Project", "parentId": "demoparent2"},
        SID_3: {"title": "Demo Broken"},
    }
    CONTENT = {
        SID_1: {"files": [
            {"name": "Code", "type": "SERVER_JS",
             "source": "function onSubmit(e) {\n  // demo only\n  return e;\n}\n"},
            {"name": "form", "type": "HTML", "source": "<html><body>demo</body></html>"},
            {"name": "appsscript", "type": "JSON",
             "source": '{\n  "timeZone": "Asia/Tokyo"\n}'},
        ]},
        SID_2: {"files": [
            {"name": "Code", "type": "SERVER_JS",
             "source": "function myFunction() {\n\n}"},
            {"name": "appsscript", "type": "JSON", "source": "{}"},
        ]},
    }

    def get(self, scriptId):
        if scriptId == SID_3:
            raise RuntimeError("demo simulated API error")
        return Req(self.META[scriptId])

    def getContent(self, scriptId):
        if scriptId == SID_3:
            raise RuntimeError("demo simulated API error")
        return Req(self.CONTENT[scriptId])


class FakeScriptApi:
    def projects(self):
        return FakeProjects()


def main():
    entries = [
        {"scriptId": SID_1, "fileName": "demo-expense-form", "folderName": "demo-folder",
         "fileId": "demofile1", "mimeType": "application/vnd.google-apps.spreadsheet"},
        {"scriptId": SID_2, "fileName": "demo-blank"},
        {"scriptId": SID_3},
        {"scriptId": SID_1},  # duplicate -> deduped by load_entries, shown here pre-dedup
    ]
    summary = exporter.export_projects(
        FakeScriptApi(), entries[:3], DEMO, log=lambda m: print(f"  {m}"), log_dir=DEMO_LOGS)
    print(f"demo written under {DEMO}")
    print(f"success={summary['successCount']} skipped={summary['skippedCount']} "
          f"failures={summary['failureCount']}")
    # also demonstrate the discover-side artifact shape
    common.write_json(DEMO_LOGS / "script-ids.json", [
        {"fileId": "demofile1", "fileName": "demo-expense-form",
         "mimeType": "application/vnd.google-apps.spreadsheet",
         "folderId": "demofolder", "folderName": "demo-folder",
         "scriptId": SID_1,
         "url": f"https://script.google.com/home/projects/{SID_1}"},
    ])
    print("wrote demo script-ids.json (discover output schema)")


if __name__ == "__main__":
    main()

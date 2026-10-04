"""OFFLINE browser regression for the 'visible but undetected' tab bug.

Proves (without any Google/site navigation) that _wait_for_script_page
detects a tab opened late via window.open -- only possible because the
poll loop drives Playwright's sync event dispatch via
page.wait_for_timeout instead of blocking time.sleep.

Standalone (not collected by unittest discovery):
    .venv\\Scripts\\python.exe tests\\browser_offline_repro.py
Exit 0 on success. Requires playwright + local Chrome only.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gas_export.discover as d  # noqa: E402

FAKE_ID = "FAKEOFFLINEID" + "0" * 30
MARKER = "editorprobe"


def main():
    from playwright.sync_api import sync_playwright

    # Map ONLY the probed new tab's marker URL to a fake ID -- no real
    # URLs or IDs anywhere; real script.google.com detection untouched.
    orig = d.script_id_from_url
    d.script_id_from_url = (
        lambda url: FAKE_ID if MARKER in (url or "") else orig(url))

    pw = sync_playwright().start()
    browser = ctx = None
    try:
        browser = pw.chromium.launch(channel="chrome", headless=False)
        ctx = browser.new_context()
        src = ctx.new_page()
        src.goto("about:blank")
        # Delayed popup like the Apps Script menu opening the editor.
        src.evaluate(
            "setTimeout(() => window.open('about:blank?%s'), 1000)" % MARKER)
        result = d._wait_for_script_page(
            ctx, src, set(), deadline_ms=15_000)
        assert result["id"] == FAKE_ID, result
        print(f"OK: new tab detected via event-driven wait "
              f"(id={FAKE_ID!r})")
        return 0
    finally:
        d.script_id_from_url = orig
        try:
            if ctx is not None:
                ctx.close()
        finally:
            if browser is not None:
                browser.close()
            pw.stop()


if __name__ == "__main__":
    raise SystemExit(main())

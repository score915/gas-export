"""OAuth helpers. google-* imports are lazy so offline tests never need them.

Each command uses its own scopes and therefore its own token cache file.
A token whose granted scopes do not cover the required scopes is rejected
rather than silently reused.
"""

import json
from pathlib import Path

from .common import write_text_atomic

SCOPE_DRIVE_METADATA_READONLY = "https://www.googleapis.com/auth/drive.metadata.readonly"
SCOPE_DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"
SCOPE_SCRIPT_PROJECTS_READONLY = "https://www.googleapis.com/auth/script.projects.readonly"


def load_credentials(credentials_file, token_file, scopes, account=None):
    """Return google-auth Credentials for ``scopes``.

    Reuses ``token_file`` only if its granted scopes cover ``scopes``;
    otherwise runs the installed-app browser consent flow and saves the
    resulting token. Raises if a cached token lacks required scopes instead
    of silently using it.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    required = set(scopes)
    token_file = Path(token_file)
    if token_file.is_file():
        # Check the stored scopes BEFORE constructing Credentials: passing
        # scopes= into from_authorized_user_file would overwrite the granted
        # set and make an under-scoped token look sufficient.
        if not credentials_token_has_scopes(token_file, required):
            raise RuntimeError(
                f"token {token_file} lacks required scopes {sorted(required)}; "
                "delete it or point --token at a token issued for these scopes"
            )
        creds = Credentials.from_authorized_user_file(str(token_file))
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            write_text_atomic(token_file, creds.to_json())
        return creds

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), scopes=list(required))
    options = {"login_hint": account, "prompt": "consent"} if account else {"prompt": "select_account"}
    creds = flow.run_local_server(port=0, timeout_seconds=600, **options, authorization_prompt_message="Google認証画面を開きました。対象のGoogleアカウントで同意を完了してください。")
    if creds is None:
        raise TimeoutError("Google認証が10分以内に完了しませんでした")
    token_file.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(token_file, creds.to_json())
    return creds


def build_service(name, version, creds):
    """googleapiclient service (lazy import)."""
    from googleapiclient.discovery import build
    return build(name, version, credentials=creds, cache_discovery=False)


def credentials_token_has_scopes(token_file, scopes):
    """True if the serialized token grants all of ``scopes``."""
    try:
        data = json.loads(Path(token_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    granted = set(data.get("scopes") or data.get("scope", "").split())
    return set(scopes) <= granted

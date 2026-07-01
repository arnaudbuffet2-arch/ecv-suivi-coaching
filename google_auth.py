import json
from pathlib import Path
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

ECV_DIR = Path.home() / ".ecv"
CREDENTIALS_PATH = ECV_DIR / "credentials.json"
TOKEN_PATH = ECV_DIR / "tokens.json"

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


def _reauth():
    """Ouvre le navigateur pour re-authentification OAuth et sauvegarde les nouveaux tokens."""
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=8081)
    TOKEN_PATH.write_text(json.dumps({
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
    }, indent=2))


def get_google_credentials():
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(f"Credentials Google introuvables : {CREDENTIALS_PATH}")

    if not TOKEN_PATH.exists():
        _reauth()

    tokens = json.loads(TOKEN_PATH.read_text())
    cred_data = json.loads(CREDENTIALS_PATH.read_text())
    client_info = cred_data.get("installed") or cred_data.get("web")

    creds = Credentials(
        token=tokens.get("access_token"),
        refresh_token=tokens.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_info["client_id"],
        client_secret=client_info["client_secret"],
        scopes=SCOPES,
    )

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            tokens["access_token"] = creds.token
            TOKEN_PATH.write_text(json.dumps(tokens, indent=2))
        except RefreshError:
            # Token révoqué → re-auth automatique via navigateur
            _reauth()
            return get_google_credentials()

    return creds

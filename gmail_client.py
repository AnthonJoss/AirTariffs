import base64
import os
import re
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

BASE_DIR = Path(__file__).parent
PDF_DIR = BASE_DIR / "pdfs"
TOKEN_PATH = BASE_DIR / "token.json"
CREDENTIALS_PATH = BASE_DIR / "credentials.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

SENDER = "franco@dataservicios.com"
MAX_MAILS = 30


class NotAuthorized(Exception):
    pass


def get_gmail_service(interactive: bool = False):
    """interactive=True abre el navegador para autorizar (solo desde `python gmail_client.py`)."""
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                creds = None
        else:
            creds = None
        if creds is None:
            if not interactive:
                raise NotAuthorized("Gmail no autorizado: ejecuta `python gmail_client.py` una vez.")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
            creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
        TOKEN_PATH.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _header(headers, name):
    return next((h["value"] for h in headers if h["name"].lower() == name.lower()), "")


def _pdf_parts(payload):
    """Yield every PDF attachment part in a message payload (recursive)."""
    for part in payload.get("parts", []) or []:
        filename = part.get("filename", "")
        if filename.lower().endswith(".pdf") and part.get("body", {}).get("attachmentId"):
            yield part
        yield from _pdf_parts(part)


def list_mails(sender: str = SENDER, limit: int = MAX_MAILS):
    service = get_gmail_service()
    res = service.users().messages().list(userId="me", q=f"from:{sender}", maxResults=limit).execute()
    mails = []
    for ref in res.get("messages", []):
        full = service.users().messages().get(userId="me", id=ref["id"], format="full").execute()
        headers = full["payload"]["headers"]
        mails.append({
            "id": ref["id"],
            "subject": _header(headers, "Subject") or "(sin asunto)",
            "from": _header(headers, "From"),
            "date": _header(headers, "Date"),
            "pdfs": [p["filename"] for p in _pdf_parts(full["payload"])],
        })
    return mails


def download_pdfs(message_id: str):
    service = get_gmail_service()
    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    PDF_DIR.mkdir(exist_ok=True)
    saved = []
    for part in _pdf_parts(msg["payload"]):
        att = service.users().messages().attachments().get(
            userId="me", messageId=message_id, id=part["body"]["attachmentId"]
        ).execute()
        name = re.sub(r'[\\/:*?"<>|]', "_", part["filename"])
        dest = PDF_DIR / f"{message_id}_{name}"
        dest.write_bytes(base64.urlsafe_b64decode(att["data"]))
        saved.append(dest.name)
    return saved


if __name__ == "__main__":
    get_gmail_service(interactive=True)
    print("Autorizado. token.json guardado.")

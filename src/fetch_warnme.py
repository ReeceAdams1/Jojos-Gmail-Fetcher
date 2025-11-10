import argparse
import os
import json
from typing import List, Dict, Any
from datetime import datetime

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from dateutil import parser as dtparser

# If modifying these scopes, delete token.json.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
SENDER = "ucberkeley@warnme.berkeley.edu"


def get_service(credentials_path: str = "credentials.json", token_path: str = "token.json"):
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    # Refresh or login if no valid creds
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                print(f"Failed to refresh token, will re-auth: {e}")
                creds = None
        if not creds:
            if not os.path.exists(credentials_path):
                raise FileNotFoundError(
                    f"Missing {credentials_path}. Download OAuth client credentials (Desktop app) from Google Cloud Console and place here."
                )
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        # Save the credentials for the next run
        with open(token_path, "w") as token:
            token.write(creds.to_json())
    service = build("gmail", "v1", credentials=creds)
    return service


def list_message_ids(service, query: str, max_results: int = None) -> List[str]:
    ids: List[str] = []
    page_token = None
    while True:
        params = {"userId": "me", "q": query}
        if page_token:
            params["pageToken"] = page_token
        if max_results:
            params["maxResults"] = min(500, max_results - len(ids))
            if params["maxResults"] <= 0:
                break
        resp = service.users().messages().list(**params).execute()
        for m in resp.get("messages", []):
            ids.append(m["id"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
        if max_results and len(ids) >= max_results:
            break
    return ids


def fetch_message(service, msg_id: str, format_: str = "metadata") -> Dict[str, Any]:
    return service.users().messages().get(userId="me", id=msg_id, format=format_, metadataHeaders=["Subject", "Date", "From", "To"]).execute()


def extract_headers(message: Dict[str, Any]) -> Dict[str, Any]:
    payload = message.get("payload", {})
    headers = payload.get("headers", [])
    header_map = {h["name"].lower(): h["value"] for h in headers}
    date_raw = header_map.get("date")
    date_parsed = None
    if date_raw:
        try:
            date_parsed = dtparser.parse(date_raw)
        except Exception:
            pass
    return {
        "id": message.get("id"),
        "threadId": message.get("threadId"),
        "snippet": message.get("snippet"),
        "subject": header_map.get("subject"),
        "from": header_map.get("from"),
        "to": header_map.get("to"),
        "date_raw": date_raw,
        "date": date_parsed.isoformat() if date_parsed else None,
        "internalDate": message.get("internalDate"),
        "sizeEstimate": message.get("sizeEstimate"),
    }


def save_full(service, msg_id: str, output_dir: str):
    full = service.users().messages().get(userId="me", id=msg_id, format="raw").execute()
    raw_data = full.get("raw")
    if raw_data:
        import base64
        eml_bytes = base64.urlsafe_b64decode(raw_data)
        path = os.path.join(output_dir, f"{msg_id}.eml")
        with open(path, "wb") as f:
            f.write(eml_bytes)
        return path
    return None


def main():
    parser = argparse.ArgumentParser(description="Fetch Gmail messages from ucberkeley@warnme.berkeley.edu")
    parser.add_argument("--credentials", default="credentials.json", help="Path to OAuth client credentials JSON")
    parser.add_argument("--token", default="token.json", help="Path to stored user token JSON")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of messages")
    parser.add_argument("--save", action="store_true", help="Save full raw .eml files to output/ directory")
    parser.add_argument("--json", action="store_true", help="Write a messages.json summary file")
    parser.add_argument("--since", help="Only messages after this ISO date or YYYY-MM-DD")
    parser.add_argument("--until", help="Only messages before this ISO date or YYYY-MM-DD")
    args = parser.parse_args()

    query_parts = [f"from:{SENDER}"]
    def parse_date(dstr: str):
        return dtparser.parse(dstr)
    if args.since:
        since_dt = parse_date(args.since)
        query_parts.append(f"after:{since_dt.strftime('%Y/%m/%d')}")
    if args.until:
        until_dt = parse_date(args.until)
        # Gmail "before" is exclusive; ensure formatting
        query_parts.append(f"before:{until_dt.strftime('%Y/%m/%d')}")
    query = " ".join(query_parts)

    try:
        service = get_service(args.credentials, args.token)
        ids = list_message_ids(service, query=query, max_results=args.limit)
        print(f"Found {len(ids)} messages for query: {query}")
        results = []
        for i, mid in enumerate(ids, 1):
            try:
                meta = fetch_message(service, mid)
            except HttpError as he:
                print(f"Failed to fetch {mid}: {he}")
                continue
            info = extract_headers(meta)
            results.append(info)
            print(f"[{i}/{len(ids)}] {info.get('date')} | {info.get('subject')} | {info.get('id')}")
        if args.save or args.json:
            os.makedirs("output", exist_ok=True)
        if args.save:
            for mid in ids:
                path = save_full(service, mid, "output")
                if path:
                    print(f"Saved raw email to {path}")
        if args.json:
            with open(os.path.join("output", "messages.json"), "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2)
            print("Wrote output/messages.json")
    except FileNotFoundError as fe:
        print(str(fe))
        print("Setup instructions: run 'python src/fetch_warnme.py' after placing credentials.json (Desktop OAuth). First run opens browser for consent.")
    except HttpError as error:
        print(f"An HTTP error occurred: {error}")


if __name__ == "__main__":
    main()

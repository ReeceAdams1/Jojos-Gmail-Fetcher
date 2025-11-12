import argparse
import os
import json
import csv
import re
import base64
import quopri
from typing import List, Dict, Any, Optional, Tuple
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
    """Fetch a Gmail message.
    format_ can be: 'metadata', 'full', 'raw'. We default to metadata for speed.
    """
    return service.users().messages().get(
        userId="me",
        id=msg_id,
        format=format_,
        metadataHeaders=["Subject", "Date", "From", "To"],
    ).execute()


def extract_headers(message: Dict[str, Any]) -> Dict[str, Any]:
    headers = {h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])}
    date_raw = headers.get("date")
    try:
        date_iso = dtparser.parse(date_raw).isoformat() if date_raw else None
    except Exception:
        date_iso = None
    return {
        "id": message.get("id"),
        "threadId": message.get("threadId"),
        "snippet": message.get("snippet"),
        "subject": headers.get("subject"),
        "from": headers.get("from"),
        "to": headers.get("to"),
        "date_raw": date_raw,
        "date": date_iso,
        "internalDate": message.get("internalDate"),
        "sizeEstimate": message.get("sizeEstimate"),
    }


def save_full(service, msg_id: str, output_dir: str):
    raw_data = service.users().messages().get(userId="me", id=msg_id, format="raw").execute().get("raw")
    if not raw_data:
        return None
    path = os.path.join(output_dir, f"{msg_id}.eml")
    with open(path, "wb") as f:
        f.write(base64.urlsafe_b64decode(raw_data))
    return path


def _collect_parts(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    parts = []
    if not payload:
        return parts
    if payload.get("parts"):
        for p in payload.get("parts", []):
            parts.extend(_collect_parts(p))
    else:
        parts.append(payload)
    return parts


def extract_body_text(full_message: Dict[str, Any]) -> str:
    """Extract a readable body from a 'full' format Gmail message.
    Prefers text/plain; falls back to stripped text/html; else snippet.
    """
    payload = full_message.get("payload", {})
    parts = _collect_parts(payload)
    text_chunks: List[str] = []
    html_chunks: List[str] = []
    for part in parts:
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if not data:
            continue
        try:
            decoded_raw = base64.urlsafe_b64decode(data)
            decoded = decoded_raw.decode("utf-8", errors="replace")
        except Exception:
            continue
        # Heuristic: quoted-printable parts often contain =\n or soft line breaks with '='
        if re.search(r"=\r?\n", decoded) or re.search(r"=[0-9A-Fa-f]{2}", decoded):
            try:
                decoded = quopri.decodestring(decoded_raw).decode("utf-8", errors="replace")
            except Exception:
                pass
        if mime.startswith("text/plain"):
            text_chunks.append(decoded.strip())
        elif mime.startswith("text/html"):
            html_chunks.append(decoded)
    if text_chunks:
        return "\n\n".join(text_chunks)
    if html_chunks:
        # naive HTML tag stripping
        combined = "\n\n".join(html_chunks)
        # Remove script/style
        combined = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", combined, flags=re.DOTALL | re.IGNORECASE)
        # Remove tags
        combined = re.sub(r"<[^>]+>", " ", combined)
        # Collapse whitespace
        combined = re.sub(r"\s+", " ", combined)
        return combined.strip()
    return full_message.get("snippet", "")


LOCATION_PATTERNS = [
    re.compile(r"(?i)^location[:\-]\s*(.+)$"),
    re.compile(r"(?i)^area[:\-]\s*(.+)$"),
    re.compile(r"(?i)^campus[:\-]\s*(.+)$"),
]

# Crime type pattern examples:
# UC Berkeley WarnMe: Robbery Reported. Some May Find the Content Upsetting.
# UC Berkeley WarnMe - Arson Reported. Some May Find the Content Upsetting.
# UC Berkeley WarnMe: Sexual Battery Reported. Some May Find the Content Upsetting.
# UC Berkeley WarnMe: Violent Crime Reported. Some May Find the Content Upsetting.
CRIME_PATTERN = re.compile(
    r"UC Berkeley WarnMe(?:\s*[:\-])\s*(?P<crime>.+?)\.\s+Some May Find the Content Upsetting\." , re.IGNORECASE
)

def extract_crime_type(subject: str) -> Optional[str]:
    """Extract the crime type from the standardized WarnMe subject line.
    Strips trailing 'Reported' or 'Report' and normalizes spacing.
    Returns None if pattern not matched.
    """
    if not subject:
        return None
    m = CRIME_PATTERN.search(subject)
    if not m:
        return None
    crime = m.group("crime").strip()
    # Remove trailing 'Reported' variants
    crime = re.sub(r"(?i)\bReported\b", "", crime).strip()
    crime = re.sub(r"(?i)\bReport\b", "", crime).strip()
    # Collapse multiple spaces
    crime = re.sub(r"\s+", " ", crime)
    return crime or None


def extract_location(body: str) -> Optional[str]:
    """Extract location from explicit labeled lines or narrative 'occurred ...' clause.

    Handles patterns like:
      occurred in the area of Oxford Street & Hearst Ave in the City of Berkeley.
      occurred at Lower Sproul Plaza.
      occurred near Memorial Glade.
    Returns the location portion without trailing period and without the leading qualifier.
    """
    # Explicit labeled lines
    for line in body.splitlines():
        for pat in LOCATION_PATTERNS:
            m = pat.match(line.strip())
            if m:
                return m.group(1).strip().rstrip('.')
    # Narrative patterns following 'occurred'
    patterns = [
        re.compile(r"occurred\s+in\s+the\s+area\s+of\s+(.+?)\.", re.IGNORECASE),
        re.compile(r"occurred\s+at\s+(.+?)\.", re.IGNORECASE),
        re.compile(r"occurred\s+near\s+(.+?)\.", re.IGNORECASE),
    ]
    for pat in patterns:
        m = pat.search(body)
        if m:
            return m.group(1).strip().rstrip('.')
    return None


INCIDENT_PATTERN = re.compile(
    r"On\s+(?P<date>\d{1,2}/\d{1,2}/\d{2})(?P<comma>,?)\s+at\s+(?:(?P<qualifier>approximately|about)\s+)?(?P<time>(?:\d{3,4}|\d{1,2}:\d{2}\s*(?:am|pm|AM|PM)))(?:\s*hours)?",
    re.IGNORECASE,
)


def _convert_time_to_24_and_12(raw_time: str) -> Tuple[Optional[str], Optional[str]]:
    """Given a raw time token (HHMM, HMM, or h:mm am/pm), return (24h HH:MM, 12h h:MM am/pm)."""
    raw = raw_time.strip()
    # If already has a colon assume maybe 12h with am/pm
    if ":" in raw:
        # Normalize spacing
        m = re.match(r"^(\d{1,2}):(\d{2})\s*(am|pm|AM|PM)?$", raw)
        if not m:
            return None, None
        hh = int(m.group(1))
        mm = m.group(2)
        ampm = m.group(3)
        if ampm:
            ampm_lower = ampm.lower()
            if ampm_lower == "pm" and hh != 12:
                hh24 = hh + 12
            elif ampm_lower == "am" and hh == 12:
                hh24 = 0
            else:
                hh24 = hh
            time_24 = f"{hh24:02d}:{mm}"
            time_12 = f"{hh}:{mm} {ampm.lower()}"
            return time_24, time_12
        # No am/pm supplied, treat as 24h
        if hh < 24:
            time_24 = f"{hh:02d}:{mm}"
            # Convert to 12h
            if hh == 0:
                time_12 = f"12:{mm} am"
            elif hh == 12:
                time_12 = f"12:{mm} pm"
            elif hh > 12:
                time_12 = f"{hh-12}:{mm} pm"
            else:
                time_12 = f"{hh}:{mm} am"
            return time_24, time_12
        return None, None
    # Digits only (HHMM or HMM)
    if raw.isdigit() and 3 <= len(raw) <= 4:
        raw_padded = raw.zfill(4)
        hh = int(raw_padded[:2])
        mm = raw_padded[2:]
        if hh > 23:
            return None, None
        time_24 = f"{hh:02d}:{mm}"
        # Build 12h
        if hh == 0:
            time_12 = f"12:{mm} am"
        elif hh == 12:
            time_12 = f"12:{mm} pm"
        elif hh > 12:
            time_12 = f"{hh-12}:{mm} pm"
        else:
            time_12 = f"{hh}:{mm} am"
        return time_24, time_12
    return None, None


MONTH_DAY_PATTERN = re.compile(
    r"On\s+(?P<month>January|February|March|April|May|June|July|August|September|October|November|December|Jan\.?|Feb\.?|Mar\.?|Apr\.?|Jun\.?|Jul\.?|Aug\.?|Sep\.?|Sept\.?|Oct\.?|Nov\.?|Dec\.?)\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,\s*(?P<year>\d{4}))?(?:,)?"
    r"(?:\s+at\s+(?:(?P<qual>approximately|about)\s+)?(?P<time>(?:\d{3,4}|\d{1,2}:\d{2}\s*(?:am|pm|AM|PM)))(?:\s*hours)?)?",
    re.IGNORECASE,
)

def extract_incident_datetime(body: str, email_iso: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """Return (incident_date_iso, incident_time_24h). Supports numeric mm/dd/yy and month name forms."""
    m = INCIDENT_PATTERN.search(body)
    if m:
        try:
            date_iso = datetime.strptime(m.group("date"), "%m/%d/%y").date().isoformat()
        except Exception:
            date_iso = None
        time_24, _ = _convert_time_to_24_and_12(m.group("time"))
        return date_iso, time_24
    m2 = MONTH_DAY_PATTERN.search(body)
    if not m2:
        return None, None
    month_raw = m2.group("month").lower().rstrip('.')
    month_map = {"january":1,"jan":1,"february":2,"feb":2,"march":3,"mar":3,"april":4,"apr":4,"may":5,"june":6,"jun":6,"july":7,"jul":7,"august":8,"aug":8,"september":9,"sep":9,"sept":9,"october":10,"oct":10,"november":11,"nov":11,"december":12,"dec":12}
    month = month_map.get(month_raw)
    try:
        day = int(m2.group("day"))
    except Exception:
        return None, None
    year_token = m2.group("year")
    if year_token and year_token.isdigit():
        year = int(year_token)
    else:
        # Infer from email timestamp if possible
        year = None
        if email_iso:
            try:
                year = dtparser.parse(email_iso).year
            except Exception:
                year = None
        if year is None:
            year = datetime.now().year
    date_iso = f"{year:04d}-{month:02d}-{day:02d}" if month else None
    raw_time = m2.group("time")
    time_24 = None
    if raw_time:
        time_24, _ = _convert_time_to_24_and_12(raw_time)
    return date_iso, time_24


def main():
    parser = argparse.ArgumentParser(description="Fetch Gmail messages from ucberkeley@warnme.berkeley.edu")
    parser.add_argument("--credentials", default="credentials.json", help="Path to OAuth client credentials JSON")
    parser.add_argument("--token", default="token.json", help="Path to stored user token JSON")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of messages")
    parser.add_argument("--save", action="store_true", help="Save full raw .eml files to output/ directory")
    parser.add_argument("--json", action="store_true", help="Write a messages.json summary file")
    parser.add_argument("--csv", nargs="?", const="output/messages.csv", help="Write extracted fields to CSV (optionally provide path)")
    parser.add_argument("--latest-text", nargs="?", const="output/latest_warnme.txt", help="Write the most recent email body to a text file (optionally provide path)")
    parser.add_argument("--since", help="Only messages after this ISO date or YYYY-MM-DD")
    parser.add_argument("--until", help="Only messages before this ISO date or YYYY-MM-DD")
    args = parser.parse_args()

    query_parts = [f"from:{SENDER}"]
    if args.since:
        since_dt = dtparser.parse(args.since)
        query_parts.append(f"after:{since_dt.strftime('%Y/%m/%d')}")
    if args.until:
        until_dt = dtparser.parse(args.until)
        # Gmail "before" is exclusive; ensure formatting
        query_parts.append(f"before:{until_dt.strftime('%Y/%m/%d')}")
    query = " ".join(query_parts)

    try:
        service = get_service(args.credentials, args.token)
        ids = list_message_ids(service, query=query, max_results=args.limit)
        print(f"Found {len(ids)} messages for query: {query}")

        need_full = bool(args.csv or args.latest_text)
        meta_results: List[Dict[str, Any]] = []
        csv_rows: List[Dict[str, Any]] = []

        for i, mid in enumerate(ids, 1):
            try:
                msg = fetch_message(service, mid, format_="full" if need_full else "metadata")
            except HttpError as he:
                print(f"Failed to fetch {mid}: {he}")
                continue
            info = extract_headers(msg)
            subj = (info.get("subject") or "").strip()
            # Skip Community Advisory and Critical Alert subjects entirely
            if re.search(r"(?i)\bCommunity Advisory\b", subj):
                print(f"Skipping Community Advisory message id {info.get('id')}")
                continue
            if re.search(r"(?i)\bCritical Alert\b", subj):
                print(f"Skipping Critical Alert message id {info.get('id')}")
                continue
            meta_results.append(info)
            body_text = extract_body_text(msg) if need_full else ""
            incident_date, incident_time = extract_incident_datetime(body_text, info.get("date")) if need_full else (None, None)
            location = extract_location(body_text) if need_full else None
            if need_full:
                original_subject = subj
                crime = extract_crime_type(original_subject)
                # Derive incident_date (MM/DD/YY) from ISO date or first mm/dd/yy occurrence
                date_formatted = ""
                if incident_date:
                    try:
                        dt_iso = datetime.strptime(incident_date, "%Y-%m-%d")
                        date_formatted = dt_iso.strftime("%m/%d/%y")
                    except Exception:
                        # Fallback: try to extract mm/dd/yy from body text directly
                        m = re.search(r"\b(\d{1,2}/\d{1,2}/\d{2})\b", body_text)
                        if m:
                            mm, dd, yy = m.group(1).split("/")
                            date_formatted = f"{mm.zfill(2)}/{dd.zfill(2)}/{yy}"
                csv_rows.append({
                    "email_timestamp": info.get("date") or "",
                    "incident_date": date_formatted,
                    "incident_time": incident_time or "",
                    "location": location or "",
                    "subject": crime if crime else original_subject,
                })
            print(f"[{i}/{len(ids)}] {info.get('date')} | {info.get('subject')} | {info.get('id')}")

        if args.save or args.json or args.csv or args.latest_text:
            os.makedirs("output", exist_ok=True)

        if args.save:
            for mid in ids:
                path = save_full(service, mid, "output")
                if path:
                    print(f"Saved raw email to {path}")

        if args.json:
            with open(os.path.join("output", "messages.json"), "w", encoding="utf-8") as f:
                json.dump(meta_results, f, indent=2)
            print("Wrote output/messages.json")

        if args.csv:
            csv_path = args.csv if args.csv != "" else "output/messages.csv"
            fieldnames = ["email_timestamp", "incident_date", "incident_time", "location", "subject"]
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for row in csv_rows:
                    writer.writerow(row)
            print(f"Wrote CSV {csv_path} ({len(csv_rows)} rows)")

        if args.latest_text and ids:
            # Determine first non-Community Advisory message
            chosen_id = None
            for candidate in ids:
                c_msg = fetch_message(service, candidate, format_="metadata")
                c_subj = ""
                try:
                    c_subj = next((h["value"] for h in c_msg.get("payload", {}).get("headers", []) if h["name"].lower()=="subject"), "")
                except Exception:
                    c_subj = ""
                if re.search(r"(?i)\bCommunity Advisory\b", c_subj or ""):
                    continue
                if re.search(r"(?i)\bCritical Alert\b", c_subj or ""):
                    continue
                chosen_id = candidate
                break
            if chosen_id:
                msg_full = fetch_message(service, chosen_id, format_="full")
                info = extract_headers(msg_full)
                body_text = extract_body_text(msg_full)
                incident_date, incident_time = extract_incident_datetime(body_text, info.get('date'))
                location = extract_location(body_text)
                crime = extract_crime_type(info.get('subject') or '')
                header_lines = [
                    f"Crime: {crime if crime else (info.get('subject') or '')}",
                    f"Email Timestamp: {info.get('date') or ''}",
                    f"Incident Date: {incident_date or ''}",
                    f"Incident Time: {incident_time or ''}",
                    f"Location: {location or ''}",
                    "",
                ]
                text_path = args.latest_text if args.latest_text != "" else "output/latest_warnme.txt"
                with open(text_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(header_lines))
                    f.write(body_text.strip())
                print(f"Wrote latest email text to {text_path}")
            else:
                print("No non-Community Advisory messages found for latest-text export.")
    except FileNotFoundError as fe:
        print(str(fe))
        print("Setup instructions: run 'python src/fetch_warnme.py' after placing credentials.json (Desktop OAuth). First run opens browser for consent.")
    except HttpError as error:
        print(f"An HTTP error occurred: {error}")


if __name__ == "__main__":
    main()

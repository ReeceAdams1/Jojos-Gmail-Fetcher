# Jojo's Gmail Fetcher

Fetch Gmail messages from a specific sender (ucberkeley@warnme.berkeley.edu) using the official Gmail API with OAuth.

## What you need to provide

- A Google account with Gmail.
- One-time setup in Google Cloud Console (takes ~5 minutes):
  1. Create a project (or reuse one).
  2. Enable the Gmail API for that project.
  3. Create OAuth 2.0 Client ID of type "Desktop app".
  4. Download the client secrets file and save it as `credentials.json` in this project root.
- Python 3.9+ installed on your machine.

On first run, a browser window will open asking you to sign in and grant read-only access to your Gmail. A `token.json` file will be stored locally so you don't have to re-auth again.

> Important: `credentials.json` and `token.json` are ignored by Git via `.gitignore` so you won’t accidentally commit them.

## Quick start (Windows PowerShell)

```powershell
# 1) (Optional) Create & activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2) Install dependencies
pip install -r requirements.txt

# 3) Place your downloaded OAuth client file here as credentials.json
#    (from Google Cloud Console > APIs & Services > Credentials)

# 4) Run the fetcher
python .\src\fetch_warnme.py --json --save
```

The first run opens a browser to authorize. After success, you'll see:
- `output/messages.json`: summary (id, subject, date, from, snippet)
- `output/*.eml`: raw RFC822 emails (if you passed `--save`)

## Command options

```text
usage: fetch_warnme.py [-h] [--credentials CREDENTIALS] [--token TOKEN]
                       [--limit LIMIT] [--save] [--json]
                       [--since SINCE] [--until UNTIL]

Fetch Gmail messages from ucberkeley@warnme.berkeley.edu

options:
  -h, --help            show this help message and exit
  --credentials CREDENTIALS
                        Path to OAuth client credentials JSON (default: credentials.json)
  --token TOKEN         Path to stored user token JSON (default: token.json)
  --limit LIMIT         Limit number of messages to fetch
  --save                Save full raw .eml files to output/ directory
  --json                Write a messages.json summary file
  --since SINCE         Only messages after this date (YYYY-MM-DD or ISO)
  --until UNTIL         Only messages before this date (YYYY-MM-DD or ISO)
```

Date filters use Gmail search:
- `--since 2025-01-01` becomes `after:2025/01/01`
- `--until 2025-12-31` becomes `before:2025/12/31`

## Alternate approach: IMAP with App Password (optional)

If you prefer IMAP instead of the Gmail API, you must have 2-Step Verification enabled and create an App Password for "Mail". Then you can use standard IMAP libraries. This repo focuses on the official Gmail API method (more secure and reliable). If you want the IMAP variant added here, let me know and I’ll include a script.

## Troubleshooting

- Missing `credentials.json`: Create OAuth client (Desktop) and place it in the project root.
- Consent screen: If using a non-published app in Google Cloud, you may see "unverified app"—click through the advanced link for your own account.
- 403 or quota errors: Rare for personal use; try again later or enable API in the correct project.
- Wrong Google account in browser: Use the account that matches your mailbox.

## Notes

- Scope used: `https://www.googleapis.com/auth/gmail.readonly` (read-only).
- You can change the sender address in the script by editing the `SENDER` constant.
- The script paginates to collect all messages unless `--limit` is set.

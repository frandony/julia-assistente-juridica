"""
Run once locally to authorize Google Calendar access and get a refresh token.
Requires: pip install google-auth-oauthlib

Usage:
    python setup_google_oauth.py

Then add the printed values to Modal secrets:
  GOOGLE_CLIENT_ID
  GOOGLE_CLIENT_SECRET
  GOOGLE_REFRESH_TOKEN
"""

import json
import glob
import os

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("Installing google-auth-oauthlib...")
    os.system("pip install google-auth-oauthlib")
    from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Find the client_secret JSON in current directory
files = glob.glob("client_secret_*.json")
if not files:
    raise FileNotFoundError("No client_secret_*.json file found in current directory.")

client_secrets_file = files[0]
print(f"Using: {client_secrets_file}\n")

flow = InstalledAppFlow.from_client_secrets_file(client_secrets_file, SCOPES)
creds = flow.run_local_server(port=0)

print("\n=== Add these 3 values to Modal secrets (marina-secrets) ===\n")
print(f"GOOGLE_CLIENT_ID     = {creds.client_id}")
print(f"GOOGLE_CLIENT_SECRET = {creds.client_secret}")
print(f"GOOGLE_REFRESH_TOKEN = {creds.refresh_token}")
print("\n============================================================")

"""
Local Zerodha Kite Connect login helper for Windows.

Captures the short-lived request_token on http://127.0.0.1:8000,
exchanges it for an access token, and saves only api_key/access_token to:
  %USERPROFILE%\\.kiteconnect\\session.json

The API secret is requested via hidden input and is never written to disk.
Do not share the session file. Access tokens are sensitive.
"""

import getpass
import json
import logging
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

from kiteconnect import KiteConnect

REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 8000
IST = ZoneInfo("Asia/Kolkata")
received = {}


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        received["request_token"] = params.get("request_token", [None])[0]
        received["status"] = params.get("status", [""])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if received["request_token"] and received["status"] == "success":
            body = b"<h2>Kite login received.</h2><p>You may close this browser tab and return to Command Prompt.</p>"
        else:
            body = b"<h2>Kite login was not completed.</h2><p>Return to Command Prompt and try again.</p>"
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("\nKITE CONNECT LOCAL LOGIN — PAPER TRADING SETUP")
    print("This helper does not place or modify orders.\n")
    api_key = input("Paste your API KEY (not the secret): ").strip()
    if not api_key:
        raise SystemExit("API key is required.")
    api_secret = getpass.getpass("Enter API SECRET (input is hidden): ").strip()
    if not api_secret:
        raise SystemExit("API secret is required.")

    kite = KiteConnect(api_key=api_key)
    server = HTTPServer((REDIRECT_HOST, REDIRECT_PORT), CallbackHandler)
    server.timeout = 240

    login_url = kite.login_url()
    print("\nOpening the official Zerodha login page in your browser...")
    print("Complete login and 2FA. Waiting up to 4 minutes for the local callback.")
    print("If the browser reports a connection error, return here and check that")
    print("the app Redirect URL is exactly http://127.0.0.1:8000")
    webbrowser.open(login_url)

    server.handle_request()
    server.server_close()

    request_token = received.get("request_token")
    if received.get("status") != "success" or not request_token:
        raise SystemExit("No successful request_token callback received. Run this helper again.")

    session = kite.generate_session(request_token, api_secret=api_secret)
    target_dir = Path.home() / ".kiteconnect"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / "session.json"
    payload = {
        "api_key": api_key,
        "access_token": session["access_token"],
        "user_id": session.get("user_id", ""),
        "generated_at_ist": datetime.now(IST).isoformat(timespec="seconds")
    }
    target_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("\nAuthentication successful.")
    print(f"Session saved locally to: {target_file}")
    print("The API secret was not saved.")
    print("Keep this session file private. The access token normally expires at 6 AM next day.")


if __name__ == "__main__":
    main()

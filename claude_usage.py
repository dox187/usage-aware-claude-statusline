#!/usr/bin/env python3
"""Fetches Claude Code's current usage/rate-limit data (stdlib only).

Usage:
  - as a CLI:    python claude_usage.py   -> raw JSON to stdout
  - as a module: from claude_usage import fetch_usage; data = fetch_usage()

Token source, in order of precedence:
  1) CLAUDE_CODE_OAUTH_TOKEN env variable
  2) macOS login Keychain ("Claude Code-credentials") — macOS only, auto-detected
  3) ~/.claude/.credentials.json  (claudeAiOauth.accessToken) — Linux / headless

On macOS we read the Keychain via /usr/bin/security; the Keychain sees this binary
as the accessor (not python). Since the statusline is launched by Claude Code (a
trusted app), it runs without a prompt. The token is NEVER written to a file.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"
# The usage endpoint expects Claude Code-like headers (a generic User-Agent
# may trigger a 429). The version can be overridden via the CLAUDE_CODE_UA env variable.
USER_AGENT = os.environ.get("CLAUDE_CODE_UA", "claude-code/2.0.0")
ANTHROPIC_BETA = "oauth-2025-04-20"


def token_from_credentials():
    """(token, expires_at_ms) from the credentials file, or (None, None)."""
    path = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None, None
    oauth = data.get("claudeAiOauth") or data.get("claude_ai_oauth") or {}
    return oauth.get("accessToken"), oauth.get("expiresAt")


def token_from_keychain():
    """(token, expires_at_ms) from the macOS login Keychain, or (None, None).

    We invoke /usr/bin/security as a subprocess: the Keychain sees this binary
    as the accessor (not python), and -w returns ONLY the secret to stdout.
    Runs on macOS only; any error/rejection -> (None, None).
    """
    if sys.platform != "darwin":
        return None, None
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    cmd = ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE]
    if user:
        cmd += ["-a", user]
    cmd += ["-w"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    if out.returncode != 0 or not out.stdout.strip():
        return None, None
    try:
        oauth = json.loads(out.stdout).get("claudeAiOauth") or {}
        return oauth.get("accessToken"), oauth.get("expiresAt")
    except (ValueError, AttributeError):
        return None, None


def get_token():
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if tok:
        return tok
    tok, _ = token_from_keychain()      # macOS: login Keychain (auto-detected)
    if tok:
        return tok
    tok, _ = token_from_credentials()   # Linux / headless-macOS fallback
    return tok


def fetch_usage(timeout=20):
    """Fetches and returns the /api/oauth/usage JSON response (dict).

    Raises RuntimeError if there is no token; re-raises urllib errors.
    """
    token = get_token()
    if not token:
        raise RuntimeError("No OAuth token (CLAUDE_CODE_OAUTH_TOKEN or credentials).")
    req = urllib.request.Request(URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": ANTHROPIC_BETA,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def main() -> int:
    token = get_token()
    if not token:
        print("No token. Set the CLAUDE_CODE_OAUTH_TOKEN env variable, "
              "or sign in with Claude Code.", file=sys.stderr)
        return 2
    _, expires_at = token_from_credentials()
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") and expires_at and expires_at / 1000 < time.time():
        print("Warning: the access token has probably expired. "
              "Start Claude Code to refresh it, or run 'claude setup-token'.",
              file=sys.stderr)
    try:
        data = fetch_usage()
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"Network error: {e.reason}", file=sys.stderr)
        return 1

    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

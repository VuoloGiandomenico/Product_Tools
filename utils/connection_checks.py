"""Shared Jira / Confluence connection checks (no Streamlit dependency)."""
from __future__ import annotations

import requests

from utils.jira_helpers import get_jira_auth_headers


def confluence_base_url(server: str) -> str:
    """Normalise a Confluence base URL (Atlassian Cloud needs the /wiki suffix)."""
    base_url = (server or "").strip().rstrip("/")
    if "atlassian.net" in base_url and not base_url.endswith("/wiki"):
        base_url += "/wiki"
    return base_url


def check_jira_connection(server: str, token: str, auth_method: str, email: str,
                          timeout: float = 5) -> tuple[bool, str]:
    """Call /rest/api/2/myself. Returns (ok, short status message)."""
    if not (server and token):
        return False, "Server URL or Token empty"
    headers, auth = get_jira_auth_headers(token, auth_method, email or "")
    try:
        resp = requests.get(f"{server.strip().rstrip('/')}/rest/api/2/myself",
                            headers=headers, auth=auth, timeout=timeout)
    except requests.RequestException as e:
        return False, f"Connection Failed ({e})"
    if resp.status_code == 200:
        try:
            user_name = resp.json().get("displayName", "User")
        except ValueError:
            user_name = "User"
        return True, f"Connected as `{user_name}`"
    return False, f"Auth Failed (HTTP {resp.status_code})"


def check_confluence_connection(server: str, token: str, auth_method: str, email: str,
                                timeout: float = 5) -> tuple[bool, str]:
    """Call /rest/api/content?limit=1. Returns (ok, short status message)."""
    if not (server and token):
        return False, "Server URL or Token empty"
    headers, auth = get_jira_auth_headers(token, auth_method, email or "")
    try:
        resp = requests.get(f"{confluence_base_url(server)}/rest/api/content",
                            params={"limit": 1}, headers=headers, auth=auth, timeout=timeout)
    except requests.RequestException as e:
        return False, f"Connection Failed ({e})"
    if resp.status_code == 200:
        return True, "Connected successfully"
    return False, f"Auth Failed (HTTP {resp.status_code})"


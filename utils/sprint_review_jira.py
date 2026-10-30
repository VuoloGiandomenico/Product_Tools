"""Jira ingestion for the Sprint Review workbook (pure logic, no Streamlit).

The Sprint Review dataset differs from the Quarterly Planner one in
``utils.jira_helpers.fetch_jira_tickets_dataset``: it adds the workbook columns
(Outlook, Sprint Review, Release Notes, Demo, Type), normalises fix versions and
resolves epic summaries. Errors are raised as ``JiraFetchError`` so the page decides
how to display them.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping

import pandas as pd
import requests

from utils.jira_helpers import get_jira_auth_headers, map_jira_status
from utils.pdf_helpers import extract_numeric_version

logger = logging.getLogger(__name__)

ALLOWED_JIRA_TYPES = ("Bug", "Epic", "Improvement", "Story", "Task", "Sub-task", "Technical Sub-task")
EPIC_LINK_FIELDS = ("customfield_10014", "customfield_10000", "customfield_10008", "customfield_10009")
SEARCH_FIELDS = ("key,summary,status,fixVersions,parent,customfield_10000,customfield_10008,"
                 "customfield_10009,customfield_10014,assignee,issuetype,labels")
# Some corporate SSO gateways only let browser-like agents through.
BROWSER_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Ingestion checkbox (session_state key) -> Jira issue types it enables.
ISSUE_TYPE_FLAGS = {
    "inc_story": ("Story",),
    "inc_bug": ("Bug",),
    "inc_task": ("Task", "Sub-task"),
    "inc_tech": ("Task", "Epic"),
    "inc_subtask": ("Sub-task", "Technical Sub-task"),
    "inc_improvement": ("Improvement",),
}


class JiraFetchError(Exception):
    """Jira could not be queried. ``body_preview`` holds the start of a non-JSON body."""

    def __init__(self, message: str, body_preview: str | None = None):
        super().__init__(message)
        self.message = message
        self.body_preview = body_preview


def selected_issue_types(flags: Mapping[str, object]) -> list[str]:
    """Jira issue types enabled by the ingestion checkboxes (sorted, de-duplicated)."""
    if flags.get("inc_all", False):
        return sorted(ALLOWED_JIRA_TYPES)
    types: set[str] = set()
    for flag, jira_types in ISSUE_TYPE_FLAGS.items():
        if flags.get(flag):
            types.update(jira_types)
    return sorted(t for t in types if t in ALLOWED_JIRA_TYPES)


def build_sprint_jql(query_val: str, is_sprint: bool, only_unresolved: bool,
                     issue_types: Iterable[str]) -> str:
    """JQL for a sprint (id or name) restricted to the given issue types, or a raw JQL."""
    if not is_sprint:
        return query_val
    query_val = str(query_val).strip()
    jql = f"sprint = {query_val}" if query_val.isdigit() else f"sprint = '{query_val}'"
    if only_unresolved:
        jql += " AND resolution is empty"
    types = list(issue_types)
    if types:
        jql += " AND issuetype in (" + ", ".join(f"'{t}'" for t in types) + ")"
    else:
        jql += " AND issuetype in ('')"  # nothing selected -> no results
    return jql


def classify_issue_type(raw_name: str, include_all_types: bool = False) -> str:
    """Map a Jira issue type name onto the workbook's type vocabulary."""
    raw = (raw_name or "Task").lower()
    if ("technical sub-task" in raw or "tech sub-task" in raw or "technical subtask" in raw
            or "tech subtask" in raw or ("sub-task" in raw and ("tech" in raw or "technical" in raw))):
        return "Technical Sub-task"
    if "story" in raw:
        return "User Story"
    if "bug" in raw:
        return "Bug"
    if "improvement" in raw:
        return "Improvement"
    if any(word in raw for word in ("technical", "tech", "performance", "scaling", "infrastructure")):
        return "Technical Task"
    if "sub-task" in raw or "subtask" in raw:
        return "Sub-task"
    if "task" in raw:
        return "Task"
    return raw_name if include_all_types else "Technical Task"


def _epic_for(fields: Mapping) -> str:
    epic_key = None
    for custom_field in EPIC_LINK_FIELDS:
        cf_val = fields.get(custom_field)
        if cf_val:
            epic_key = (cf_val.get("key") or str(cf_val)) if isinstance(cf_val, dict) else str(cf_val)
            break
    parent = fields.get("parent")
    parent_summary = None
    if parent:
        parent_summary = (parent.get("fields") or {}).get("summary")
        if not epic_key:
            epic_key = parent.get("key")
    if not epic_key:
        return "-"
    if parent and epic_key == parent.get("key") and parent_summary:
        return f"{epic_key} - {parent_summary}"
    return epic_key


def issue_to_row(issue: Mapping, include_raw_status: bool = False,
                 include_all_types: bool = False) -> dict:
    """Convert one Jira search result into a Sprint Review workbook row."""
    fields = issue.get("fields", {}) or {}
    status_obj = fields.get("status") or {}
    status = map_jira_status(status_obj.get("name", "To Do"))
    res_name = (fields.get("resolution") or {}).get("name")
    if res_name:
        status = f"{status} [{res_name}]"
    fix_versions = fields.get("fixVersions") or []
    raw_fix_version = ", ".join(v.get("name", "") for v in fix_versions)
    assignee_obj = fields.get("assignee")
    labels = fields.get("labels", [])
    row = {
        "Key": issue.get("key", "N/A"),
        "Summary": fields.get("summary", "Untitled Task"),
        "Epic": _epic_for(fields),
        "Status": status,
        "Fix Version": extract_numeric_version(raw_fix_version),
        "Outlook": "",
        "Sprint Review": True,
        "Release Notes": True,
        "Assignee": assignee_obj.get("displayName", "Unassigned") if assignee_obj else "Unassigned",
        "Demo": False,
        "Type": classify_issue_type((fields.get("issuetype") or {}).get("name", "Task"), include_all_types),
        "Labels": ", ".join(labels) if isinstance(labels, list) else "",
    }
    if include_raw_status:
        row["Raw Status"] = status_obj.get("name", "To Do")
    return row


def _resolve_epic_summaries(rows: list[dict], url: str, headers: dict, auth) -> None:
    """Replace bare epic keys with 'KEY - Summary' (best effort; failures are logged)."""
    keys = {r["Epic"] for r in rows if r["Epic"] != "-" and " - " not in r["Epic"]}
    if not keys:
        return
    params = {"jql": "key in (" + ",".join(f"'{k}'" for k in sorted(keys)) + ")",
              "fields": "key,summary", "maxResults": 100}
    try:
        resp = requests.get(url, headers=headers, params=params, auth=auth, timeout=10)
        if resp.status_code != 200:
            logger.warning("Epic summary lookup failed (HTTP %s)", resp.status_code)
            return
        epic_map = {}
        for epic_issue in resp.json().get("issues", []):
            e_key = epic_issue.get("key")
            e_summary = (epic_issue.get("fields") or {}).get("summary")
            if e_key and e_summary:
                epic_map[e_key] = f"{e_key} - {e_summary}"
    except (requests.RequestException, ValueError):
        logger.warning("Epic summary lookup failed", exc_info=True)
        return
    for r in rows:
        r["Epic"] = epic_map.get(r["Epic"], r["Epic"])


def fetch_sprint_review_dataset(server: str, token: str, query_val: str, *, is_sprint: bool = True,
                                auth_type: str = "Personal Access Token (Bearer PAT)", email: str = "",
                                only_unresolved: bool = False, include_raw_status: bool = False,
                                issue_types: Iterable[str] = (), include_all_types: bool = False
                                ) -> pd.DataFrame:
    """Query Jira and return the Sprint Review dataset (empty DataFrame when nothing matches).

    Raises JiraFetchError on missing configuration, HTTP errors, network errors or
    non-JSON responses (typically an SSO/proxy login page).
    """
    if not server or not token:
        raise JiraFetchError("Please provide Jira Server URL and Personal Access Token (PAT).")
    jql = build_sprint_jql(query_val, is_sprint, only_unresolved, issue_types)
    url = f"{server.rstrip('/')}/rest/api/2/search"
    headers, auth = get_jira_auth_headers(token, auth_type, email or "")
    headers["User-Agent"] = BROWSER_USER_AGENT
    params = {"jql": jql, "maxResults": 100, "fields": SEARCH_FIELDS}

    try:
        response = requests.get(url, headers=headers, params=params, auth=auth, timeout=15)
    except requests.RequestException as e:
        raise JiraFetchError(f"Exception connecting to Jira: {e}") from e
    if response.status_code != 200:
        raise JiraFetchError(f"Jira API connection failed ({response.status_code}): {response.text[:500]}")
    try:
        data = response.json()
    except ValueError as e:
        raise JiraFetchError("Jira returned a non-JSON response (Status 200 OK).",
                             body_preview=response.text[:1000]) from e

    issues = data.get("issues", []) or []
    rows = [issue_to_row(i, include_raw_status, include_all_types) for i in issues]
    _resolve_epic_summaries(rows, url, headers, auth)
    return pd.DataFrame(rows)


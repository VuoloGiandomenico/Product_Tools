"""Streamlit glue shared by the Release Plan readiness sub-pages (no rule logic here).

Read-only: Jira and Confluence are only read (GET). Every page keeps its data in its own
session keys ``rr<N>_*`` and fetches nothing until its own Refresh button is clicked.
"""
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import streamlit as st

from utils import confluence_helpers, jira_helpers
from utils import release_checks as rc
from utils import release_plan_helpers as rp


NOT_RELEVANT_WINDOW_DAYS = 90


@dataclass
class Context:
    prefix: str
    server: str
    token: str
    auth: str
    email: str
    conf_server: str
    conf_token: str
    cfg: object
    sandbox: bool
    version: str = ""         # typed version (also names the Release Plan page)
    source_version: str = ""  # version whose Jira issues are read
    valid: bool = False


def page_header(title: str, caption: str) -> None:
    st.title(title)
    st.caption(caption)


def load_context(prefix: str) -> Context:
    """Check config, render the version input and the sandbox banner. Stops on config errors."""
    import dotenv
    dotenv.load_dotenv(override=True)
    missing = rp.missing_config(os.environ)
    if missing:
        st.error("Missing configuration (set in .env): " + ", ".join(missing))
        st.stop()
    try:
        cfg = rp.load_release_plan_config(os.environ)
        sandbox_cfg = rp.load_sandbox_config(os.environ)
    except ValueError as e:
        st.error(str(e))
        st.stop()
    version = bind_release_version_input(prefix, label="Release version (X.Y.Z)")
    ctx = Context(
        prefix=prefix,
        server=os.getenv("JIRA_SERVER", ""), token=os.getenv("JIRA_API_TOKEN", ""),
        auth=os.getenv("JIRA_AUTH_METHOD", "Jira Server Token (Bearer)"), email=os.getenv("JIRA_EMAIL", ""),
        conf_server=os.getenv("CONFLUENCE_SERVER", ""), conf_token=os.getenv("CONFLUENCE_API_TOKEN", ""),
        cfg=cfg, sandbox=False, version=version,
    )
    try:
        ctx.version = rp.validate_version(version)
        ctx.valid = True
    except ValueError:
        return ctx
    ctx.source_version, ctx.sandbox = rp.resolve_source_version(ctx.version, sandbox_cfg)
    if ctx.sandbox:
        st.warning(
            f"SANDBOX: Release {ctx.version} reads the Jira issues of "
            f"{rp.build_fix_version(ctx.source_version, cfg.label)} (read-only). "
            f"Confluence writes are limited to page {rp.SANDBOX_WRITE_PAGE_ID}; Jira is never written."
        )
    return ctx


def _read_freeze(ctx: Context):
    """Return (date|None, note). Read-only GETs of the typed version's Release Plan page."""
    try:
        children = confluence_helpers.list_child_pages(
            ctx.conf_server, ctx.conf_token, ctx.auth, ctx.email, ctx.cfg.parent_id)
        if children.get("error"):
            return None, children["error"]
        title = rp.build_target_title(ctx.cfg.title_template, ctx.version)
        target = rp.select_target_page(children["results"], title, ctx.cfg.parent_id)
        page = confluence_helpers.get_confluence_page(
            ctx.conf_server, ctx.conf_token, ctx.auth, ctx.email, target["id"])
        if page.get("error"):
            return None, page["error"]
        return rc.extract_freeze_date(page.get("storage", "")), ""
    except ValueError as e:
        return None, str(e)


def freeze_date_input(prefix: str, data: Optional[dict]) -> Optional[date]:
    """Freeze date from the Release Plan page, else a manual date input (AC12/AC13)."""
    if not data:
        return None
    if data.get("freeze") is not None:
        st.caption(f"Freeze date (from the Release Plan page): {data['freeze'].isoformat()}")
        return data["freeze"]
    note = data.get("freeze_note")
    st.info("No 'Freeze date' found on the Release Plan page"
            + (f" ({note})" if note else "") + ". Enter it manually (optional); without it, "
            "missing approvals are only warnings.")
    return st.date_input("Freeze date (manual)", value=None, key=f"{prefix}_freeze_manual")


def fetch_issues(ctx: Context, fields: str = jira_helpers.RELEASE_READINESS_FIELDS):
    """Read-only Jira search for the release. Returns (issues, error)."""
    jql = rp.build_release_jql(ctx.source_version, ctx.cfg.label)
    res = jira_helpers.fetch_release_issues_with_comments(
        ctx.server, ctx.token, ctx.auth, ctx.email, jql, fields=fields)
    if res.get("error"):
        if rp.is_missing_fix_version_error(res["error"]):
            return [], f"Version {rp.build_fix_version(ctx.source_version, ctx.cfg.label)} does not exist in Jira."
        return [], res["error"]
    return [rc.issue_from_jira(i) for i in res["issues"]], None


def fetch_not_relevant(ctx: Context, fields: str = jira_helpers.RELEASE_READINESS_FIELDS):
    """Read-only: issues carrying the 'Not release relevant' fixVersion.

    That fixVersion is shared by every release, so the query is bounded to issues updated in the
    last NOT_RELEVANT_WINDOW_DAYS days (paginated GET by the helper) to keep volume sane.
    """
    jql = (f'project = {rp.JIRA_PROJECT} AND issuetype in ({", ".join(rp.ISSUE_TYPES)}) '
           f'AND fixVersion = "{rc.NOT_RELEVANT_FIX_VERSION}" '
           f'AND updated >= -{NOT_RELEVANT_WINDOW_DAYS}d')
    res = jira_helpers.fetch_release_issues_with_comments(
        ctx.server, ctx.token, ctx.auth, ctx.email, jql, fields=fields)
    if res.get("error"):
        return [], res["error"]
    return [rc.issue_from_jira(i) for i in res["issues"]], None


def refresh(ctx: Context, with_freeze: bool = False) -> None:
    """The page's own Refresh button: fetches only when clicked; stores under rr<N>_data."""
    key = f"{ctx.prefix}_data"
    if not st.button("Refresh", key=f"{ctx.prefix}_refresh", disabled=not ctx.valid):
        return
    st.session_state.pop(key, None)
    st.session_state.pop(f"{ctx.prefix}_freeze_manual", None)
    data = {}
    if with_freeze:
        data["freeze"], data["freeze_note"] = _read_freeze(ctx)
    issues, err = fetch_issues(ctx)
    if err:
        st.error(err)
        return
    data["issues"] = issues
    data["checked_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st.session_state[key] = data


def get_data(ctx: Context) -> Optional[dict]:
    if not ctx.valid:
        st.info("Enter a release version X.Y.Z, then click Refresh.")
        return None
    data = st.session_state.get(f"{ctx.prefix}_data")
    if not data:
        st.info("Nothing loaded yet. Click Refresh to check this rule.")
    return data


def render_findings(findings: list, checked_at: str = "", empty_msg: str = "No findings.") -> None:
    ordered = rc.sort_findings(findings)
    if checked_at:
        st.caption(f"Checked at {checked_at}")
    counts = {s: sum(1 for f in ordered if f.severity == s) for s in (rc.ERROR, rc.WARN, rc.INFO)}
    st.write(f"ERROR: {counts[rc.ERROR]} | WARN: {counts[rc.WARN]} | INFO: {counts[rc.INFO]}")
    if not ordered:
        st.success(empty_msg)
        return
    import pandas as pd
    st.dataframe(pd.DataFrame([
        {"Severity": f.severity, "Issue": f.issue_key, "Summary": f.summary,
         "Assignee": f.assignee or "Unassigned", "Reason": f.reason} for f in ordered
    ]), hide_index=True, width='stretch')


def render_critical(rows: list, checked_at: str = "", others: Optional[list] = None) -> None:
    if checked_at:
        st.caption(f"Checked at {checked_at}")
    import pandas as pd
    if not rows:
        st.success("No critical changes.")
    else:
        st.dataframe(pd.DataFrame([
            {"Issue": r.issue_key, "Summary": r.summary, "Assignee": r.assignee or "Unassigned",
             "Reasons": "; ".join(r.reasons)} for r in rows
        ]), hide_index=True, width='stretch')
    if others is not None:
        with st.expander("Other in-scope issues", expanded=False):
            if not others:
                st.write("No other in-scope issues.")
            else:
                st.dataframe(pd.DataFrame([
                    {"Issue": i.key, "Summary": i.summary, "Status": i.status,
                     "Assignee": i.assignee or "Unassigned"} for i in others
                ]), hide_index=True, width='stretch')


def refresh_all(ctx: Context, with_not_relevant: bool = True) -> None:
    """Own Refresh for the aggregate pages (0, 1, 8): in-scope issues + freeze (+ not-relevant issues)."""
    key = f"{ctx.prefix}_data"
    if not st.button("Refresh", key=f"{ctx.prefix}_refresh", disabled=not ctx.valid):
        return
    st.session_state.pop(key, None)
    st.session_state.pop(f"{ctx.prefix}_freeze_manual", None)
    data = {}
    data["freeze"], data["freeze_note"] = _read_freeze(ctx)
    issues, err = fetch_issues(ctx)
    if err:
        st.error(err)
        return
    data["scope_issues"] = list(issues)
    data["not_relevant_issues"] = []
    if with_not_relevant:
        extra, err = fetch_not_relevant(ctx)
        if err:
            st.warning(f"Could not read 'Not release relevant' issues: {err}")
        else:
            seen = {i.key for i in issues}
            data["not_relevant_issues"] = [i for i in extra if i.key not in seen]
    data["issues"] = data["scope_issues"] + data["not_relevant_issues"]
    data["checked_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st.session_state[key] = data


def render_overview(data: dict, freeze) -> None:
    import pandas as pd
    results = rc.run_all(data["issues"], freeze, date.today())
    summ = rc.summarize(results)
    icon = {"GREEN": "🟢", "AMBER": "🟠", "RED": "🔴"}[summ["semaphore"]]
    st.caption(f"Checked at {data['checked_at']}")
    st.subheader(f"{icon} Release readiness: {summ['semaphore']}")
    c1, c2, c3 = st.columns(3)
    c1.metric("ERROR", summ["counts"][rc.ERROR])
    c2.metric("WARN", summ["counts"][rc.WARN])
    c3.metric("INFO", summ["counts"][rc.INFO])
    st.markdown("#### Critical changes")
    render_critical(results["Critical changes"])
    st.markdown("#### Per owner")
    if summ["per_owner"]:
        st.dataframe(pd.DataFrame([
            {"Owner": o, "ERROR": c[rc.ERROR], "WARN": c[rc.WARN], "INFO": c[rc.INFO]}
            for o, c in sorted(summ["per_owner"].items())
        ]), hide_index=True, width='stretch')
    else:
        st.success("No findings.")
    st.markdown("#### Findings per rule")
    for rule, items in results.items():
        if rule == "Critical changes":
            continue
        with st.expander(f"{rule} ({len(items)})", expanded=False):
            render_findings(items)


RELEASE_PLAN_SHARED_VERSION_KEY = "release_plan_version"


def bind_release_version_input(prefix: str, label: str = "Release version (X.Y.Z)") -> str:
    """Keep Release Plan version in sync across all sidebar subpages."""
    shared = st.session_state.get(RELEASE_PLAN_SHARED_VERSION_KEY, "")
    current = st.session_state.get(f"{prefix}_version", shared)
    value = current if current else shared
    version = st.text_input(label, value=value, placeholder="e.g. 35.0.0",
                            key=f"{prefix}_version").strip()
    if version:
        st.session_state[RELEASE_PLAN_SHARED_VERSION_KEY] = version
    elif shared:
        st.session_state[f"{prefix}_version"] = shared
        return shared
    return version

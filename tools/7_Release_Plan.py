import os

import streamlit as st
from dotenv import load_dotenv

from utils.confluence_helpers import get_confluence_page, list_child_pages, update_confluence_page
from utils.jira_helpers import fetch_release_issues_with_comments
from utils.release_plan_helpers import (
    build_preview,
    build_fix_version,
    build_release_jql,
    build_target_title,
    extract_actions,
    check_target_guard,
    fix_version_display,
    is_missing_fix_version_error,
    load_release_plan_config,
    missing_config,
    publish,
    select_target_page,
    validate_version,
)
from utils.release_readiness_page import bind_release_version_input

load_dotenv(override=True)

JIRA_SERVER = os.getenv("JIRA_SERVER", "")
JIRA_TOKEN = os.getenv("JIRA_API_TOKEN", "")
JIRA_AUTH_METHOD = os.getenv("JIRA_AUTH_METHOD", "Jira Server Token (Bearer)")
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "")
CONF_SERVER = os.getenv("CONFLUENCE_SERVER", "")
CONF_TOKEN = os.getenv("CONFLUENCE_API_TOKEN", "")

st.title("📋 Post Actions")
st.caption(
    "Extracts `POST-ACTION: <text>` lines from the Jira descriptions and comments of a release and "
    "adds them, unchecked, to the Post-actions section of a Release Plan page. Existing content is never overwritten."
)

missing = missing_config(os.environ)
if missing:
    st.error("Missing configuration (set in .env): " + ", ".join(missing))
    st.stop()

try:
    cfg = load_release_plan_config(os.environ)
except ValueError as e:
    st.error(str(e))
    st.stop()


def _get_page(page_id):
    return get_confluence_page(CONF_SERVER, CONF_TOKEN, JIRA_AUTH_METHOD, JIRA_EMAIL, page_id)


version_in = bind_release_version_input("rp", label="Release version (X.Y.Z)")
st.text_input("fixVersion prefix", value=fix_version_display(version_in, cfg.label), disabled=True)
st.caption("Once you finish typing, click outside the field (or press Enter) to update the fixVersion.")



def _show_target(title, page_id, url):
    link = f" - [open page]({url})" if url else ""
    st.write(f"Target page: **{title or '?'}** (ID {page_id}){link}")


just_generated = False
if st.button("Generate preview", key="rp_preview_btn"):
    just_generated = True
    st.session_state.pop("rp_preview", None)
    try:
        version = validate_version(version_in)
        children = list_child_pages(CONF_SERVER, CONF_TOKEN, JIRA_AUTH_METHOD, JIRA_EMAIL, cfg.parent_id)
        if children.get("error"):
            raise ValueError(children["error"])
        title = build_target_title(cfg.title_template, version)
        target = select_target_page(children["results"], title, cfg.parent_id)
    except ValueError as e:
        st.error(str(e))
        st.stop()
    page_id = target["id"]
    page = _get_page(page_id)
    # RF13: guard and show the target BEFORE any Jira call.
    guard = check_target_guard(page, version, cfg.label, parent_id=cfg.parent_id)
    _show_target(guard.title or target.get("title", ""), page_id, target.get("url", ""))
    if not guard.ok:
        for err in guard.errors:
            st.error(err)
        st.stop()
    jql = build_release_jql(version, cfg.label)
    res = fetch_release_issues_with_comments(JIRA_SERVER, JIRA_TOKEN, JIRA_AUTH_METHOD, JIRA_EMAIL, jql)
    if res.get("error"):
        if is_missing_fix_version_error(res["error"]):
            st.error(f"Version {build_fix_version(version, cfg.label)} does not exist in Jira. Nothing to extract.")
        else:
            st.error(res["error"])
        st.stop()
    preview = build_preview(page, res["issues"], version, cfg.label, JIRA_SERVER, cfg.parent_id)
    st.session_state["rp_preview"] = {
        "page_id": page_id, "version": version, "label": cfg.label, "parent_id": cfg.parent_id,
        "page_url": target.get("url", ""),
        "page_version": page.get("version"), "preview": preview,
        "actions": extract_actions(res["issues"]), "issue_count": len(res["issues"]),
    }

state = st.session_state.get("rp_preview")
if state:
    preview = state["preview"]
    guard = preview["guard"]
    st.subheader("Preview")
    if not just_generated:
        _show_target(guard.title, guard.page_id, state.get("page_url", ""))
    if not guard.ok:
        for err in guard.errors:
            st.error(err)
    elif preview["error"]:
        st.error(preview["error"])
    else:
        plan = preview["plan"]
        st.write(f"Issues processed: {state['issue_count']}")
        rows = [(a, "New") for a in plan.new_actions] + [(a, "Already present (skipped)") for a in plan.skipped]
        if not rows:
            st.info("No post-actions found for this release.")
        else:
            st.write(f"{len(plan.new_actions)} new, {len(plan.skipped)} already present")
            lines = ["| Issue | Text | Author | Status |", "|---|---|---|---|"]
            for a, status in rows:
                link = f"[{a.issue_key}]({JIRA_SERVER.rstrip('/')}/browse/{a.issue_key})"
                text = a.text.replace("|", "\\|")
                lines.append(f"| {link} | {text} | {a.author} | {status} |")
            st.markdown("\n".join(lines))

        if st.button("Publish to Confluence", key="rp_publish_btn", disabled=not plan.new_actions):
            result = publish(
                get_page=lambda: _get_page(state["page_id"]),
                update_page=lambda pid, title, ver, storage: update_confluence_page(
                    CONF_SERVER, CONF_TOKEN, JIRA_AUTH_METHOD, JIRA_EMAIL, pid, title, ver, storage),
                actions=state["actions"], version=state["version"], label=state["label"],
                jira_server=JIRA_SERVER, parent_id=state["parent_id"],
            )
            if result["ok"]:
                st.success(f"Published: {result['added']} new action(s) added, {result['skipped']} skipped.")
                st.session_state.pop("rp_preview", None)
            else:
                st.error(result["error"])

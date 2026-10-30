import pandas as pd
import requests
import os
import re
from datetime import datetime
import numpy as np
from html.parser import HTMLParser

import altair as alt
from utils.sonar_helpers import SONAR_KPIS, sonar_configured
from utils.sbom_helpers import SBOM_KPI, sbom_configured
from utils.vanguard_helpers import VANGUARD_KPI, vanguard_configured

QUALITY_KPIS = [*SONAR_KPIS.values(), SBOM_KPI, VANGUARD_KPI]

# Item-count KPIs (number of tickets, not story points). Counts come from Jira's sprint report
# (see fetch_exact_sprint_report), so they match Jira's sprint report and burndown chart.
ITEMS_COMMITTED_KPI = "Committed"
ITEMS_ADDED_KPI = "Added"
ITEMS_DELIVERED_KPI = "Delivered"
ITEM_KPIS = [ITEMS_COMMITTED_KPI, ITEMS_ADDED_KPI, ITEMS_DELIVERED_KPI]

# Standard KPI names available for Sprint Review selection.
# Keep this lazy so the module can import before the helper function is defined.
SPRINT_REVIEW_KPI_OPTIONS = []

# Headers an upstream version used for the same counts; only kept so existing Confluence tables still parse.
LEGACY_SCOPE_KPIS = ["Issues at Sprint Start", "Issues Added During Sprint", "Issues Completed in Sprint"]

# Columns that may be appended at the end of an existing Confluence KPI table
APPENDABLE_KPIS = ITEM_KPIS + LEGACY_SCOPE_KPIS + QUALITY_KPIS

# Standard (non-appendable) columns of the Confluence KPI table, in order
BASE_KPI_HEADERS = [
    "Sprint", "Target Dates", "Committed SP (at Start)", "Delivered SP (Total)", "Delivery %", "Releases",
    "Open Bugs (Sev A+B)", "Critical Bugs (Sev A)", "Resolved Bugs", "Avg Cycle Time (Days)",
]
BASE_METRIC_KEYS = ["dates", "total_sp", "achieved_sp", "sprint_pct", "releases", "open_bugs", "crit_bugs", "resolved_bugs", "cycle_time"]


def configured_quality_kpis():
    return ((list(SONAR_KPIS.values()) if sonar_configured() else [])
            + ([SBOM_KPI] if sbom_configured() else [])
            + ([VANGUARD_KPI] if vanguard_configured() else []))


def sprint_review_kpi_catalog():
    """Return the ordered list of Sprint Review KPI names available to the user."""
    catalog = list(ITEM_KPIS) + [
        "Committed SP (at Start)",
        "Delivered SP (Total)",
        "Delivery %",
        "Releases",
        "Open Bugs (Sev A+B)",
        "Critical Bugs (Sev A)",
        "Resolved Bugs",
        "Avg Cycle Time (Days)",
    ]
    catalog.extend(configured_quality_kpis())
    seen = set()
    ordered = []
    for name in catalog:
        if not name or name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return ordered


# Finalize the public catalog after helper functions are defined so UI code can consume a static list.
SPRINT_REVIEW_KPI_OPTIONS = sprint_review_kpi_catalog()


def default_sprint_review_kpis():
    """Default selection for the Sprint Review KPI block, configurable via env var."""
    raw = (os.getenv("DEFAULT_INCLUDED_SPRINT_KPIS") or os.getenv("DEFAULT_INCLUDE_SPRINT_KPIS") or "").strip()
    catalog = sprint_review_kpi_catalog()
    if raw:
        parsed = []
        for name in [part.strip() for part in raw.split(',') if part.strip()]:
            if name in catalog:
                parsed.append(name)
        if parsed:
            return parsed
    default = [
        "Committed",
        "Delivered",
        "Open Bugs (Sev A+B)",
        "Critical Bugs (Sev A)",
        "Avg Cycle Time (Days)",
    ]
    return [name for name in default if name in catalog]



def normalize_jira_issue_types(selected_types):
    """Normalize user-facing issue-type labels to the Jira issue names used in JQL."""
    if not selected_types:
        return []
    mapping = {
        "User Story": ["Story", "User Story"],
        "Task": ["Task"],
        "Technical Task": ["Task", "Technical Task"],
        "Technical Sub-task": ["Technical Sub-task", "Sub-task", "Technical Subtask", "Tech Sub-task"],
        "Sub-task": ["Sub-task"],
        "Improvement": ["Improvement"],
        "Bug": ["Bug"],
        "Epic": ["Epic"],
    }
    normalized = []
    for value in selected_types:
        if value is None:
            continue
        raw = str(value).strip()
        if not raw:
            continue
        for item in mapping.get(raw, [raw]):
            if item and item not in normalized:
                normalized.append(item)
    return normalized
def sprint_kpi_summary_df(selected_kpis=None, metrics=None):
    """Build a two-column KPI summary dataframe for the Sprint Review workbook/export."""
    selected = list(selected_kpis or [])
    if not selected:
        return pd.DataFrame(columns=["KPI", "Value"])
    metrics = metrics or {}
    item_map = metrics.get("items", {}) or {}
    quality_map = metrics.get("quality", {}) or {}
    rows = []
    for name in selected:
        value = ""
        if name in ITEM_KPIS:
            value = item_map.get(name, "")
        elif name == "Committed SP (at Start)":
            value = metrics.get("total_sp", "")
        elif name == "Delivered SP (Total)":
            value = metrics.get("achieved_sp", "")
        elif name == "Delivery %":
            value = metrics.get("sprint_pct", "")
        elif name == "Releases":
            value = metrics.get("releases", "")
        elif name == "Open Bugs (Sev A+B)":
            value = metrics.get("open_bugs", "")
        elif name == "Critical Bugs (Sev A)":
            value = metrics.get("crit_bugs", "")
        elif name == "Resolved Bugs":
            value = metrics.get("resolved_bugs", "")
        elif name == "Avg Cycle Time (Days)":
            value = metrics.get("cycle_time", "")
        elif name in quality_map:
            value = quality_map.get(name, "")
        rows.append({"KPI": name, "Value": value})
    return pd.DataFrame(rows, columns=["KPI", "Value"])


def compute_sprint_metrics(res):
    """Turns a fetch_sprint_kpi_data() result into the metrics dict used by publish_kpis_to_confluence (all issues included)."""
    df = res.get("df")
    if df is None:
        df = pd.DataFrame()
    added_keys = res.get("gh_added_keys") or []
    scope_counts = res.get("scope_counts")
    s_start, s_end = res.get("sprint_start"), res.get("sprint_end")

    if df.empty:
        total_sp = achieved_sp = 0.0
        resolved_bugs = 0
        avg_cycle_time = 0
    else:
        # Scope is only known when Jira's sprint report could be read; otherwise everything counts as committed
        is_added = df["Key"].isin(added_keys) if scope_counts else pd.Series(False, index=df.index)
        # "Resolved" already means "resolved in this sprint" (Sprint Resolved == sprint), set in fetch_sprint_kpi_data
        resolved = df["Resolved"] == True
        total_sp = df[~is_added]["Story Points"].sum()
        achieved_sp = df[resolved]["Story Points"].sum()
        resolved_bugs = int((resolved & (df["Is Bug"] == True)).sum())
        avg_cycle_time = df["Cycle Time (Days)"].mean()
        avg_cycle_time = round(avg_cycle_time, 2) if pd.notnull(avg_cycle_time) else 0

    sprint_pct = (achieved_sp / total_sp * 100) if total_sp > 0 else 0
    return {
        "dates": f"{s_start.strftime('%Y-%m-%d')} to {s_end.strftime('%Y-%m-%d')}" if (s_start and s_end) else "Unknown",
        "total_sp": f"{total_sp:g}",
        "achieved_sp": f"{achieved_sp:g}",
        "sprint_pct": f"{sprint_pct:.1f}%",
        "releases": str(res.get("releases_count", 0)),
        "open_bugs": str(res.get("global_open_bugs", 0)),
        "crit_bugs": str(res.get("global_critical_bugs", 0)),
        "resolved_bugs": str(resolved_bugs),
        "cycle_time": f"{avg_cycle_time:.1f}",
        "items": {name: str(scope_counts[name]) if scope_counts else "" for name in ITEM_KPIS},
        "quality": {},
    }


def metrics_to_table_row(sprint_name, metrics):
    """One row with the same column names as the Confluence KPI table."""
    row = dict(zip(BASE_KPI_HEADERS, [sprint_name] + [metrics.get(k, "") for k in BASE_METRIC_KEYS]))
    row.update(metrics.get("items", {}))
    row.update({k: v for k, v in metrics.get("quality", {}).items() if v not in (None, "")})
    return row


def remove_sprint_rows(body, sprint_names):
    """Removes rows of the first table whose first or second cell matches one of sprint_names."""
    names = {str(n).strip() for n in sprint_names if n}
    start = body.find("<table")
    end = body.find("</table>", start)
    if start == -1 or end == -1 or not names:
        return body
    table = body[start:end]

    def keep(match):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", match.group(0), flags=re.DOTALL)]
        return "" if any(c in names for c in cells[:2]) else match.group(0)

    table = re.sub(r"<tr\b.*?</tr>", keep, table, flags=re.DOTALL)
    return body[:start] + table + body[end:]


def get_auth_headers(server, token, auth_type, email):
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "X-Atlassian-Token": "nocheck"
    }
    auth = None
    token_clean = token.strip()
    if token_clean.lower().startswith("bearer "):
        token_clean = token_clean[7:].strip()

    if auth_type in ["Corporate Login (Username + Password)", "Jira Cloud/Server Basic (Email/User + Token)"]:
        auth = (email.strip(), token_clean)
    else:
        headers["Authorization"] = f"Bearer {token_clean}"
    return headers, auth

def extract_sprint_dates(sprint_string):
    if not sprint_string:
        return None, None
    
    start_match = re.search(r'startDate=([^,]+)', sprint_string)
    end_match = re.search(r'endDate=([^,]+)', sprint_string)
    
    start_date = None
    end_date = None
    try:
        if start_match and start_match.group(1) != "<null>":
            start_date = pd.to_datetime(start_match.group(1)).tz_localize(None)
        if end_match and end_match.group(1) != "<null>":
            end_date = pd.to_datetime(end_match.group(1)).tz_localize(None)
    except Exception:
        pass
    
    return start_date, end_date

def calculate_business_days(start_dt, end_dt):
    if pd.isna(start_dt) or pd.isna(end_dt): return None
    if start_dt > end_dt: return 0.0
    
    bdays = pd.bdate_range(start_dt.date(), end_dt.date())
    
    if len(bdays) == 0:
        return 0.0
        
    if len(bdays) == 1:
        diff = (end_dt - start_dt).total_seconds() / 86400.0
        return round(diff, 2)
        
    first_day_end = pd.Timestamp(start_dt.date()) + pd.Timedelta(days=1)
    first_day_frac = (first_day_end - start_dt).total_seconds() / 86400.0
    
    last_day_start = pd.Timestamp(end_dt.date())
    last_day_frac = (end_dt - last_day_start).total_seconds() / 86400.0
    
    middle_days = len(bdays) - 2
    
    total = first_day_frac + middle_days + last_day_frac
    return round(total, 2)

def get_story_points_field_id(server, headers, auth):
    """Return the Jira field id used for the Sprint field, with a safe fallback."""
    url = f"{server.rstrip('/')}/rest/api/2/field"
    try:
        resp = requests.get(url, headers=headers, auth=auth, timeout=10) if auth else requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            for f in resp.json():
                name_lower = f.get("name", "").lower().strip()
                if name_lower in ["story points", "story point estimate"]:
                    return f.get("id")
    except Exception:
        pass
    return os.getenv("JIRA_STORY_POINTS_FIELD", "customfield_10016")

def get_sprint_field_id(server, headers, auth):
    """Return the Jira field id used for the Sprint field, with a safe fallback."""
    url = f"{server.rstrip('/')}/rest/api/2/field"
    try:
        resp = requests.get(url, headers=headers, auth=auth, timeout=10) if auth else requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            for f in resp.json():
                name_lower = f.get("name", "").lower().strip()
                if name_lower in ["sprint", "sprints"]:
                    return f.get("id")
    except Exception:
        pass
    return os.getenv("JIRA_SPRINT_FIELD", "customfield_10020")


def fetch_exact_sprint_report(server, headers, auth, sprint_id, selected_types=None):
    """Returns (committed SP, achieved SP, added keys, scope counts, error). Tries twice: the sprint report endpoint can fail transiently."""
    normalized_types = normalize_jira_issue_types(selected_types)
    def get_json(url):
        resp = requests.get(url, headers=headers, auth=auth, timeout=30) if auth else requests.get(url, headers=headers, timeout=30)
        if resp.status_code != 200:
            raise Exception(f"{url.split('/rest/')[-1].split('?')[0]} returned {resp.status_code}")
        return resp.json()

    error = None
    for _ in range(2):
        try:
            board_id = get_json(f"{server.rstrip('/')}/rest/agile/1.0/sprint/{sprint_id}").get("originBoardId")
            if not board_id:
                return None, None, [], None, f"sprint {sprint_id} has no board"

            contents = get_json(f"{server.rstrip('/')}/rest/greenhopper/1.0/rapid/charts/sprintreport?rapidViewId={board_id}&sprintId={sprint_id}").get("contents", {})

            completed_initial = contents.get("completedIssuesInitialEstimateSum", {}).get("value", 0)
            not_completed_initial = contents.get("issuesNotCompletedInitialEstimateSum", {}).get("value", 0)
            punted_initial = contents.get("puntedIssuesInitialEstimateSum", {}).get("value", 0)
            outside_initial = contents.get("issuesCompletedInAnotherSprintInitialEstimateSum", {}).get("value", 0)

            total_committed = (completed_initial or 0) + (not_completed_initial or 0) + (punted_initial or 0) + (outside_initial or 0)
            total_achieved = contents.get("completedIssuesEstimateSum", {}).get("value", 0)
            added_keys = list(contents.get("issueKeysAddedDuringSprint", {}).keys())

            # Issue counts as Jira's sprint report and burndown show them (they include issues removed during the
            # sprint and exclude sub-tasks), limited to the selected issue types. The report only has type ids.
            type_names = {str(t.get("id")): t.get("name") for t in get_json(f"{server.rstrip('/')}/rest/api/2/issuetype")} if normalized_types else {}
            def keys_in(list_names):
                return {issue["key"] for name in list_names for issue in contents.get(name, [])
                        if not normalized_types or type_names.get(str(issue.get("typeId"))) in normalized_types}
            report_keys = keys_in(("completedIssues", "issuesNotCompletedInCurrentSprint", "puntedIssues", "issuesCompletedInAnotherSprint"))
            issues_added = len(report_keys & set(added_keys))
            scope_counts = dict(zip(ITEM_KPIS, (len(report_keys) - issues_added, issues_added, len(keys_in(("completedIssues",))))))

            return total_committed, total_achieved, added_keys, scope_counts, None
        except Exception as e:
            error = str(e)
    return None, None, [], None, error


def fetch_sprint_kpi_data(server, token, auth_type, email, sprint_query, selected_types):
    if not server or not token or not sprint_query:
        return {"error": "Server, Token, or Sprint query is missing."}

    normalized_types = normalize_jira_issue_types(selected_types)
    headers, auth = get_auth_headers(server, token, auth_type, email)
    
    if sprint_query.isdigit():
        jql = f"sprint = {sprint_query}"
    else:
        jql = f"sprint = '{sprint_query}'"
        
    if normalized_types:
        types_str = ", ".join([f"'{t}'" for t in normalized_types])
        jql += f" AND issuetype in ({types_str})"
        
    sp_field = get_story_points_field_id(server, headers, auth)
    sprint_field = get_sprint_field_id(server, headers, auth)
    
    sev_a_label = os.getenv("JIRA_SEVERITY_A_LABEL", "Sev-A")
    sev_b_label = os.getenv("JIRA_SEVERITY_B_LABEL", "Sev-B")
    in_prog_status = os.getenv("JIRA_STATUS_IN_PROGRESS", "In Progress").lower()
    
    fields = f"key,summary,status,issuetype,labels,resolution,created,resolutiondate,{sp_field},{sprint_field},customfield_10016,customfield_10024,customfield_10020,customfield_10008,customfield_10015"
    
    url = f"{server.rstrip('/')}/rest/api/2/search"
    params = {
        "jql": jql,
        "maxResults": 200,
        "fields": fields,
        "expand": "changelog"
    }
    
    try:
        response = requests.get(url, headers=headers, params=params, auth=auth, timeout=30) if auth else requests.get(url, headers=headers, params=params, timeout=30)
            
        if response.status_code != 200:
            return {"error": f"Jira API Error {response.status_code}: {response.text}"}
            
        data = response.json()
        issues = data.get("issues", [])
        
        if not issues:
            return {"df": pd.DataFrame()}
            
        rows = []
        sprint_start = None
        sprint_end = None
        
        for issue in issues:
            key = issue.get("key")
            fields_data = issue.get("fields", {})
            
            issuetype = fields_data.get("issuetype", {}).get("name", "")
            status_name = fields_data.get("status", {}).get("name", "")
            resolution = fields_data.get("resolution")
            
            if resolution and resolution.get("name"):
                status_name = f"{status_name} [{resolution.get('name')}]"
            
            story_points = fields_data.get(sp_field)
            if story_points is None:
                story_points = fields_data.get("customfield_10016")
            if story_points is None:
                story_points = fields_data.get("customfield_10024")
                
            try:
                story_points = float(story_points) if story_points is not None else 0
            except:
                story_points = 0
                
            raw_labels = fields_data.get("labels", [])
            labels_str = ", ".join(raw_labels) if isinstance(raw_labels, list) else ""
            labels = [l.lower() for l in raw_labels]
            
            is_sev_a = sev_a_label.lower() in labels
            is_sev_b = sev_b_label.lower() in labels
            
            res_date_str = fields_data.get("resolutiondate")
            res_date = pd.to_datetime(res_date_str).tz_localize(None) if res_date_str else None

            changelog = issue.get("changelog", {}).get("histories", [])
            last_in_progress = None
            changelog = sorted(changelog, key=lambda x: x.get("created", ""))
            
            for history in changelog:
                created_str = history.get("created")
                if not created_str: continue
                dt = pd.to_datetime(created_str).tz_localize(None)
                
                if res_date and dt > res_date:
                    continue

                for item in history.get("items", []):
                    if item.get("field") == "status":
                        to_str = item.get("toString", "").lower()
                        if to_str == in_prog_status:
                            last_in_progress = dt

            is_achieved_in_sprint = False
            if resolution is not None:
                if sprint_end and res_date:
                    if res_date <= sprint_end:
                        is_achieved_in_sprint = True
                else:
                    is_achieved_in_sprint = True

            cycle_time_end = res_date if is_achieved_in_sprint else None
            
            if last_in_progress is None and cycle_time_end is not None:
                if sprint_start and sprint_start <= cycle_time_end:
                    last_in_progress = sprint_start
                    
            cycle_time_days = None
            if last_in_progress and cycle_time_end and last_in_progress <= cycle_time_end:
                cycle_time_days = calculate_business_days(last_in_progress, cycle_time_end)

            server_clean = server.rstrip('/') if server else ""
            jira_url = f"{server_clean}/browse/{key}" if server_clean else key

            # Sprint Resolved: the issue's latest sprint that had started when the issue was resolved.
            # Sprint field values are dicts or "com.atlassian...Sprint@x[id=1,name=Sprint 1,startDate=...]" strings
            sprint_resolved = ""
            if res_date is not None:
                started_sprints = []
                for s_val in fields_data.get(sprint_field) or fields_data.get("customfield_10020") or []:
                    if isinstance(s_val, dict):
                        s_name, s_start = s_val.get("name", ""), s_val.get("startDate")
                    else:
                        name_match = re.search(r'name=([^,]+)', str(s_val))
                        start_match = re.search(r'startDate=([^,]+)', str(s_val))
                        s_name = name_match.group(1) if name_match else ""
                        s_start = start_match.group(1) if start_match and start_match.group(1) != "<null>" else None
                    if s_start and pd.to_datetime(s_start).tz_localize(None) <= res_date:
                        started_sprints.append((pd.to_datetime(s_start).tz_localize(None), s_name))
                if started_sprints:
                    sprint_resolved = max(started_sprints)[1]

            rows.append({
                "Key": key,
                "Jira URL": jira_url,
                "Summary": fields_data.get("summary", ""),
                "Sprint Resolved": sprint_resolved,
                "Type": issuetype,
                "Status": status_name,
                "Labels": labels_str,
                "Resolved": is_achieved_in_sprint,
                "Story Points": story_points,
                "Is Bug": issuetype.lower() == "bug",
                "Is Sev A": is_sev_a,
                "Is Sev B": is_sev_b,
                "Last In Progress": last_in_progress,
                "Resolved At": cycle_time_end,
                "Cycle Time (Days)": cycle_time_days
            })
            
        df = pd.DataFrame(rows)
        
        most_common_id = None
        sprint_name = None
        if sprint_start is None and issues:
            from collections import Counter
            sprint_ids = []
            sprint_map = {}
            for issue in issues:
                s_data = issue.get("fields", {}).get(sprint_field)
                if not s_data:
                    s_data = issue.get("fields", {}).get("customfield_10020")
                if not s_data:
                    s_data = issue.get("fields", {}).get("customfield_10008")
                if not s_data:
                    s_data = issue.get("fields", {}).get("customfield_10015")
                    
                if s_data and isinstance(s_data, list):
                    for s_val in s_data:
                        s_id = None
                        if isinstance(s_val, str):
                            id_match = re.search(r'id=(\d+)', s_val)
                            s_id = id_match.group(1) if id_match else s_val
                        elif isinstance(s_val, dict):
                            s_id = str(s_val.get("id"))
                            
                        if s_id:
                            sprint_ids.append(s_id)
                            sprint_map[s_id] = s_val
            
            if sprint_ids:
                most_common_id = Counter(sprint_ids).most_common(1)[0][0]
                try:
                    sprint_url = f"{server.rstrip('/')}/rest/agile/1.0/sprint/{most_common_id}"
                    s_resp = requests.get(sprint_url, headers=headers, auth=auth, timeout=10) if auth else requests.get(sprint_url, headers=headers, timeout=10)
                    if s_resp.status_code == 200:
                        s_json = s_resp.json()
                        s_str = s_json.get("startDate")
                        e_str = s_json.get("endDate")
                        if s_json.get("name"):
                            sprint_name = s_json.get("name")
                        if s_str: sprint_start = pd.to_datetime(s_str).tz_localize(None)
                        if e_str: sprint_end = pd.to_datetime(e_str).tz_localize(None)
                except Exception:
                    pass
                    
                if sprint_start is None or sprint_name is None:
                    best_sprint = sprint_map.get(most_common_id)
                    if isinstance(best_sprint, str):
                        if not sprint_name:
                            name_match = re.search(r'name=([^,]+)', best_sprint)
                            if name_match: sprint_name = name_match.group(1)
                        if sprint_start is None:
                            sprint_start, sprint_end = extract_sprint_dates(best_sprint)
                    elif isinstance(best_sprint, dict):
                        if not sprint_name:
                            sprint_name = best_sprint.get("name")
                        s_str = best_sprint.get("startDate")
                        e_str = best_sprint.get("endDate")
                        if s_str and sprint_start is None:
                            try: sprint_start = pd.to_datetime(s_str).tz_localize(None)
                            except: pass
                        if e_str and sprint_end is None:
                            try: sprint_end = pd.to_datetime(e_str).tz_localize(None)
                            except: pass
                            
        base_link = os.getenv("JIRA_VERSION_LINK_BASE", "")
        main_project = ""
        if "/projects/" in base_link:
            parts = base_link.split("/projects/")
            if len(parts) > 1:
                main_project = parts[1].split("/")[0]
                
        project_keys_set = set([issue["key"].split("-")[0] for issue in issues])
        if main_project:
            project_keys_set.add(main_project)
        project_keys = list(project_keys_set)
        
        releases_count = 0
        if sprint_start and sprint_end and project_keys:
            released_version_ids = set()
            for p_key in project_keys:
                ver_url = f"{server.rstrip('/')}/rest/api/2/project/{p_key}/versions"
                try:
                    v_resp = requests.get(ver_url, headers=headers, auth=auth, timeout=10) if auth else requests.get(ver_url, headers=headers, timeout=10)
                    if v_resp.status_code == 200:
                        for v in v_resp.json():
                            if v.get("released") and v.get("releaseDate"):
                                r_date = pd.to_datetime(v.get("releaseDate")).tz_localize(None)
                                if sprint_start.date() <= r_date.date() <= sprint_end.date():
                                    released_version_ids.add(v.get("id"))
                except Exception:
                    pass
            releases_count = len(released_version_ids)
                            
        global_open_bugs = 0
        global_critical_bugs = 0
        search_url = f"{server.rstrip('/')}/rest/api/2/search"
        projects_jql = ", ".join([f'"{pk}"' for pk in project_keys])
        
        if sprint_start is not None and sprint_end is not None and pd.notna(sprint_start) and pd.notna(sprint_end):
            sprint_start_str = sprint_start.strftime('%Y-%m-%d %H:%M')
            sprint_end_str = sprint_end.strftime('%Y-%m-%d %H:%M')
            jql_all_bugs = f'project in ({projects_jql}) AND issuetype = "Bug" AND labels in ("{sev_a_label}", "{sev_b_label}") AND created <= "{sprint_end_str}" AND (resolution is EMPTY OR resolutiondate >= "{sprint_start_str}")'
            jql_crit_bugs = f'project in ({projects_jql}) AND issuetype = "Bug" AND labels = "{sev_a_label}" AND created <= "{sprint_end_str}" AND (resolution is EMPTY OR resolutiondate >= "{sprint_start_str}")'
        else:
            jql_all_bugs = f'project in ({projects_jql}) AND issuetype = "Bug" AND resolution is EMPTY AND labels in ("{sev_a_label}", "{sev_b_label}")'
            jql_crit_bugs = f'project in ({projects_jql}) AND issuetype = "Bug" AND resolution is EMPTY AND labels = "{sev_a_label}"'

        params_all_bugs = {"jql": jql_all_bugs, "maxResults": 0}
        try:
            resp_all = requests.get(search_url, headers=headers, params=params_all_bugs, auth=auth, timeout=10) if auth else requests.get(search_url, headers=headers, params=params_all_bugs, timeout=10)
            if resp_all.status_code == 200:
                global_open_bugs = resp_all.json().get("total", 0)
        except Exception:
            pass
            
        params_crit_bugs = {"jql": jql_crit_bugs, "maxResults": 0}
        try:
            resp_crit = requests.get(search_url, headers=headers, params=params_crit_bugs, auth=auth, timeout=10) if auth else requests.get(search_url, headers=headers, params=params_crit_bugs, timeout=10)
            if resp_crit.status_code == 200:
                global_critical_bugs = resp_crit.json().get("total", 0)
        except Exception:
            pass
            
        # Delivered in this sprint = resolved while this sprint was the issue's current sprint (see Sprint Resolved).
        # The loop above runs before the sprint is known, so it cannot check this itself.
        if sprint_name:
            df["Resolved"] = df["Sprint Resolved"] == sprint_name

        gh_committed, gh_achieved, gh_added_keys, scope_counts, scope_error = None, None, [], None, "no sprint found on the issues"
        if most_common_id:
            gh_committed, gh_achieved, gh_added_keys, scope_counts, scope_error = fetch_exact_sprint_report(server, headers, auth, most_common_id, selected_types)
                            
        return {
            "df": df,
            "gh_added_keys": gh_added_keys,
            "scope_counts": scope_counts,
            "scope_error": scope_error,
            "releases_count": releases_count,
            "global_open_bugs": global_open_bugs,
            "global_critical_bugs": global_critical_bugs,
            "sprint_start": sprint_start,
            "sprint_end": sprint_end,
            "sprint_name": sprint_name or sprint_query,
            "error": None
        }
    except Exception as e:
        import traceback
        return {"error": f"Exception during fetch: {e}\n{traceback.format_exc()}"}

def upload_confluence_attachment(server_url, auth, headers, page_id, file_path, file_name):
    base_url = server_url.rstrip("/")
    if "atlassian.net" in base_url and not base_url.endswith("/wiki"):
        base_url = base_url + "/wiki"

    upload_headers = {
        "Accept": "application/json",
        "X-Atlassian-Token": "nocheck"
    }
    if headers and "Authorization" in headers:
        upload_headers["Authorization"] = headers["Authorization"]

    check_url = f"{base_url}/rest/api/content/{page_id}/child/attachment"
    params = {"filename": file_name}
    
    try:
        resp = requests.get(check_url, headers=upload_headers, params=params, auth=auth, timeout=15)
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            if results:
                attachment_id = results[0]["id"]
                update_url = f"{base_url}/rest/api/content/{page_id}/child/attachment/{attachment_id}/data"
                with open(file_path, "rb") as f:
                    files = {"file": (file_name, f, "image/png")}
                    u_resp = requests.post(update_url, headers=upload_headers, files=files, auth=auth, timeout=15)
                    if u_resp.status_code != 200:
                        raise Exception(f"Failed to update attachment {file_name}")
                return
    except Exception:
        pass

    create_url = f"{base_url}/rest/api/content/{page_id}/child/attachment"
    with open(file_path, "rb") as f:
        files = {"file": (file_name, f, "image/png")}
        c_resp = requests.post(create_url, headers=upload_headers, files=files, auth=auth, timeout=15)
        if c_resp.status_code not in (200, 201):
            raise Exception(f"Failed to upload attachment {file_name}")

def sort_df_chronologically(df, sprint_col):
    if df.empty:
        return df
    sprint_nums = []
    for val in df[sprint_col].astype(str):
        match = re.search(r'\d+', val)
        if match:
            sprint_nums.append(int(match.group()))
        else:
            sprint_nums.append(None)
    if all(x is not None for x in sprint_nums):
        df_copy = df.copy()
        df_copy["_sort_key"] = sprint_nums
        return df_copy.sort_values(by="_sort_key", ascending=True).drop(columns=["_sort_key"]).reset_index(drop=True)
        
    date_col = None
    for col in df.columns:
        if "date" in col.lower():
            date_col = col
            break
    if date_col:
        dates = []
        for val in df[date_col].astype(str):
            match = re.search(r'\d{4}-\d{2}-\d{2}', val)
            if match:
                dates.append(pd.to_datetime(match.group()))
            else:
                dates.append(pd.NaT)
        if not all(pd.isna(x) for x in dates):
            df_copy = df.copy()
            df_copy["_sort_key"] = dates
            return df_copy.sort_values(by="_sort_key", ascending=True).drop(columns=["_sort_key"]).reset_index(drop=True)
    return df.reset_index(drop=True)

def format_sprint_label(label, max_len=12):
    s = str(label).strip()
    if len(s) <= max_len:
        return s
    return "..." + s[-(max_len - 3):]

def build_items_chart(df_plot):
    """Stacked/Grouped bars: items committed + added are stacked vs delivered. Expects numeric ITEM_KPIS columns."""
    req_cols = ["Sprint Display", ITEMS_COMMITTED_KPI, ITEMS_ADDED_KPI, ITEMS_DELIVERED_KPI]
    missing = [c for c in req_cols if c not in df_plot.columns]
    if missing:
        return None
        
    df_items = df_plot[req_cols].dropna(
        subset=[ITEMS_COMMITTED_KPI, ITEMS_ADDED_KPI, ITEMS_DELIVERED_KPI], 
        how="all"
    )
    if df_items.empty:
        return None

    # Melt the dataframe for Altair
    df_melted = df_items.melt(
        id_vars=["Sprint Display"],
        value_vars=[ITEMS_COMMITTED_KPI, ITEMS_ADDED_KPI, ITEMS_DELIVERED_KPI],
        var_name="Metric",
        value_name="Items"
    ).dropna(subset=["Items"])
    
    # Create a "Group" column to split the visualization into two distinct columns per sprint
    df_melted["Group"] = df_melted["Metric"].apply(
        lambda m: "Scope (Committed + Added)" if m in [ITEMS_COMMITTED_KPI, ITEMS_ADDED_KPI] else "Delivered"
    )
    
    # Map a stack order so 'Committed' is always on the bottom of the stacked bar
    order_map = {ITEMS_COMMITTED_KPI: 1, ITEMS_ADDED_KPI: 2, ITEMS_DELIVERED_KPI: 1}
    df_melted["StackOrder"] = df_melted["Metric"].map(order_map)

    # Pre-compute vertical (Y) positions to centre the labels on the stacked bars
    df_melted = df_melted.sort_values(by=["Sprint Display", "Group", "StackOrder"])
    df_melted["y_top"] = df_melted.groupby(["Sprint Display", "Group"])["Items"].cumsum()
    df_melted["y_mid"] = df_melted["y_top"] - (df_melted["Items"] / 2)

    # Shared base configuration
    base = alt.Chart(df_melted).encode(
        x=alt.X("Sprint Display:N", title=None, axis=alt.Axis(labelAngle=0), sort=None),
        xOffset=alt.XOffset("Group:N", sort=["Scope (Committed + Added)", "Delivered"])
    )

    # 1. Bar layer
    bars = base.mark_bar().encode(
        y=alt.Y("Items:Q", title="Items"),
        color=alt.Color(
            "Metric:N", 
            legend=alt.Legend(orient="top", title=None, symbolType="circle"),
            scale=alt.Scale(
                domain=[ITEMS_COMMITTED_KPI, ITEMS_ADDED_KPI, ITEMS_DELIVERED_KPI],
                range=["#42a5f5", "#ff7043", "#66bb6a"] # light blue, light orange, green
            )
        ),
        order=alt.Order("StackOrder:O", sort="ascending"),
        tooltip=[
            alt.Tooltip("Sprint Display:N", title="Sprint"),
            alt.Tooltip("Group:N", title="Category"),
            alt.Tooltip("Metric:N", title="Metric"),
            alt.Tooltip("Items:Q", title="Items")
        ]
    )
    
    # 2. Text layer for Scope (Committed and Added): centred on its bar segment
    text_scope = base.transform_filter(
        (alt.datum.Metric != ITEMS_DELIVERED_KPI) & (alt.datum.Items > 0)
    ).mark_text(
        align='center', 
        baseline='middle'
    ).encode(
        y=alt.Y("y_mid:Q"),
        text=alt.Text("Items:Q", format=".0f"),
        color=alt.condition(
            alt.datum.Metric == ITEMS_COMMITTED_KPI,
            alt.value("#0d47a1"), # darker blue
            alt.value("#bf360c")  # darker orange
        )
    )

    # 3. Text layer for the Scope TOTAL: floating above the stacked bar, in white
    text_scope_total = base.transform_filter(
        alt.datum.Group == "Scope (Committed + Added)"
    ).transform_aggregate(
        total_items='sum(Items)',
        groupby=['Sprint Display', 'Group']
    ).transform_filter(
        alt.datum.total_items > 0
    ).mark_text(
        align='center', 
        baseline='bottom', 
        dy=-5,
        color='white'
    ).encode(
        y=alt.Y("total_items:Q"),
        text=alt.Text("total_items:Q", format=".0f")
    )
    
    # 4. Text layer for Delivered: floating above the bar
    text_delivered = base.transform_filter(
        (alt.datum.Metric == ITEMS_DELIVERED_KPI) & (alt.datum.Items > 0)
    ).mark_text(
        align='center', 
        baseline='bottom', 
        dy=-5,
        color='#66bb6a' # same green as the bar
    ).encode(
        y=alt.Y("y_top:Q"),
        text=alt.Text("Items:Q", format=".0f")
    )
    
    # Combine all layers (including text_scope_total)
    return bars + text_scope + text_scope_total + text_delivered


def build_code_quality_chart(df_plot):
    """One small line chart per quality KPI, each with its own scale (smells ~3000, coverage ~90%, issues ~3)."""
    cols = [c for c in QUALITY_KPIS if c in df_plot.columns]
    df_q = df_plot[["Sprint Display"] + cols].dropna(subset=cols, how="all") if cols else pd.DataFrame()
    if df_q.empty:
        return None
    df_q = df_q.melt(id_vars=["Sprint Display"], value_vars=cols, var_name="Metric", value_name="Value").dropna(subset=["Value"])
    line = alt.Chart().mark_line(point=True, color="#26a69a").encode(
        x=alt.X("Sprint Display:N", title=None, axis=alt.Axis(labelAngle=0), sort=None),
        y=alt.Y("Value:Q", title=None, scale=alt.Scale(zero=False, padding=24)),
        tooltip=["Sprint Display", "Metric", "Value"]
    )
    text = line.mark_text(baseline="bottom", dy=-6).encode(text=alt.Text("Value:Q", format=",.4~f"))
    return alt.layer(line, text, data=df_q).properties(width=150, height=200).facet(
        column=alt.Column("Metric:N", title=None, sort=QUALITY_KPIS)
    ).resolve_scale(y="independent")


def generate_and_save_kpi_charts(df_hist):
    if df_hist is None or df_hist.empty:
        return []

    import altair as alt
    
    df_plot = df_hist.copy()
    sprint_col = None
    for pattern in ["sprint name", "sprint", "target dates"]:
        for c in df_plot.columns:
            if pattern in c.lower():
                sprint_col = c
                break
        if sprint_col:
            break
    if not sprint_col:
        sprint_col = df_plot.columns[0]

    def find_column(df_target, patterns):
        cols = list(df_target.columns)
        for p in patterns:
            p_lower = p.lower()
            for c in cols:
                if p_lower == c.lower().strip():
                    return c
        for p in patterns:
            p_lower = p.lower()
            for c in cols:
                if p_lower in c.lower():
                    return c
        return None

    def extract_numeric_col(df_target, patterns):
        matched_col = find_column(df_target, patterns)
        if matched_col:
            s = df_target[matched_col].astype(str)
            s_clean = s.str.replace("%", "", regex=False).str.replace(",", ".", regex=False).str.strip()
            num = pd.to_numeric(s_clean, errors="coerce")
            return num
        return pd.Series(dtype=float, index=df_target.index)

    df_plot = sort_df_chronologically(df_plot, sprint_col)
    df_plot["Sprint Display"] = df_plot[sprint_col].apply(lambda x: format_sprint_label(x, 12))
    df_plot = df_plot.tail(5)

    df_plot["Committed SP"] = extract_numeric_col(df_plot, ["committed sp (at start)", "committed sp", "committed"])
    df_plot["Delivered SP"] = extract_numeric_col(df_plot, ["delivered sp (total)", "delivered sp", "delivered", "achieved"])
    df_plot["Delivery %"] = extract_numeric_col(df_plot, ["delivery %", "delivery", "pct", "entrega", "cumplimiento"])
    
    if "Committed SP" in df_plot.columns and "Delivered SP" in df_plot.columns:
        calc_pct = (df_plot["Delivered SP"] / df_plot["Committed SP"] * 100).round(1)
        df_plot["Delivery %"] = df_plot["Delivery %"].fillna(calc_pct)

    df_plot["Avg Cycle Time (Days)"] = extract_numeric_col(df_plot, ["avg cycle time (days)", "avg cycle time", "cycle time"])
    df_plot["Open Bugs (Sev A+B)"] = extract_numeric_col(df_plot, ["open bugs (sev a+b)", "open bugs"])
    df_plot["Critical Bugs (Sev A)"] = extract_numeric_col(df_plot, ["critical bugs (sev a)", "critical bugs"])

    saved_files = []

    df_sp = df_plot[["Sprint Display", "Committed SP", "Delivered SP"]].dropna(subset=["Committed SP", "Delivered SP"], how="all")
    if not df_sp.empty:
        df_sp_melted = df_sp.melt(id_vars=["Sprint Display"], value_vars=["Committed SP", "Delivered SP"], var_name="Metric", value_name="Story Points")
        bars_sp = alt.Chart(df_sp_melted).mark_bar().encode(
            x=alt.X("Sprint Display:N", title=None, axis=alt.Axis(labelAngle=0), sort=None),
            xOffset="Metric:N",
            y=alt.Y("Story Points:Q", title="Story Points"),
            color=alt.Color("Metric:N", legend=alt.Legend(orient="top", title=None), scale=alt.Scale(range=["#42a5f5", "#66bb6a"])),
            tooltip=["Sprint Display", "Metric", "Story Points"]
        )
        text_sp = bars_sp.mark_text(align='center', baseline='bottom', dy=-5).encode(text=alt.Text("Story Points:Q", format=".0f"))
        chart_sp = (bars_sp + text_sp).properties(width=500, height=280)
        path_sp = "temp_velocity_chart.png"
        chart_sp.save(path_sp)
        saved_files.append((path_sp, "velocity_chart.png"))

    df_pct = df_plot[["Sprint Display", "Delivery %"]].dropna(subset=["Delivery %"])
    if not df_pct.empty:
        bars_pct = alt.Chart(df_pct).mark_bar(color="#ab47bc").encode(
            x=alt.X("Sprint Display:N", title=None, axis=alt.Axis(labelAngle=0), sort=None),
            y=alt.Y("Delivery %:Q", title="Delivery %"),
            tooltip=["Sprint Display", "Delivery %"]
        )
        text_pct = bars_pct.mark_text(align='center', baseline='bottom', dy=-5, color='#ab47bc').encode(text=alt.Text("Delivery %:Q", format=".1f"))
        chart_pct = (bars_pct + text_pct).properties(width=500, height=280)
        path_pct = "temp_delivery_chart.png"
        chart_pct.save(path_pct)
        saved_files.append((path_pct, "delivery_chart.png"))

    df_ct = df_plot[["Sprint Display", "Avg Cycle Time (Days)"]].dropna(subset=["Avg Cycle Time (Days)"])
    if not df_ct.empty:
        bars_ct = alt.Chart(df_ct).mark_bar(color="#ffa726").encode(
            x=alt.X("Sprint Display:N", title=None, axis=alt.Axis(labelAngle=0), sort=None),
            y=alt.Y("Avg Cycle Time (Days):Q", title="Days"),
            tooltip=["Sprint Display", "Avg Cycle Time (Days)"]
        )
        text_ct = bars_ct.mark_text(align='center', baseline='bottom', dy=-5, color='#ffa726').encode(text=alt.Text("Avg Cycle Time (Days):Q", format=".1f"))
        chart_ct = (bars_ct + text_ct).properties(width=500, height=280)
        path_ct = "temp_cycle_time_chart.png"
        chart_ct.save(path_ct)
        saved_files.append((path_ct, "cycle_time_chart.png"))

    df_bugs = df_plot[["Sprint Display", "Open Bugs (Sev A+B)", "Critical Bugs (Sev A)"]].dropna(subset=["Open Bugs (Sev A+B)", "Critical Bugs (Sev A)"], how="all")
    if not df_bugs.empty:
        df_bugs_melted = df_bugs.melt(id_vars=["Sprint Display"], value_vars=["Open Bugs (Sev A+B)", "Critical Bugs (Sev A)"], var_name="Bug Type", value_name="Count")
        bars_bugs = alt.Chart(df_bugs_melted).mark_bar().encode(
            x=alt.X("Sprint Display:N", title=None, axis=alt.Axis(labelAngle=0), sort=None),
            xOffset="Bug Type:N",
            y=alt.Y("Count:Q", title="Bugs"),
            color=alt.Color("Bug Type:N", legend=alt.Legend(orient="top", title=None), scale=alt.Scale(range=["#29b6f6", "#b71c1c"])),
            tooltip=["Sprint Display", "Bug Type", "Count"]
        )
        text_bugs = bars_bugs.mark_text(align='center', baseline='bottom', dy=-5).encode(text=alt.Text("Count:Q", format=".0f"))
        chart_bugs = (bars_bugs + text_bugs).properties(width=500, height=280)
        path_bugs = "temp_bugs_chart.png"
        chart_bugs.save(path_bugs)
        saved_files.append((path_bugs, "bugs_chart.png"))

    # Items Committed vs Added vs Delivered (only if the table has the item columns)
    for name in ITEM_KPIS:
        if name in df_plot.columns:  # exact match: "Committed" must not pick up "Committed SP (at Start)"
            df_plot[name] = extract_numeric_col(df_plot, [name])
    chart_items = build_items_chart(df_plot)
    if chart_items is not None:
        path_items = "temp_items_chart.png"
        chart_items.properties(width=500, height=280).save(path_items)
        saved_files.append((path_items, "items_chart.png"))

    # 5. Code Quality Chart (quality columns, only if the table has them)
    for name in QUALITY_KPIS:
        if find_column(df_plot, [name]):
            df_plot[name] = extract_numeric_col(df_plot, [name])
    chart_quality = build_code_quality_chart(df_plot)
    if chart_quality is not None:
        path_quality = "temp_code_quality_chart.png"
        chart_quality.save(path_quality)
        saved_files.append((path_quality, "code_quality_chart.png"))

    return saved_files

class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.current_row = []
        self.current_cell = []
        self.in_cell = False
        self.table_count = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.table_count += 1
        if self.table_count == 1:
            if tag in ('td', 'th'):
                self.in_cell = True
                self.current_cell = []
            elif tag == 'tr':
                self.current_row = []

    def handle_endtag(self, tag):
        if self.table_count == 1:
            if tag in ('td', 'th'):
                self.in_cell = False
                self.current_row.append(''.join(self.current_cell).strip())
            elif tag == 'tr':
                if self.current_row:
                    self.rows.append(self.current_row)

    def handle_data(self, data):
        if self.table_count == 1 and self.in_cell:
            self.current_cell.append(data)

def parse_confluence_html_table(html_body):
    if not html_body or '<table' not in html_body:
        return None
    try:
        parser = TableParser()
        parser.feed(html_body)
        if not parser.rows or len(parser.rows) < 2:
            return None
        headers = parser.rows[0]
        data_rows = parser.rows[1:]
        # Pad rows written before the item-count or quality columns existed; drop any other length mismatch for safety
        valid_rows = [r + [""] * (len(headers) - len(r)) for r in data_rows if len(r) <= len(headers) and all(h in APPENDABLE_KPIS for h in headers[len(r):])]
        return pd.DataFrame(valid_rows, columns=headers)
    except Exception:
        return None

def fetch_confluence_kpi_history(server_url, auth_type, token, email, space_key, page_title):
    if not server_url or not token or not space_key or not page_title:
        return None
        
    base_url = server_url.rstrip("/")
    if "atlassian.net" in base_url and not base_url.endswith("/wiki"):
        base_url = base_url + "/wiki"
        
    headers = {"Accept": "application/json"}
    auth = None
    if auth_type in ["Corporate Login (Username + Password)", "Jira Cloud (Email + API Token)"]:
        if not email:
            return None
        auth = (email, token)
    else:
        headers["Authorization"] = f"Bearer {token}"
        
    try:
        page = find_confluence_page(base_url, headers, auth, space_key, page_title)
        if not page:
            return None

        html_body = page.get("body", {}).get("storage", {}).get("value", "")
        return parse_confluence_html_table(html_body)
    except Exception:
        return None

def find_confluence_page(base_url, headers, auth, space_key, page_title, attempts=15):
    # Confluence Data Center often answers "no results" (or 500) when the page body is expanded, while
    # the same lookup without the body is reliable. Confirm the page exists first, then retry the body
    # read, so a flaky answer is never mistaken for a missing page.
    find_url = f"{base_url}/rest/api/content"
    params = {"title": page_title, "spaceKey": space_key}
    resp = requests.get(find_url, headers=headers, params={**params, "expand": "version"}, auth=auth, timeout=15)
    if resp.status_code != 200:
        raise Exception(f"Failed to query Confluence page ({resp.status_code}): {resp.text}")
    found = resp.json().get("results", [])
    if not found:
        return None
    for _ in range(attempts):
        resp = requests.get(find_url, headers=headers, params={**params, "expand": "body.storage"}, auth=auth, timeout=15)
        results = resp.json().get("results", []) if resp.status_code == 200 else []
        if results:
            return {**found[0], "body": results[0]["body"]}
    raise Exception("Confluence found the page but did not return its content. Please try again in a moment.")


def publish_kpis_to_confluence(server_url, auth_type, token, email, space_key, page_title, sprint_val, sprint_name, metrics,
                               replace_existing=False, upload_charts=True):
    if not server_url or not token or not space_key or not page_title:
        raise Exception("Required configuration fields (URL, Token, Space Key, Page Title) cannot be empty.")
        
    base_url = server_url.rstrip("/")
    if "atlassian.net" in base_url and not base_url.endswith("/wiki"):
        base_url = base_url + "/wiki"
        
    headers, auth = get_auth_headers(server_url, token, auth_type, email)

    page = find_confluence_page(base_url, headers, auth, space_key, page_title)
    results = [page] if page else []

    display_sprint = sprint_name if sprint_name else sprint_val

    # Item-count columns and quality columns (SonarQube, SBOM Inventory, Vanguard) go at the end of the table. Each one is
    # appended to an existing table the first time it is available, and kept (empty if the source is unavailable) once there.
    current_body = results[0]["body"]["storage"]["value"] if results else ""
    header_parser = TableParser()
    header_parser.feed(current_body)
    existing_headers = header_parser.rows[0] if header_parser.rows else []
    added_columns = [name for name in ITEM_KPIS + configured_quality_kpis() if name not in existing_headers]
    extra_columns = [h for h in existing_headers if h in APPENDABLE_KPIS] + added_columns
    quality_columns = [c for c in extra_columns if c in QUALITY_KPIS]
    item_values = metrics.get("items", {})
    legacy_values = {legacy: item_values.get(name, "") for legacy, name in zip(LEGACY_SCOPE_KPIS, ITEM_KPIS)}
    extra_values = {**item_values, **legacy_values, **metrics.get("quality", {})}
    quality_th = "".join(f"<th>{name}</th>" for name in added_columns)
    quality_td = "".join(f"<td>{extra_values.get(name, '')}</td>" for name in extra_columns)
    quality_cols = "<col/>" * len(added_columns)

    # Standard single Sprint column format (10 columns)
    new_row_single = f"""
    <tr>
        <td>{display_sprint}</td>
        <td>{metrics.get('dates', '')}</td>
        <td>{metrics.get('total_sp', '')}</td>
        <td>{metrics.get('achieved_sp', '')}</td>
        <td>{metrics.get('sprint_pct', '')}</td>
        <td>{metrics.get('releases', '')}</td>
        <td>{metrics.get('open_bugs', '')}</td>
        <td>{metrics.get('crit_bugs', '')}</td>
        <td>{metrics.get('resolved_bugs', '')}</td>
        <td>{metrics.get('cycle_time', '')}</td>
        {quality_td}
    </tr>
    """

    # Double Sprint column format (11 columns - for compatibility with pages containing both Sprint and Sprint Name)
    new_row_double = f"""
    <tr>
        <td>{sprint_val}</td>
        <td>{sprint_name}</td>
        <td>{metrics.get('dates', '')}</td>
        <td>{metrics.get('total_sp', '')}</td>
        <td>{metrics.get('achieved_sp', '')}</td>
        <td>{metrics.get('sprint_pct', '')}</td>
        <td>{metrics.get('releases', '')}</td>
        <td>{metrics.get('open_bugs', '')}</td>
        <td>{metrics.get('crit_bugs', '')}</td>
        <td>{metrics.get('resolved_bugs', '')}</td>
        <td>{metrics.get('cycle_time', '')}</td>
        {quality_td}
    </tr>
    """

    base_table = f"""
    <table class="wrapped">
        <colgroup><col/><col/><col/><col/><col/><col/><col/><col/><col/><col/>{quality_cols}</colgroup>
        <tbody>
            <tr>
                <th>Sprint</th>
                <th>Target Dates</th>
                <th>Committed SP (at Start)</th>
                <th>Delivered SP (Total)</th>
                <th>Delivery %</th>
                <th>Releases</th>
                <th>Open Bugs (Sev A+B)</th>
                <th>Critical Bugs (Sev A)</th>
                <th>Resolved Bugs</th>
                <th>Avg Cycle Time (Days)</th>
                {quality_th}
            </tr>
            {new_row_single}
        </tbody>
    </table>
    """

    image_markup = """
    <p><strong>Historical KPI Evolution</strong></p>
    <table class="wrapped">
        <tbody>
            <tr>
                <td style="text-align: center;">
                    <h4>Committed vs Delivered Story Points</h4>
                    <p style="font-size: 12px; color: #555;"><em>Compares total SP committed at start vs total SP actually delivered.</em><br/>
                    <span style="font-size: 11px;"><b>Calculation:</b> Committed SP strictly excludes scope creep (tickets added mid-sprint). Delivered SP includes all resolved tickets on or before the sprint's End Date.</span></p>
                    <ac:image ac:original-height="280" ac:original-width="500">
                        <ri:attachment ri:filename="velocity_chart.png" />
                    </ac:image>
                </td>
                <td style="text-align: center;">
                    <h4>Delivery % Trend</h4>
                    <p style="font-size: 12px; color: #555;"><em>Measures ability to complete committed workload.</em><br/>
                    <span style="font-size: 11px;"><b>Calculation:</b> (Delivered SP / Committed SP) * 100. Can exceed 100% if scope creep tickets are completed.</span></p>
                    <ac:image ac:original-height="280" ac:original-width="500">
                        <ri:attachment ri:filename="delivery_chart.png" />
                    </ac:image>
                </td>
            </tr>
            <tr>
                <td style="text-align: center;">
                    <h4>Average Cycle Time (Days)</h4>
                    <p style="font-size: 12px; color: #555;"><em>Tracks the average working time to complete an issue.</em><br/>
                    <span style="font-size: 11px;"><b>Calculation:</b> The mean of business days from the last 'In Progress' transition until 'Resolved', clamped to the sprint start date.</span></p>
                    <ac:image ac:original-height="280" ac:original-width="500">
                        <ri:attachment ri:filename="cycle_time_chart.png" />
                    </ac:image>
                </td>
                <td style="text-align: center;">
                    <h4>Open &amp; Critical Bugs Trend</h4>
                    <p style="font-size: 12px; color: #555;"><em>Monitors unresolved high-priority bugs at the end of each sprint.</em><br/>
                    <span style="font-size: 11px;"><b>Calculation:</b> Uses JQL to count all project bugs (Sev A+B) created before the sprint ended that remained unresolved, or were resolved after the sprint started.</span></p>
                    <ac:image ac:original-height="280" ac:original-width="500">
                        <ri:attachment ri:filename="bugs_chart.png" />
                    </ac:image>
                </td>
            </tr>
            <tr>
                <td style="text-align: center;">
                    <h4>Items Committed vs Added vs Delivered</h4>
                    <p style="font-size: 12px; color: #555;"><em>Compares the number of tickets committed at sprint start, added during the sprint and finally delivered.</em><br/>
                    <span style="font-size: 11px;"><b>Calculation:</b> Committed = tickets in the sprint at start (excludes scope creep). Added = tickets flagged by Jira as added after sprint start. Delivered = all tickets resolved on or before the sprint's End Date.</span></p>
                    <ac:image ac:original-height="280" ac:original-width="500">
                        <ri:attachment ri:filename="items_chart.png" />
                    </ac:image>
                </td>
                <td></td>
            </tr>
        </tbody>
    </table>
    """

    quality_image_markup = """
    <p><strong>Code Quality Evolution (SonarQube, SBOM Inventory, Vanguard)</strong></p>
    <ac:image ac:original-height="250" ac:original-width="560">
        <ri:attachment ri:filename="code_quality_chart.png" />
    </ac:image>
    """

    if results:
        page_id = results[0]["id"]
        current_version = results[0]["version"]["number"]

        if added_columns and existing_headers:
            # ponytail: assumes the KPI table is the first table on the page, same as TableParser
            header_end = current_body.find("</tr>")
            current_body = current_body[:header_end] + quality_th + current_body[header_end:]
            colgroup_end = current_body.find("</colgroup>")
            if -1 < colgroup_end < header_end:
                current_body = current_body[:colgroup_end] + quality_cols + current_body[colgroup_end:]

        # Check if existing table has 10 columns (contains both Sprint and Sprint Name)
        is_double_header = "<th>Sprint Name</th>" in current_body and "<th>Sprint</th>" in current_body
        row_to_insert = new_row_double if is_double_header else new_row_single
        if replace_existing:
            current_body = remove_sprint_rows(current_body, [display_sprint, sprint_val])
        # Append row to existing table if present
        if "</tbody>" in current_body:
            new_body = current_body.replace("</tbody>", f"{row_to_insert}</tbody>", 1)
        elif "</table>" in current_body:
            new_body = current_body.replace("</table>", f"{row_to_insert}</table>", 1)
        else:
            new_body = current_body + "<br/>" + base_table

        # Always update the charts section to ensure the latest titles and descriptions are visible
        new_body = re.sub(r'<br/>\s*<p><strong>Historical KPI Evolution</strong></p>.*?</table>', '', new_body, flags=re.DOTALL)
        new_body = re.sub(r'\s*<p><strong>Historical KPI Evolution</strong></p>.*?</table>', '', new_body, flags=re.DOTALL)
        # The separate "Sprint Scope Evolution" section is superseded by the items chart in Historical KPI Evolution
        new_body = re.sub(r'(<br/>)?\s*<p><strong>Sprint Scope Evolution \(Issues\)</strong></p>.*?</ac:image>', '', new_body, flags=re.DOTALL)
        new_body = new_body.strip() + "<br/>" + image_markup
        
        if quality_columns and "code_quality_chart.png" not in new_body:
            new_body = new_body + "<br/>" + quality_image_markup

        update_url = f"{base_url}/rest/api/content/{page_id}"
        update_payload = {
            "id": page_id,
            "type": "page",
            "title": page_title,
            "space": {"key": space_key},
            "body": {"storage": {"value": new_body, "representation": "storage"}},
            "version": {"number": current_version + 1}
        }
        
        update_headers = headers.copy()
        update_headers["Content-Type"] = "application/json"
        
        u_resp = requests.put(update_url, headers=update_headers, json=update_payload, auth=auth, timeout=15) if auth else requests.put(update_url, headers=update_headers, json=update_payload, timeout=15)
        if u_resp.status_code != 200:
            raise Exception(f"Failed to update Confluence page ({u_resp.status_code}): {u_resp.text}")
            
    else:
        create_url = f"{base_url}/rest/api/content"
        new_body = f"<p>Sprint KPIs Overview</p>{base_table}<br/>{image_markup}"
        if quality_columns:
            new_body = new_body + "<br/>" + quality_image_markup
        create_payload = {
            "type": "page",
            "title": page_title,
            "space": {"key": space_key},
            "body": {"storage": {"value": new_body, "representation": "storage"}}
        }
        
        create_headers = headers.copy()
        create_headers["Content-Type"] = "application/json"
        
        c_resp = requests.post(create_url, headers=create_headers, json=create_payload, auth=auth, timeout=15) if auth else requests.post(create_url, headers=create_headers, json=create_payload, timeout=15)
        if c_resp.status_code not in (200, 201):
            raise Exception(f"Failed to create new Confluence page ({c_resp.status_code}): {c_resp.text}")
        
        page_id = c_resp.json().get("id")

    # Generate PNG charts from the updated table body and upload them
    df_hist = parse_confluence_html_table(new_body) if upload_charts else None
    if df_hist is not None:
        try:
            saved_files = generate_and_save_kpi_charts(df_hist)
            for temp_path, target_filename in saved_files:
                try:
                    upload_confluence_attachment(
                        server_url=server_url,
                        auth=auth,
                        headers=headers,
                        page_id=page_id,
                        file_path=temp_path,
                        file_name=target_filename
                    )
                finally:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
        except Exception as chart_ex:
            raise Exception(f"KPI table uploaded, but failed to generate/upload charts: {chart_ex}")
        
    return f"{base_url}/pages/viewpage.action?pageId={page_id}"

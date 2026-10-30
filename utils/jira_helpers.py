import requests
import re
import pandas as pd
import logging

logger = logging.getLogger(__name__)

def map_jira_status(status_name):
    if not status_name or not isinstance(status_name, str):
        return "To Do"
    s_clean = status_name.strip().lower()
    if s_clean in ["blocked", "impeded"]:
        return "Blocked"
    elif s_clean in ["resolved", "closed", "done", "acceptance test"]:
        return "Done"
    elif s_clean in ["to do", "todo", "to-do", "backlog", "open", "new", "reopened"]:
        return "To Do"
    else:
        return "In Progress"

def map_epic_status_for_completion_table(status_name):
    status_clean = str(status_name).strip().lower()
    if status_clean in ["done", "closed", "resolved", "complete", "acceptance test"]:
        return "Done"
    if status_clean == "in progress":
        return "In Progress"
    return "To Do"

def get_custom_field_ids(server, headers, auth):
    """Queries Jira fields to find custom field IDs for Target start date and Target end date."""
    target_start_id = None
    target_end_id = None
    url = f"{server.rstrip('/')}/rest/api/2/field"
    try:
        if auth:
            resp = requests.get(url, headers=headers, auth=auth, timeout=10)
        else:
            resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            fields_list = resp.json()
            for f in fields_list:
                name_lower = f.get("name", "").lower().strip()
                field_id = f.get("id")
                if name_lower in ["target start date", "target start", "target_start_date", "target_start"]:
                    target_start_id = field_id
                elif name_lower in ["target end date", "target end", "target_end_date", "target_end", "target enddate"]:
                    target_end_id = field_id
    except Exception as e:
        print(f"[JIRA Helpers] Error fetching fields list: {e}")
    return target_start_id, target_end_id

def get_jira_creation_custom_fields(server, token, auth_type, email):
    story_points_id = None
    sprint_id = None
    epic_link_id = None
    url = f"{server.rstrip('/')}/rest/api/2/field"
    headers, auth = get_jira_auth_headers(token, auth_type, email)
    try:
        if auth:
            resp = requests.get(url, headers=headers, auth=auth, timeout=10)
        else:
            resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            for f in resp.json():
                name_lower = f.get("name", "").lower().strip()
                field_id = f.get("id")
                if name_lower in ["story points", "story point estimate"]:
                    story_points_id = field_id
                elif name_lower == "sprint":
                    sprint_id = field_id
                elif name_lower == "epic link":
                    epic_link_id = field_id
    except Exception as e:
        print(f"[JIRA Helpers] Error fetching fields for issue creation: {e}")
    return story_points_id, sprint_id, epic_link_id

def resolve_sprint_id_by_name(server, token, auth_type, email, sprint_name):
    # If the user passed an integer string, use it directly
    if str(sprint_name).strip().isdigit():
        return int(str(sprint_name).strip())
        
    url = f"{server.rstrip('/')}/rest/api/2/search"
    headers, auth = get_jira_auth_headers(token, auth_type, email)
    params = {"jql": f"sprint='{sprint_name}'", "maxResults": 1}
    try:
        if auth:
            resp = requests.get(url, headers=headers, params=params, auth=auth, timeout=10)
        else:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            
        if resp.status_code == 200:
            issues = resp.json().get("issues", [])
            if issues:
                fields = issues[0].get("fields", {})
                for k, v in fields.items():
                    if isinstance(v, list) and len(v) > 0 and isinstance(v[0], str) and "com.atlassian.greenhopper.service.sprint.Sprint" in v[0]:
                        import re
                        match = re.search(r'id=(\d+)', v[0])
                        if match:
                            return int(match.group(1))
                    elif isinstance(v, dict) and "id" in v and "state" in v and "name" in v:
                        return int(v.get("id"))
                    elif isinstance(v, list) and len(v) > 0 and isinstance(v[0], dict) and "id" in v[0] and "state" in v[0]:
                        return int(v[0].get("id"))
    except Exception as e:
        print(f"[JIRA Helpers] Error resolving sprint ID by name: {e}")
    
    return None

def fetch_jira_tickets_dataset(server, token, query_val, is_sprint=True, auth_type="Personal Access Token (Bearer PAT)", email="", only_unresolved=False, include_raw_status=False, include_description=False):
    if not server or not token:
        try:
            import streamlit as st
            st.session_state.jira_query_error = "Server URL or Token is missing."
        except:
            pass
        return None
        
    token_clean = token.strip()
    if token_clean.lower().startswith("bearer "):
        token_clean = token_clean[7:].strip()
        
    if is_sprint:
        if query_val.isdigit():
            jql = f"sprint = {query_val}"
        else:
            jql = f"sprint = '{query_val}'"
            
        if only_unresolved:
            jql += " AND resolution is empty"
    else:
        jql = query_val
        
    url = f"{server.rstrip('/')}/rest/api/2/search"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "X-Atlassian-Token": "nocheck"
    }

    auth = None
    if auth_type in ["Corporate Login (Username + Password)", "Jira Cloud/Server Basic (Email/User + Token)"]:
        auth = (email.strip(), token_clean)
    else:
        headers["Authorization"] = f"Bearer {token_clean}"
        
    # Dynamically find the custom fields for Target start and Target end dates
    target_start_cf, target_end_cf = get_custom_field_ids(server, headers, auth)
    
    requested_fields = ["key", "summary", "status", "fixVersions", "parent", "assignee", "issuetype", "labels", "duedate"]
    if include_description:
        requested_fields.append("description")
    if target_start_cf:
        requested_fields.append(target_start_cf)
    if target_end_cf:
        requested_fields.append(target_end_cf)
    
    # Fallback common customfield IDs just in case
    for fb in ["customfield_12115", "customfield_12116", "customfield_10015", "customfield_10016", "customfield_10008", "customfield_10009"]:
        if fb not in requested_fields:
            requested_fields.append(fb)
        
    params = {
        "jql": jql,
        "maxResults": 100,
        "fields": ",".join(requested_fields)
    }

    print(f"[DEBUG] fetch_jira_tickets_dataset: url={url}, JQL={jql}")
    try:
        if auth:
            response = requests.get(url, headers=headers, params=params, auth=auth, timeout=15)
        else:
            response = requests.get(url, headers=headers, params=params, timeout=15)

        print(f"[DEBUG] fetch_jira_tickets_dataset: response status_code={response.status_code}")
        logger.debug(f"fetch_jira_tickets_dataset: response status_code={response.status_code}")
        if response.status_code != 200:
            err_msg = f"Jira API connection failed ({response.status_code}): {response.text}"
            logger.warning(f"fetch_jira_tickets_dataset ERROR: {err_msg}")
            print(f"[JIRA Helpers] {err_msg}")
            try:
                import streamlit as st
                st.session_state.jira_query_error = err_msg
            except:
                pass
            return None
            
        try:
            data = response.json()
        except ValueError as json_err:
            err_msg = f"Jira returned a non-JSON response: {json_err}"
            print(f"[JIRA Helpers] {err_msg}")
            try:
                import streamlit as st
                st.session_state.jira_query_error = err_msg
            except:
                pass
            return None
            
        issues = data.get("issues", [])
        logger.debug(f"fetch_jira_tickets_dataset: issues length = {len(issues) if issues else 0}")
        if not issues:
            return pd.DataFrame()
            
        rows = []
        for issue in issues:
            fields = issue.get("fields", {})
            key = issue.get("key", "N/A")
            summary = fields.get("summary", "Untitled Task")
            
            status_obj = fields.get("status") or {}
            raw_status = status_obj.get("name", "To Do")
            status = map_jira_status(raw_status)
            
            fix_versions = fields.get("fixVersions", [])
            fix_version = fix_versions[0].get("name", "") if fix_versions else ""
            
            assignee_obj = fields.get("assignee") or {}
            assignee = assignee_obj.get("displayName", "Unassigned")
            
            # Use space separator to match Jira's raw label list format (consistent with the rest of the tool)
            labels = " ".join(fields.get("labels", []))

            # Epic detection: check known epic-link custom fields and parent
            epic = "-"
            for cf in ["customfield_10014", "customfield_10000", "customfield_10008", "customfield_10009"]:
                cf_val = fields.get(cf)
                if cf_val:
                    epic = cf_val.get("key", str(cf_val)) if isinstance(cf_val, dict) else str(cf_val)
                    break
            parent = fields.get("parent")
            if parent:
                parent_key = parent.get("key", "")
                parent_summary = (parent.get("fields") or {}).get("summary", "")
                if epic == "-":
                    epic = f"{parent_key} - {parent_summary}" if parent_summary else parent_key

            # Extract start and end planning dates
            due_date = fields.get("duedate")
            target_start = None
            if target_start_cf:
                target_start = fields.get(target_start_cf)
            if not target_start:
                target_start = fields.get("customfield_12115") or fields.get("customfield_10015")
                
            target_end = None
            if target_end_cf:
                target_end = fields.get(target_end_cf)
            if not target_end:
                target_end = fields.get("customfield_12116") or fields.get("customfield_10016") or due_date
            
            row = {
                "Key": key,
                "Summary": summary,
                "Epic": epic,
                "Status": status,
                "Fix Version": fix_version,
                "Assignee": assignee,
                "Labels": labels,
                "Target start date": target_start if target_start else "",
                "Target end date": target_end if target_end else ""
            }
            if include_raw_status:
                row["Raw Status"] = raw_status
            if include_description:
                row["Description"] = fields.get("description") or ""
            rows.append(row)
            
        return pd.DataFrame(rows)
    except Exception as e:
        err_msg = f"Exception fetching dataset: {e}"
        print(f"[JIRA Helpers] {err_msg}")
        try:
            import streamlit as st
            st.session_state.jira_query_error = err_msg
        except:
            pass
        return None

def fetch_epic_completion(server, token, epic_keys, epic_link_field, auth_type="Personal Access Token (Bearer PAT)", email="", return_issue_counts=False):
    token_clean = token.strip()
    if token_clean.lower().startswith("bearer "):
        token_clean = token_clean[7:].strip()

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "X-Atlassian-Token": "nocheck"
    }
    auth = None
    if auth_type in ["Corporate Login (Username + Password)", "Jira Cloud/Server Basic (Email/User + Token)"]:
        auth = (email.strip(), token_clean)
    else:
        headers["Authorization"] = f"Bearer {token_clean}"

    completion_by_epic = {}
    issue_status_counts_by_epic = {}
    for k in epic_keys:
        completion_by_epic[k] = "-"
        issue_status_counts_by_epic[k] = {"Closed / Resolved": 0, "To Do": 0, "In progress": 0}

    if not epic_keys:
        return (completion_by_epic, issue_status_counts_by_epic) if return_issue_counts else completion_by_epic

    # Build JQL to query all child tickets in one go
    keys_str = ",".join([f'"{k}"' for k in epic_keys])
    relation_jql = f'parent in ({keys_str})' if epic_link_field == "Parent" else f'"Epic Link" in ({keys_str})'
    
    params = {
        "jql": relation_jql,
        "maxResults": 1000,
        "fields": f"status,{'parent' if epic_link_field == 'Parent' else 'customfield_10014,customfield_10000,customfield_10008,customfield_10009'}"
    }

    try:
        response = requests.get(
            f"{server.rstrip('/')}/rest/api/2/search",
            headers=headers,
            params=params,
            auth=auth,
            timeout=15
        )
        if response.status_code != 200:
            return (completion_by_epic, issue_status_counts_by_epic) if return_issue_counts else completion_by_epic

        issues = response.json().get("issues", [])
        if not issues:
            return (completion_by_epic, issue_status_counts_by_epic) if return_issue_counts else completion_by_epic

        # Group child issues by their Epic key
        epic_issues = {}
        for issue in issues:
            fields = issue.get("fields") or {}
            
            # Find which epic this issue belongs to
            epic_key = None
            if epic_link_field == "Parent":
                parent = fields.get("parent") or {}
                epic_key = parent.get("key")
            else:
                for cf in ["customfield_10014", "customfield_10000", "customfield_10008", "customfield_10009"]:
                    cf_val = fields.get(cf)
                    if cf_val:
                        epic_key = cf_val.get("key", str(cf_val)) if isinstance(cf_val, dict) else str(cf_val)
                        break
            
            if epic_key and epic_key in completion_by_epic:
                if epic_key not in epic_issues:
                    epic_issues[epic_key] = []
                status_name = (fields.get("status") or {}).get("name", "To Do")
                normalized_status = map_jira_status(status_name)
                score = 100 if normalized_status == "Done" else 0 if normalized_status == "To Do" else 50
                epic_issues[epic_key].append((score, status_name))

        # Calculate percentage for each Epic
        for epic_key, issues_with_status in epic_issues.items():
            if issues_with_status:
                scores = [score for score, _ in issues_with_status]
                completion_by_epic[epic_key] = f"{round(sum(scores) / len(scores))}%"
                raw_statuses = [str(status_name).strip().lower() for _, status_name in issues_with_status]
                normalized_raw_statuses = [re.sub(r"[\s_-]+", "", status) for status in raw_statuses]
                issue_status_counts_by_epic[epic_key] = {
                    "Closed / Resolved": sum(status in {"closed", "resolved"} for status in raw_statuses),
                    "To Do": sum(status == "todo" for status in normalized_raw_statuses),
                    "In progress": sum(
                        normalized_status not in {"closed", "resolved", "todo"}
                        for normalized_status in normalized_raw_statuses
                    ),
                }

    except Exception as e:
        logger.exception(f"fetch_epic_completion EXCEPTION: {e}")

    return (completion_by_epic, issue_status_counts_by_epic) if return_issue_counts else completion_by_epic

def build_quarterly_epic_progress_table(server, token, committed_label, quarter_label, title, position, auth_type, email, project_key="MYPROJECT"):
    """
    Query committed quarterly epics strictly requiring both configured labels.
    Calculates progress for each Epic from child issues linked via parent or Epic Link.
    """
    escaped_quarter_label = quarter_label.replace('"', '\\"')
    escaped_committed_label = committed_label.replace('"', '\\"')
    
    proj_jql = f'project = "{project_key.strip()}" AND ' if project_key.strip() else ''
    
    jql = (
        f'{proj_jql}issuetype = Epic '
        f'AND labels = "{escaped_committed_label}" '
        f'AND labels = "{escaped_quarter_label}" '
        'ORDER BY priority DESC, updated DESC'
    )
    
    try:
        # Query matching epics
        logger.debug(f"build_quarterly_epic_progress_table JQL: {jql}")
        res_df = fetch_jira_tickets_dataset(
            server,
            token,
            jql,
            is_sprint=False,
            auth_type=auth_type,
            email=email,
            include_raw_status=True
        )
        
        if res_df is None:
            logger.debug("build_quarterly_epic_progress_table: fetch_jira_tickets_dataset returned None")
            return None
    
        cols_to_keep = ["Key", "Summary", "Completion", "Closed / Resolved", "To Do", "In progress", "Epic", "Status", "Fix Version", "Assignee", "Labels"]
    
        if not res_df.empty:
            # Standardize Statuses
            if "Raw Status" in res_df.columns:
                res_df["Status"] = res_df["Raw Status"].apply(map_epic_status_for_completion_table)
            
            # Calculate individual epic completion percentages
            epic_keys = res_df["Key"].dropna().astype(str).tolist()
            completion_by_epic, issue_status_counts_by_epic = fetch_epic_completion(
                server,
                token,
                epic_keys,
                "Epic Link",
                auth_type=auth_type,
                email=email,
                return_issue_counts=True
            )
            res_df["Completion"] = res_df["Key"].map(completion_by_epic).fillna("-")
            for count_column in ["Closed / Resolved", "To Do", "In progress"]:
                res_df[count_column] = res_df["Key"].map(
                    lambda epic_key: issue_status_counts_by_epic.get(str(epic_key), {}).get(count_column, 0)
                )
            extra_df = res_df[[col for col in cols_to_keep if col in res_df.columns]].copy()
        else:
            extra_df = pd.DataFrame(columns=cols_to_keep)
    
        extra_df.insert(0, "Select", False)
        if "Labels" not in extra_df.columns:
            extra_df["Labels"] = ""
    
        result_table = {
            "title": title,
            "df": extra_df,
            "position": position,
            "hidden_cols": ["Epic", "Fix Version", "Assignee", "Labels"],
            "sort_by_team": True,
            "table_type": "quarterly_epic_progress"
        }
        logger.debug(f"build_quarterly_epic_progress_table success: table title={title}, records count={len(extra_df)}")
        return result_table
    except Exception as e:
        logger.exception(f"build_quarterly_epic_progress_table EXCEPTION: {e}")
        return None

def get_jira_auth_headers(token, auth_type, email):
    token_clean = token.strip()
    if token_clean.lower().startswith("bearer "):
        token_clean = token_clean[7:].strip()
    
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "X-Atlassian-Token": "nocheck"
    }
    auth = None
    if auth_type in ["Corporate Login (Username + Password)", "Jira Cloud/Server Basic (Email/User + Token)"]:
        auth = (email.strip(), token_clean)
    else:
        headers["Authorization"] = f"Bearer {token_clean}"
    return headers, auth

def fetch_jira_projects(server, token, auth_type, email):
    if not server or not token:
        return []
    headers, auth = get_jira_auth_headers(token, auth_type, email)
    url = f"{server.rstrip('/')}/rest/api/2/project"
    try:
        if auth:
            resp = requests.get(url, headers=headers, auth=auth, timeout=10)
        else:
            resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return [(p.get("key"), p.get("name")) for p in resp.json()]
    except Exception as e:
        print(f"[JIRA Helpers] Error fetching projects: {e}")
    return []

def fetch_jira_issue_types(server, token, auth_type, email, project_key):
    if not server or not token or not project_key:
        return []
    headers, auth = get_jira_auth_headers(token, auth_type, email)
    url = f"{server.rstrip('/')}/rest/api/2/project/{project_key}"
    try:
        if auth:
            resp = requests.get(url, headers=headers, auth=auth, timeout=10)
        else:
            resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            issue_types = data.get("issueTypes", [])
            return [it.get("name") for it in issue_types if not it.get("subtask")]
    except Exception as e:
        print(f"[JIRA Helpers] Error fetching issue types: {e}")
    return []

def create_jira_issue(server, token, auth_type, email, project_key, issue_type, summary, description, assignee="", labels="", parent_key="", story_points=None, fix_version="", linked_issue="", sprint=""):
    if not server or not token or not project_key or not issue_type or not summary:
        return "Error: Missing required parameters"
    headers, auth = get_jira_auth_headers(token, auth_type, email)
    url = f"{server.rstrip('/')}/rest/api/2/issue"
    
    fields = {
        "project": {"key": project_key},
        "summary": summary,
        "description": description,
        "issuetype": {"name": issue_type}
    }
    
    custom_fields = get_jira_creation_custom_fields(server, token, auth_type, email)
    story_points_id, sprint_id, epic_link_id = custom_fields
    
    if assignee:
        fields["assignee"] = {"name": assignee}
    if labels:
        fields["labels"] = [l.strip() for l in labels.split(",") if l.strip()]
    if parent_key:
        # Modern Jira uses parent for Epic and Parent links
        fields["parent"] = {"key": parent_key.strip()}
        if epic_link_id:
            fields[epic_link_id] = parent_key.strip()
    if fix_version:
        fields["fixVersions"] = [{"name": fix_version.strip()}]
    if story_points is not None and str(story_points).strip() != "":
        try:
            sp_val = float(story_points)
            if story_points_id:
                fields[story_points_id] = sp_val
            else:
                # Fallback to defaults if we didn't find the field dynamically
                fields["customfield_10016"] = sp_val
        except ValueError:
            pass
            
    if sprint:
        sp_id = resolve_sprint_id_by_name(server, token, auth_type, email, sprint)
        if sp_id is not None:
            if sprint_id:
                fields[sprint_id] = sp_id
            else:
                fields["customfield_10020"] = sp_id
            
    payload = {"fields": fields}
    
    try:
        if auth:
            resp = requests.post(url, headers=headers, auth=auth, json=payload, timeout=15)
        else:
            resp = requests.post(url, headers=headers, json=payload, timeout=15)
            
        if resp.status_code == 201:
            new_issue_key = resp.json().get("key")
            
            # Create an issue link if requested
            if linked_issue:
                link_url = f"{server.rstrip('/')}/rest/api/2/issueLink"
                link_payload = {
                    "type": {"name": "Relates"},
                    "inwardIssue": {"key": new_issue_key},
                    "outwardIssue": {"key": linked_issue.strip()}
                }
                if auth:
                    requests.post(link_url, headers=headers, auth=auth, json=link_payload, timeout=10)
                else:
                    requests.post(link_url, headers=headers, json=link_payload, timeout=10)
                    
            return new_issue_key
        else:
            return f"Error: {resp.status_code} - {resp.text}"
    except Exception as e:
        return f"Exception: {str(e)}"

def attach_files_to_jira_issue(server, token, auth_type, email, issue_key, uploaded_files):
    if not uploaded_files:
        return []
    url = f"{server.rstrip('/')}/rest/api/2/issue/{issue_key}/attachments"
    headers, auth = get_jira_auth_headers(token, auth_type, email)
    if "Content-Type" in headers:
        del headers["Content-Type"]
    headers["X-Atlassian-Token"] = "nocheck"
    
    results = []
    for f in uploaded_files:
        try:
            files = {'file': (f.name, f.getvalue(), f.type)}
            if auth:
                resp = requests.post(url, headers=headers, auth=auth, files=files, timeout=30)
            else:
                resp = requests.post(url, headers=headers, files=files, timeout=30)
            if resp.status_code in [200, 201]:
                results.append((f.name, True, ""))
            else:
                results.append((f.name, False, resp.text))
        except Exception as e:
            results.append((f.name, False, str(e)))
    return results

def search_jira_issues(server, token, auth_type, email, jql):
    """
    Search Jira using JQL and return simplified results.
    """
    if not server or not token:
        return {"error": "Jira server or token not configured"}
        
    url = f"{server.rstrip('/')}/rest/api/2/search"
    headers, auth = get_jira_auth_headers(token, auth_type, email)
    
    params = {
        "jql": jql,
        "maxResults": 30,
        "fields": "summary,status,description"
    }
    
    try:
        if auth:
            resp = requests.get(url, headers=headers, params=params, auth=auth, timeout=15)
        else:
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            
        if resp.status_code != 200:
            return {"error": f"Jira API returned {resp.status_code}: {resp.text}"}
            
        data = resp.json()
        results = []
        for issue in data.get("issues", []):
            key = issue.get("key", "")
            fields = issue.get("fields", {})
            summary = fields.get("summary", "")
            status = fields.get("status", {}).get("name", "")
            
            description = fields.get("description", "")
            if description and len(description) > 500:
                description = description[:500] + "..."
                
            issue_url = f"{server.rstrip('/')}/browse/{key}"
            
            results.append({
                "key": key,
                "summary": summary,
                "status": status,
                "description_excerpt": description,
                "url": issue_url
            })
            
        return {"results": results}
        
    except Exception as e:
        import traceback
        return {"error": f"Exception during Jira search: {e}\n{traceback.format_exc()}"}


RELEASE_READINESS_FIELDS = (
    "summary,description,reporter,comment,status,issuetype,assignee,fixVersions,"
    "customfield_14202,customfield_25263"
)


def fetch_release_issues_with_comments(server, token, auth_type, email, jql, page_size=50, fields=None):
    """Fetch all issues for a JQL (paginated) including description, reporter and all comments.

    `fields` (optional, comma-separated string) overrides the requested fields; GET only.
    """
    if not server or not token:
        return {"error": "Jira server or token not configured"}
    try:
        headers, auth = get_jira_auth_headers(token, auth_type, email)
        base = server.rstrip("/")
        issues = []
        start = 0
        while True:
            resp = requests.get(
                f"{base}/rest/api/2/search", headers=headers, auth=auth, timeout=30,
                params={"jql": jql, "startAt": start, "maxResults": page_size,
                        "fields": fields or "summary,description,reporter,comment,status,issuetype"},
            )
            if resp.status_code != 200:
                return {"error": f"Jira API returned {resp.status_code}: {resp.text}"}
            data = resp.json()
            batch = data.get("issues", [])
            issues.extend(batch)
            start += len(batch)
            if not batch or start >= data.get("total", 0):
                break
        for issue in issues:
            comment = (issue.get("fields") or {}).get("comment") or {}
            comments = comment.get("comments", [])
            if comment.get("total", len(comments)) > len(comments):
                all_c, c_start = [], 0
                while True:
                    r = requests.get(
                        f"{base}/rest/api/2/issue/{issue['key']}/comment", headers=headers, auth=auth,
                        timeout=30, params={"startAt": c_start, "maxResults": 100},
                    )
                    if r.status_code != 200:
                        return {"error": f"Jira API returned {r.status_code} reading comments of {issue['key']}: {r.text}"}
                    cd = r.json()
                    part = cd.get("comments", [])
                    all_c.extend(part)
                    c_start += len(part)
                    if not part or c_start >= cd.get("total", 0):
                        break
                issue["fields"]["comment"] = {"comments": all_c, "total": len(all_c)}
        return {"issues": issues}
    except Exception as e:
        return {"error": f"Exception during Jira search: {e}"}

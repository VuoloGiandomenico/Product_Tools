import pandas as pd
import requests
import os
import io
import re
import base64
from html import unescape
from datetime import datetime
from urllib.parse import quote
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether, Image as ReportLabImage, Flowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfgen import canvas
from reportlab.graphics.shapes import Drawing, Line, PolyLine
from PIL import Image as PILImage
from utils.pdf_helpers import (
    SmartKeepTogether, hex_to_reportlab_color, convert_markdown_to_pdf_rich_text,
    split_bugs_and_topics, sort_items_by_label_priority, get_team_label, get_custom_label, sort_items_by_type_and_epic,
    draw_background_landscape, NumberedCanvas, build_demos_pdf_block, build_next_releases_pdf_block,
    format_status_with_emoji, build_custom_extra_table_pdf_block, get_arrow_drawing,
    extract_numeric_version
)

def fetch_jira_tickets_dataset(server, token, query_val, query_mode="sprint", auth_type="Personal Access Token (Bearer PAT)", email="", config=None):
    if config is None: config = {}
    if not server or not token:
        raise ValueError("Please provide Jira Server URL and Personal Access Token (PAT).")
        return None
        
    # Safe cleanup of token inputs (removes accidental whitespaces or prepended 'Bearer ')
    token_clean = token.strip()
    if token_clean.lower().startswith("bearer "):
        token_clean = token_clean[7:].strip()
        
    if query_mode == "sprint":
        if query_val.isdigit():
            jql = f"sprint = {query_val}"
        else:
            jql = f"sprint = '{query_val}'"
    elif query_mode == "fix_version":
        jql = f"fixVersion = '{query_val}'"
    else:
        jql = query_val
        
    url = f"{server.rstrip('/')}/rest/api/2/search"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "X-Atlassian-Token": "nocheck"
    }

    
    auth = None
    if auth_type == "Corporate Login (Username + Password)" or auth_type == "Jira Cloud/Server Basic (Email/User + Token)":
        auth = (email.strip(), token_clean)
    else:
        headers["Authorization"] = f"Bearer {token_clean}"
        
    params = {
        "jql": jql,
        "maxResults": 100,
        "fields": "key,summary,status,fixVersions,parent,customfield_10000,customfield_10008,customfield_10009,customfield_10014,assignee,issuetype,labels"
    }

    
    try:
        if auth:
            response = requests.get(url, headers=headers, params=params, auth=auth, timeout=15)
        else:
            response = requests.get(url, headers=headers, params=params, timeout=15)

        
        if response.status_code != 200:
            raise ValueError(f"Jira API connection failed ({response.status_code}): {response.text}")
            return None
            
        try:
            data = response.json()
        except ValueError as json_err:
            raise ValueError("⚠️ **Jira returned a non-JSON response (Status 200 OK).**")
            print("This usually happens when your company's Single Sign-On (SSO) gateway, Proxy, or Firewall intercepts the API call and redirects it to a login webpage or CAPTCHA screen.")
            print("**First 1000 characters of the intercepted response:**")
            print(response.text[:1000], language="html")
            return None
            
        issues = data.get("issues", [])

        
        if not issues:
            print("No issues found matching the query parameters.")
            return pd.DataFrame()
            
        rows = []
        for issue in issues:
            fields = issue.get("fields", {})
            key = issue.get("key", "N/A")
            summary = fields.get("summary", "Untitled Task")
            
            # Status
            status_obj = fields.get("status") or {}
            status = status_obj.get("name", "To Do")
            resolution_obj = fields.get("resolution") or {}
            res_name = resolution_obj.get("name")
            if res_name:
                status = f"{status} [{res_name}]"
            
            # Fix Version
            fix_versions = fields.get("fixVersions", [])
            raw_fix_version = ", ".join([v.get("name", "") for v in fix_versions]) if fix_versions else ""
            fix_version = extract_numeric_version(raw_fix_version)
            
            # Assignee
            assignee_obj = fields.get("assignee")
            assignee = assignee_obj.get("displayName", "Unassigned") if assignee_obj else "Unassigned"
            
            # Epic detection
            epic = "-"
            epic_key = None
            
            for custom_field in ["customfield_10014", "customfield_10000", "customfield_10008", "customfield_10009"]:
                cf_val = fields.get(custom_field)
                if cf_val:
                    if isinstance(cf_val, dict):
                        epic_key = cf_val.get("key") or str(cf_val)
                    else:
                        epic_key = str(cf_val)
                    break
            
            parent_summary = None
            parent = fields.get("parent")
            if parent:
                parent_key = parent.get("key")
                parent_fields = parent.get("fields") or {}
                parent_summary = parent_fields.get("summary")
                if not epic_key:
                    epic_key = parent_key
            
            if epic_key:
                if parent and epic_key == parent.get("key") and parent_summary:
                    epic = f"{epic_key} - {parent_summary}"
                else:
                    epic = epic_key
                        
            # Issue Type detection & mapping
            issue_type_obj = fields.get("issuetype") or {}
            issue_type_raw = issue_type_obj.get("name", "Task")
            
            raw_lower = issue_type_raw.lower()
            issue_type = None
            if "technical sub-task" in raw_lower or "tech sub-task" in raw_lower or "technical subtask" in raw_lower or "tech subtask" in raw_lower or ("sub-task" in raw_lower and ("tech" in raw_lower or "technical" in raw_lower)):
                issue_type = "Technical Sub-task"
            elif "story" in raw_lower:
                issue_type = "User Story"
            elif "bug" in raw_lower:
                issue_type = "Bug"
            elif "improvement" in raw_lower:
                issue_type = "Improvement"
            elif "technical" in raw_lower or "tech" in raw_lower or "performance" in raw_lower or "scaling" in raw_lower or "infrastructure" in raw_lower:
                issue_type = "Technical Task"
            elif "sub-task" in raw_lower or "subtask" in raw_lower:
                issue_type = "Sub-task"
            elif "task" in raw_lower:
                issue_type = "Task"
            else:
                issue_type = issue_type_raw if config.get("inc_all", False) else "Technical Task"
                
            # Labels
            labels_list = fields.get("labels", [])
            labels_str = ", ".join(labels_list) if isinstance(labels_list, list) else ""
                
            rows.append({
                "Key": key,
                "Summary": summary,
                "Epic": epic,
                "Status": status,
                "Fix Version": fix_version,
                "Outlook": "",
                "Sprint Review": True,
                "Release Notes": True,
                "Assignee": assignee,
                "Demo": False,
                "Type": issue_type,
                "Labels": labels_str
            })
            
        # Bulk resolve Epic summaries from Jira
        epic_keys_to_resolve = set()
        for r in rows:
            ep_val = r["Epic"]
            if ep_val != "-" and " - " not in ep_val:
                epic_keys_to_resolve.add(ep_val)
                
        if epic_keys_to_resolve:
            try:
                keys_str = ",".join([f"'{k}'" for k in epic_keys_to_resolve])
                epic_jql = f"key in ({keys_str})"
                epic_url = f"{server.rstrip('/')}/rest/api/2/search"
                epic_params = {
                    "jql": epic_jql,
                    "fields": "key,summary",
                    "maxResults": 100
                }
                if auth:
                    epic_resp = requests.get(epic_url, headers=headers, params=epic_params, auth=auth, timeout=10)
                else:
                    epic_resp = requests.get(epic_url, headers=headers, params=epic_params, timeout=10)
                
                if epic_resp.status_code == 200:
                    epic_data = epic_resp.json()
                    epic_map = {}
                    for epic_issue in epic_data.get("issues", []):
                        e_key = epic_issue.get("key")
                        e_fields = epic_issue.get("fields") or {}
                        e_summary = e_fields.get("summary")
                        if e_key and e_summary:
                            epic_map[e_key] = f"{e_key} - {e_summary}"
                            
                    for r in rows:
                        ep_val = r["Epic"]
                        if ep_val in epic_map:
                            r["Epic"] = epic_map[ep_val]
            except Exception:
                pass
                
        return pd.DataFrame(rows)
    except Exception as e:
        raise ValueError(f"Exception connecting to Jira: {str(e)}")
        return None

def jira_request(server, token, path, params=None, auth_type="Personal Access Token (Bearer PAT)", email=""):
    headers = {"Accept": "application/json"}
    token_clean = token.strip().removeprefix("Bearer ").strip()
    auth = None
    if auth_type in ["Corporate Login (Username + Password)", "Jira Cloud/Server Basic (Email/User + Token)"]:
        auth = (email.strip(), token_clean)
    else:
        headers["Authorization"] = f"Bearer {token_clean}"
    return requests.get(f"{server.rstrip('/')}{path}", headers=headers, params=params, auth=auth, timeout=20)

def extract_release_version(version_name):
    match = re.search(r"DIGITAL:HUB\s*[-:]?\s*(v?\d+(?:\.\d+)+)", version_name or "", re.IGNORECASE)
    if not match:
        raise ValueError("The Jira version name must contain a value after 'DIGITAL:HUB', for example 'DIGITAL:HUB 33.0.0'.")
    return match.group(1).lstrip("v")

def find_history_value(row, candidates):
    normalized = {str(key).strip().lower(): value for key, value in row.items()}
    for candidate in candidates:
        for key, value in normalized.items():
            if candidate in key:
                return "" if pd.isna(value) else str(value).strip()
    return ""

def format_release_date(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return str(value)

def build_release_purpose_draft(resolved):
    """Create an editable, high-level release-purpose draft from release tickets."""
    intro = "The purpose of this release is to rollout the following functionalities:"
    if resolved is None or resolved.empty:
        return intro

    items = []
    grouped = resolved.copy()
    grouped["_epic"] = grouped["Epic"].fillna("-").astype(str).str.strip()
    for epic, epic_items in grouped[grouped["_epic"].ne("-")].groupby("_epic", sort=True):
        epic_name = re.sub(r"^[A-Z][A-Z0-9]+-\d+\s*-\s*", "", epic).strip()
        items.append(f"- {epic_name}:")

    no_epic = grouped[grouped["_epic"].eq("-")]
    if not no_epic.empty:
        has_bug = no_epic["Type"].astype(str).str.contains("bug", case=False, na=False).any()
        has_other = (~no_epic["Type"].astype(str).str.contains("bug", case=False, na=False)).any()
        if has_bug and has_other:
            items.append("- Bug fixing and general improvements across the platform.")
        elif has_bug:
            items.append("- Bug fixing and stability improvements across the platform.")
        else:
            items.append("- General improvements across the platform.")
    return intro + "\n\n" + "\n".join(items) if items else intro

def parse_confluence_history_rows(html):
    """Read Confluence storage tables without requiring optional lxml/bs4 packages."""
    for table_html in re.findall(r"<table[^>]*>(.*?)</table>", html, flags=re.IGNORECASE | re.DOTALL):
        rows = []
        for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, flags=re.IGNORECASE | re.DOTALL):
            cells = []
            for cell_html in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, flags=re.IGNORECASE | re.DOTALL):
                cell_html = re.sub(
                    r'<time[^>]*datetime=["\']([^"\']+)["\'][^>]*/?>',
                    r'\1',
                    cell_html,
                    flags=re.IGNORECASE
                )
                cell_text = re.sub(r"<br\s*/?>", "\n", cell_html, flags=re.IGNORECASE)
                cell_text = unescape(re.sub(r"<[^>]+>", "", cell_text)).strip()
                cells.append(cell_text)
            if cells:
                rows.append(cells)
        if len(rows) > 1:
            headers = rows[0]
            for row in rows[1:]:
                parsed_row = dict(zip(headers, row))
                parsed_row["__first_column__"] = row[0] if row else ""
                yield parsed_row

def fetch_release_history(confluence_server, token, release_version, auth_type, email):
    confluence_url = f"{confluence_server.rstrip('/')}/rest/api/content/201544094"
    headers = {"Accept": "application/json"}
    token_clean = token.strip().removeprefix("Bearer ").strip()
    auth = (email.strip(), token_clean) if auth_type in ["Corporate Login (Username + Password)", "Jira Cloud/Server Basic (Email/User + Token)"] else None
    if auth:
        response = requests.get(confluence_url, headers=headers, params={"expand": "body.storage"}, auth=auth, timeout=20)
    else:
        headers["Authorization"] = f"Bearer {token_clean}"
        response = requests.get(confluence_url, headers=headers, params={"expand": "body.storage"}, timeout=20)
    if response.status_code != 200:
        raise ValueError(f"Could not read Release history Documentation ({response.status_code}).")
    html = response.json().get("body", {}).get("storage", {}).get("value", "")
    for row in parse_confluence_history_rows(html):
        values = " ".join(str(value) for value in row.values())
        if re.search(rf"(?<!\d){re.escape(release_version)}(?!\d)", values):
            deploy_date = row.get("__first_column__") or find_history_value(row, ["deploy date", "date"])
            if not deploy_date:
                raise ValueError(f"Release history has a row for version {release_version}, but its Deploy Date (PROD) is still empty.")
            return {
                "deploy_date": format_release_date(deploy_date),
                "scs": find_history_value(row, ["scs"]),
                "service_center_change": find_history_value(row, ["service center change", "vw service"]),
                "test_protocols": find_history_value(row, ["test protocol", "e2e", "protocol"])
            }
    raise ValueError(f"There is no Release history row for version {release_version} yet.")

def prepare_release_notes_from_version_url(version_url, config):
    version_id_match = re.search(r"/versions/(\d+)", version_url.strip())
    if not version_id_match:
        raise ValueError("Paste a Jira version URL ending in /versions/<id>.")
    version_response = jira_request(config.get('jira_server'), config.get('jira_token'), f"/rest/api/2/version/{version_id_match.group(1)}", auth_type=config.get('jira_auth_method'), email=config.get('jira_email'))
    if version_response.status_code != 200:
        raise ValueError(f"Could not load the Jira version ({version_response.status_code}).")
    version_name = version_response.json().get("name", "")
    release_version = extract_release_version(version_name)
    history = fetch_release_history(config.get('conf_server'), config.get('conf_token'), release_version, config.get('jira_auth_method'), config.get('jira_email'))
    issues = fetch_jira_tickets_dataset(config.get('jira_server'), config.get('jira_token'), version_name, query_mode="fix_version", auth_type=config.get('jira_auth_method'), email=config.get('jira_email'))
    if issues is None:
        raise ValueError("Could not load the release tickets from Jira.")
    allowed = ["Bug", "User Story", "Task", "Improvement"]
    resolved = issues[issues["Type"].isin(allowed)].copy()
    residual_jql = (
        'issuetype = Bug AND status != Closed AND status != Resolved '
        'AND labels in (Severity_A, Severity_B) AND labels not in (Cognos)'
    )
    residual = fetch_jira_tickets_dataset(config.get('jira_server'), config.get('jira_token'), residual_jql, query_mode="custom", auth_type=config.get('jira_auth_method'), email=config.get('jira_email'))
    if residual is not None and not residual.empty:
        # Exclude only bugs assigned to this exact release. Bugs planned for a
        # future version (for example 34.0.0 while preparing 33.0.0) remain.
        residual = residual[
            ~residual["Fix Version"].astype(str).str.contains(re.escape(release_version), case=False, na=False)
        ].reset_index(drop=True)
    return {"version": release_version, "jira_version_name": version_name, "history": history, "resolved": resolved, "residual": residual if residual is not None else pd.DataFrame(), "purpose_draft": build_release_purpose_draft(resolved)}

def publish_release_note_to_history(prepared, pdf_bytes, filename, config):
    """Attach the PDF and link it from the Release history row for this version."""
    base_url = config.get('conf_server').rstrip("/")
    token = config.get('conf_token').strip().removeprefix("Bearer ").strip()
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    page_id = "201544094"
    page_response = requests.get(f"{base_url}/rest/api/content/{page_id}", headers=headers, params={"expand": "body.storage,version"}, timeout=20)
    if page_response.status_code != 200:
        raise ValueError(f"Could not read Release history Documentation ({page_response.status_code}).")
    page = page_response.json()

    upload_headers = {"Accept": "application/json", "Authorization": f"Bearer {token}", "X-Atlassian-Token": "nocheck"}
    attachment_list_response = requests.get(
        f"{base_url}/rest/api/content/{page_id}/child/attachment",
        headers=headers,
        params={"filename": filename, "limit": 1},
        timeout=20
    )
    existing_attachments = attachment_list_response.json().get("results", []) if attachment_list_response.status_code == 200 else []
    upload_url = f"{base_url}/rest/api/content/{page_id}/child/attachment"
    if existing_attachments:
        upload_url = f"{upload_url}/{existing_attachments[0]['id']}/data"
    attachment_response = requests.post(
        upload_url,
        headers=upload_headers,
        files={"file": (filename, pdf_bytes, "application/pdf")},
        timeout=30
    )
    if attachment_response.status_code not in (200, 201):
        raise ValueError(f"Could not upload the Release Note PDF ({attachment_response.status_code}).")

    html = page["body"]["storage"]["value"]
    version = prepared["version"]
    updated = False
    def replace_release_note_cell(match):
        nonlocal updated
        row_html = match.group(0)
        row_text = unescape(re.sub(r"<[^>]+>", " ", row_html))
        if updated or not re.search(rf"(?<!\d){re.escape(version)}(?!\d)", row_text):
            return row_html
        cells = list(re.finditer(r"<td[^>]*>.*?</td>", row_html, flags=re.IGNORECASE | re.DOTALL))
        if len(cells) < 5:
            return row_html
        link = f'<td><p><a href="{base_url}/download/attachments/{page_id}/{quote(filename)}">{filename}</a></p></td>'
        target = cells[4]
        updated = True
        return row_html[:target.start()] + link + row_html[target.end():]

    new_html = re.sub(r"<tr[^>]*>.*?</tr>", replace_release_note_cell, html, flags=re.IGNORECASE | re.DOTALL)
    if not updated:
        raise ValueError(f"Could not find the Release history row for version {version}.")
    update_response = requests.put(
        f"{base_url}/rest/api/content/{page_id}",
        headers={**headers, "Content-Type": "application/json"},
        json={
            "id": page_id,
            "type": "page",
            "title": page["title"],
            "body": {"storage": {"value": new_html, "representation": "storage"}},
            "version": {"number": page["version"]["number"] + 1}
        },
        timeout=30
    )
    if update_response.status_code != 200:
        detail = re.sub(r"\s+", " ", update_response.text)[:300]
        raise ValueError(f"PDF uploaded, but the history row could not be updated ({update_response.status_code}): {detail}")
    return f"{base_url}/pages/viewpage.action?pageId={page_id}"

def upload_pdf_to_confluence(server_url, auth_type, token, email, space_key, page_title, pdf_bytes, filename):
    """
    Finds or creates a Confluence page with the given page_title in space_key,
    then uploads pdf_bytes as a versioned attachment with filename.
    Returns the URL to the viewable Confluence page.
    """
    if not server_url or not token or not space_key or not page_title:
        raise Exception("Required configuration fields (URL, Token, Space Key, Page Title) cannot be empty.")
        
    base_url = server_url.rstrip("/")
    # Autocorrect typical Cloud URL structures if user missed '/wiki'
    if "atlassian.net" in base_url and not base_url.endswith("/wiki"):
        base_url = base_url + "/wiki"
        
    headers = {
        "Accept": "application/json"
    }
    
    auth = None
    if auth_type == "Corporate Login (Username + Password)" or auth_type == "Jira Cloud (Email + API Token)":
        if not email:
            raise Exception("Username/Email is required for Confluence Basic authentication.")
        auth = (email, token)
    else:
        # Bearer token PAT auth
        headers["Authorization"] = f"Bearer {token}"

        
    # Step 1: Find the target page by title in the specified space
    find_url = f"{base_url}/rest/api/content"
    params = {
        "title": page_title,
        "spaceKey": space_key,
        "expand": "version"
    }
    
    try:
        if auth:
            resp = requests.get(find_url, headers=headers, params=params, auth=auth, timeout=15)
        else:
            resp = requests.get(find_url, headers=headers, params=params, timeout=15)
    except Exception as e:
        raise Exception(f"Failed to connect to Confluence server: {str(e)}")
        
    if resp.status_code != 200:
        raise Exception(f"Failed to query Confluence page ({resp.status_code}): {resp.text}")
        
    results = resp.json().get("results", [])
    page_id = None
    
    body_html = (
        "<p>This page acts as a repository for automatically generated Sprint Reviews and Release Notes PDFs.</p>"
        "<p><strong>📂 Attached PDF Documents (Click to download):</strong></p>"
        "<ac:structured-macro ac:name=\"attachments\"></ac:structured-macro>"
    )
    
    if results:
        page_id = results[0]["id"]
        current_version = results[0]["version"]["number"]
        
        # Step 2a: Update existing page body to ensure the attachments macro is rendered
        update_url = f"{base_url}/rest/api/content/{page_id}"
        update_payload = {
            "id": page_id,
            "type": "page",
            "title": page_title,
            "space": {"key": space_key},
            "body": {
                "storage": {
                    "value": body_html,
                    "representation": "storage"
                }
            },
            "version": {
                "number": current_version + 1
            }
        }
        
        update_headers = headers.copy()
        update_headers["Content-Type"] = "application/json"
        
        try:
            if auth:
                requests.put(update_url, headers=update_headers, json=update_payload, auth=auth, timeout=15)
            else:
                requests.put(update_url, headers=update_headers, json=update_payload, timeout=15)
        except Exception:
            pass # Non-blocking update failure; proceed to upload attachment
    else:
        # Step 2b: Create the page if it doesn't exist
        create_url = f"{base_url}/rest/api/content"
        create_payload = {
            "type": "page",
            "title": page_title,
            "space": {"key": space_key},
            "body": {
                "storage": {
                    "value": body_html,
                    "representation": "storage"
                }
            }
        }
        
        # Prepare page creation headers (adding content-type)
        create_headers = headers.copy()
        create_headers["Content-Type"] = "application/json"
        
        if auth:
            cr_resp = requests.post(create_url, headers=create_headers, json=create_payload, auth=auth, timeout=15)
        else:
            cr_resp = requests.post(create_url, headers=create_headers, json=create_payload, timeout=15)
            
        if cr_resp.status_code not in (200, 201):
            raise Exception(f"Failed to create new Confluence page ({cr_resp.status_code}): {cr_resp.text}")
            
        page_id = cr_resp.json().get("id")

        
    if not page_id:
        raise Exception("Could not retrieve or create a valid Confluence Page ID.")
        
    # Step 3: Check if the attachment already exists on this page
    att_url = f"{base_url}/rest/api/content/{page_id}/child/attachment"
    att_params = {"limit": 100}
    
    if auth:
        ar_resp = requests.get(att_url, headers=headers, params=att_params, auth=auth, timeout=15)
    else:
        ar_resp = requests.get(att_url, headers=headers, params=att_params, timeout=15)
        
    att_results = ar_resp.json().get("results", []) if ar_resp.status_code == 200 else []
    attachment_id = None
    for att in att_results:
        if att.get("title") == filename:
            attachment_id = att.get("id")
            break
            
    # Step 4: Upload PDF bytes as attachment (handling new vs update/versioning)
    upload_headers = {
        "Accept": "application/json",
        "X-Atlassian-Token": "nocheck" # Critical CSRF bypass for attachments API
    }
    if auth_type == "Personal Access Token (Bearer PAT)" or auth_type == "Jira Server Token (Bearer)":
        upload_headers["Authorization"] = f"Bearer {token}"

        
    files = {
        "file": (filename, pdf_bytes, "application/pdf")
    }
    
    if attachment_id:
        # Update existing attachment (increments version)
        upload_url = f"{base_url}/rest/api/content/{page_id}/child/attachment/{attachment_id}/data"
    else:
        # Create new attachment
        upload_url = f"{base_url}/rest/api/content/{page_id}/child/attachment"
        
    if auth:
        up_resp = requests.post(upload_url, headers=upload_headers, files=files, auth=auth, timeout=20)
    else:
        up_resp = requests.post(upload_url, headers=upload_headers, files=files, timeout=20)
        
    if up_resp.status_code not in (200, 201):
        raise Exception(f"Failed to upload attachment to Confluence ({up_resp.status_code}): {up_resp.text}")
        
    page_link = f"{base_url}/pages/viewpage.action?pageId={page_id}"
    return page_link

# Helper to transform Hex colors into ReportLab Color objects
def build_prepared_release_notes_pdf(prepared, config):
    """Generate the standard MyProject Release Note from a prepared Jira version."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=54, rightMargin=54, topMargin=72, bottomMargin=60)
    styles = getSampleStyleSheet()
    primary = hex_to_reportlab_color(config.get('primary_color'))
    title = ParagraphStyle("RNTitle", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=20, leading=24, textColor=colors.black, spaceBefore=14, spaceAfter=10)
    body = ParagraphStyle("RNBody", parent=styles["Normal"], fontName="Helvetica", fontSize=10.5, leading=14, spaceAfter=8)
    header = ParagraphStyle("RNHead", parent=body, fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=colors.white)
    cell = ParagraphStyle("RNCell", parent=body, fontSize=8.5, leading=11)
    label = ParagraphStyle("RNLabel", parent=cell, fontName="Helvetica-Bold")
    story = [Spacer(1, 50), Paragraph("MyProject - Software Release Note", ParagraphStyle("CoverProject", parent=body, alignment=1, textColor=colors.HexColor("#64748B"))), Paragraph("Release Notes", ParagraphStyle("CoverTitle", parent=title, alignment=1, fontSize=32, leading=38, textColor=primary, spaceBefore=18)), Paragraph(f"Version: {prepared['version']}", ParagraphStyle("CoverVersion", parent=body, alignment=1, fontSize=18, leading=22, textColor=colors.HexColor("#334155"))), Spacer(1, 250), Paragraph("This documentation outlines the software development results of Digital:Hub for the specified release.", ParagraphStyle("CoverFooter", parent=body, alignment=1)), PageBreak()]

    story += [Paragraph("1. Release Purpose", title), Paragraph(config.get('release_purpose').replace("\n", "<br/>"), body), Paragraph("2. Software Release Information", title)]
    history = prepared["history"]
    metadata = [("Release version (MyProject)", prepared["version"]), ("Service Center Change number", history.get("service_center_change") or "-"), ("Deploy Date (PROD)", history.get("deploy_date") or "-"), ("SCS", history.get("scs") or "-")]
    meta_data = [[Paragraph(key, label), Paragraph(value.replace("\n", "<br/>"), cell)] for key, value in metadata]
    meta_table = Table(meta_data, colWidths=[225, 279])
    meta_table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), .75, colors.black), ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#E7E7E7")), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("LEFTPADDING", (0, 0), (-1, -1), 7)]))
    story += [meta_table, Paragraph("3. Release: Resolved Issues", title), Paragraph("Number of resolved issues by Issue Type:", body)]
    resolved = prepared["resolved"].copy()
    display_types = [("Stories", "User Story"), ("Task", "Task"), ("Bugs", "Bug"), ("Improvements", "Improvement")]
    counts = [[Paragraph("Issue Type", header), Paragraph("Amount", header)]] + [[Paragraph(name, cell), Paragraph(str((resolved["Type"] == issue_type).sum()), cell)] for name, issue_type in display_types]
    count_table = Table(counts, colWidths=[250, 100])
    count_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#BFBFBF")), ("GRID", (0, 0), (-1, -1), .4, colors.HexColor("#777777")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story += [count_table, Spacer(1, 12), Paragraph("List of all resolved issues (Jira ticket number) for this release:", body)]
    resolved["_epic"] = resolved["Epic"].fillna("-").astype(str)
    resolved = resolved.sort_values(["_epic", "Key"])
    issue_rows = [[Paragraph("Issue Type", header), Paragraph("Key", header), Paragraph("Summary", header)]]
    epic_group_rows = []
    last_epic = None
    for _, row in resolved.iterrows():
        epic = str(row["_epic"])
        if epic != last_epic:
            last_epic = epic
            epic_group_rows.append(len(issue_rows))
            issue_rows.append([Paragraph(f"Epic: {epic}", ParagraphStyle("RNEpic", parent=cell, fontName="Helvetica-Bold")), "", ""])
        issue_rows.append([Paragraph(str(row["Type"]), cell), Paragraph(str(row["Key"]), cell), Paragraph(str(row["Summary"]), cell)])
    issues_table = Table(issue_rows, colWidths=[105, 125, 274], repeatRows=1)
    issue_table_style = [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#BFBFBF")), ("GRID", (0, 0), (-1, -1), .6, colors.black), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
    for row_index in epic_group_rows:
        issue_table_style.extend([("SPAN", (0, row_index), (-1, row_index)), ("BACKGROUND", (0, row_index), (-1, row_index), colors.HexColor("#E7E7E7"))])
    issues_table.setStyle(TableStyle(issue_table_style))
    story += [issues_table, Paragraph("4. Release: All known residual anomalies", title), Paragraph("List of all known bugs/defects (Severity A or B) not included in this release:", body)]
    residual = prepared["residual"]
    residual_rows = [[Paragraph("Issue Type", header), Paragraph("Key", header), Paragraph("Summary", header)]]
    for _, row in residual.iterrows():
        residual_rows.append([Paragraph("Bug", cell), Paragraph(str(row["Key"]), cell), Paragraph(str(row["Summary"]), cell)])
    residual_table = Table(residual_rows, colWidths=[105, 125, 274], repeatRows=1)
    residual_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#BFBFBF")), ("GRID", (0, 0), (-1, -1), .6, colors.black), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    story += [residual_table, Paragraph("5. Test protocols", title)]
    link = "https://your-company.atlassian.net/confluence/x/nlEDD"
    story.append(Paragraph(f'E2E test protocols: <link href="{link}" color="blue">E2E test protocols for release</link>', body))
    doc.build(story, canvasmaker=NumberedCanvas)
    buffer.seek(0)
    return buffer

def build_release_notes_pdf(overview_df, config):
    if config.get("prepared_release_notes") is not None:
        return build_prepared_release_notes_pdf(config.get('prepared_release_notes'), config)
    pdf_buffer = io.BytesIO()
    
    # Setup document
    doc = SimpleDocTemplate(
        pdf_buffer,
        pagesize=letter,
        leftMargin=54,
        rightMargin=54,
        topMargin=72,
        bottomMargin=72
    )
    
    styles = getSampleStyleSheet()
    primary_color_hex = config.get('primary_color')
    primary_color = hex_to_reportlab_color(primary_color_hex)
    
    # Custom styles
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=28,
        leading=32,
        textColor=primary_color,
        spaceAfter=12
    )
    
    subtitle_style = ParagraphStyle(
        'DocSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=12.5,
        leading=17,
        textColor=colors.HexColor("#475569"),
        spaceAfter=20
    )
    
    intro_style = ParagraphStyle(
        'DocIntro',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=11.0,
        leading=16.5,
        textColor=colors.HexColor("#334155"),
        spaceAfter=20
    )
    
    section_title_style = ParagraphStyle(
        'SecTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=15,
        leading=19,
        textColor=primary_color,
        spaceBefore=15,
        spaceAfter=8
    )
    
    cell_header_style = ParagraphStyle(
        'CellHeader',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8.0,
        leading=10,
        textColor=colors.white
    )
    
    cell_body_style = ParagraphStyle(
        'CellBody',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=7.5,
        leading=9.5,
        textColor=colors.HexColor("#1E293B")
    )
    
    cell_body_bold_style = ParagraphStyle(
        'CellBodyBold',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=7.5,
        leading=9.5,
        textColor=colors.HexColor("#1E293B")
    )
    
    sub_section_title_style = ParagraphStyle(
        'SubSecTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=12.5,
        leading=16,
        textColor=colors.HexColor("#475569"),
        spaceBefore=12,
        spaceAfter=5
    )

    # Cover Page Styles
    cover_project_style = ParagraphStyle(
        'CoverProject',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=16,
        textColor=colors.HexColor("#64748B"),
        alignment=1, # Center
        spaceAfter=15
    )
    
    cover_title_style = ParagraphStyle(
        'CoverTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=32,
        leading=38,
        textColor=primary_color,
        alignment=1, # Center
        spaceAfter=10
    )
    
    cover_subtitle_style = ParagraphStyle(
        'CoverSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=18,
        leading=22,
        textColor=colors.HexColor("#334155"),
        alignment=1, # Center
        spaceAfter=25
    )
    
    cover_date_style = ParagraphStyle(
        'CoverDate',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#64748B"),
        alignment=1, # Center
        spaceAfter=30
    )

    story = []

    # --- STARTING COVER PAGE ---
    app_version = "v1.3.0"
    if config.get('app_version') is not None and str(config.get('app_version')).strip() != "":
        app_version = str(config.get('app_version')).strip()
        
    from datetime import datetime
    current_date = datetime.now().strftime("%d-%m-%Y")
    
    story.append(Spacer(1, 50))
    story.append(Paragraph(config.get('project_name').upper(), cover_project_style))
    story.append(Paragraph("Release Notes", cover_title_style))
    story.append(Paragraph(f"Version: {app_version}", cover_subtitle_style))
    story.append(Paragraph(f"Date: {current_date}", cover_date_style))
    story.append(Spacer(1, 20))
    
    cover_image_path = config.get('rn_cover_temp_path')
    if cover_image_path and os.path.exists(cover_image_path):
        try:
            pil_img = PILImage.open(cover_image_path)
            orig_w, orig_h = pil_img.size
            max_w = 400  # max width in points
            max_h = 300  # max height in points
            scale = min(max_w / orig_w, max_h / orig_h)
            img = ReportLabImage(cover_image_path, width=orig_w * scale, height=orig_h * scale)
            img.hAlign = 'CENTER'
            story.append(img)
            story.append(Spacer(1, 20))
        except Exception:
            pass
            
    story.append(PageBreak())
    # --- END OF COVER PAGE ---
    
    # 1. Document Title
    story.append(Paragraph("Release Notes", title_style))
    story.append(Paragraph("Release highlights and upcoming features.", subtitle_style))
    story.append(Spacer(1, 5))
    
    # 2. Render Custom User Intro Paragraphs
    intro_markdown = config.get('release_notes_intro')
    intro_html = convert_markdown_to_pdf_rich_text(intro_markdown)
    story.append(Paragraph(intro_html, intro_style))
    
    # --- ADDITIONAL METADATA & ITEMS RESUME SECTIONS ---
    meta_label_style = ParagraphStyle(
        'MetaLabel',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#334155")
    )
    
    meta_val_style = ParagraphStyle(
        'MetaVal',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#475569")
    )
    
    meta_data = []
    if config.get('change_request'):
        meta_data.append([
            Paragraph("Change Request:", meta_label_style),
            Paragraph(config.get('change_request'), meta_val_style)
        ])
    if config.get('release_date'):
        date_str = config.get('release_date').strftime("%d-%m-%Y") if hasattr(config.get('release_date'), 'strftime') else str(config.get('release_date'))
        meta_data.append([
            Paragraph("Release Date:", meta_label_style),
            Paragraph(date_str, meta_val_style)
        ])
        
    if meta_data:
        meta_table = Table(meta_data, colWidths=[110, 394])
        meta_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ]))
        story.append(meta_table)
        story.append(Spacer(1, 10))
        
    if config.get('show_items_resume') and overview_df is not None and not overview_df.empty:
        type_counts = overview_df['Type'].value_counts()
        if not type_counts.empty:
            story.append(Paragraph("Items Resume", sub_section_title_style))
            
            resume_data = [[
                Paragraph("Item Type", cell_header_style),
                Paragraph("Count", cell_header_style)
            ]]
            
            for item_type, count in type_counts.items():
                resume_data.append([
                    Paragraph(str(item_type), cell_body_bold_style),
                    Paragraph(str(count), cell_body_style)
                ])
                
            resume_table = Table(resume_data, colWidths=[150, 60])
            resume_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), primary_color),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('TOPPADDING', (0, 0), (-1, -1), 3),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                ('LEFTPADDING', (0, 0), (-1, -1), 7),
                ('RIGHTPADDING', (0, 0), (-1, -1), 7),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
            ]))
            story.append(resume_table)
            story.append(Spacer(1, 15))
    # ---------------------------------------------------
    
    # 3. Overview Section
    topics_ov, bugs_ov = split_bugs_and_topics(overview_df)
    
    if not topics_ov.empty or not bugs_ov.empty:
        prefix_flowables = [
            Paragraph("Overview:", section_title_style)
        ]
        
        # 3a. Delivered Topics Sub-section
        if not topics_ov.empty:
            topics_flowables = []
            if prefix_flowables:
                topics_flowables.extend(prefix_flowables)
                prefix_flowables = []
            topics_flowables.append(Paragraph("Delivered Topics", sub_section_title_style))
            # Col Widths: Total = 504pt
            # Reference: 60pt, Epic Theme: 90pt, Delivered Capability: 274pt, Release Version: 80pt
            table_data = [[
                Paragraph("Epic", cell_header_style),
                Paragraph("Key", cell_header_style),
                Paragraph("Summary", cell_header_style),
                Paragraph("Fix Version", cell_header_style)
            ]]
            
            sorted_topics = sort_items_by_type_and_epic(topics_ov)
            
            last_epic = None
            for _, row in sorted_topics.iterrows():
                epic_val = str(row['Epic']).strip() if pd.notna(row['Epic']) else "-"
                if epic_val in ["", "No Epic", "nan"]:
                    epic_val = "-"
                fv_val = extract_numeric_version(row['Fix Version'])
                if not fv_val:
                    fv_val = "-"
                    
                display_epic = epic_val
                if display_epic == last_epic:
                    if display_epic == "-":
                        epic_cell = Paragraph("-", cell_body_style)
                    else:
                        epic_cell = get_arrow_drawing(colors.HexColor("#64748B"))
                else:
                    last_epic = display_epic
                    epic_cell = Paragraph(display_epic, cell_body_style)
                    
                table_data.append([
                    epic_cell,
                    Paragraph(str(row['Key']), cell_body_bold_style),
                    Paragraph(str(row['Summary']), cell_body_style),
                    Paragraph(fv_val, cell_body_style)
                ])
                
            changelog_table = Table(
                table_data,
                colWidths=[105, 95, 254, 50]
            )
            changelog_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), primary_color),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('TOPPADDING', (0, 0), (-1, -1), 3),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                ('LEFTPADDING', (0, 0), (-1, -1), 7),
                ('RIGHTPADDING', (0, 0), (-1, -1), 7),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
            ]))
            topics_flowables.append(changelog_table)
            topics_flowables.append(Spacer(1, 10))
            story.append(SmartKeepTogether(topics_flowables))
            
        # 3b. Resolved Bugs Sub-section
        if not bugs_ov.empty:
            bugs_flowables = []
            if prefix_flowables:
                bugs_flowables.extend(prefix_flowables)
                prefix_flowables = []
            bugs_flowables.append(Paragraph("Resolved Bugs", sub_section_title_style))
            custom_bug_col = config.get("custom_bug_column", {})
            has_custom_col = custom_bug_col.get("enabled", False)
            custom_col_name = custom_bug_col.get("name", "Custom")
            custom_col_labels = custom_bug_col.get("labels", [])

            header_row = [
                Paragraph("Epic", cell_header_style),
                Paragraph("Key", cell_header_style),
                Paragraph("Summary", cell_header_style),
                Paragraph("Fix Version", cell_header_style)
            ]
            if has_custom_col:
                header_row.insert(3, Paragraph(custom_col_name, cell_header_style))
                
            bug_data = [header_row]
            
            sorted_bugs = bugs_ov.sort_values("Epic")
            
            last_epic = None
            for _, row in sorted_bugs.iterrows():
                epic_val = str(row['Epic']).strip() if pd.notna(row['Epic']) else "-"
                if epic_val in ["", "No Epic", "nan"]:
                    epic_val = "-"
                fv_val = extract_numeric_version(row['Fix Version'])
                if not fv_val:
                    fv_val = "-"
                    
                display_epic = epic_val
                if display_epic == last_epic:
                    if display_epic == "-":
                        epic_cell = Paragraph("-", cell_body_style)
                    else:
                        epic_cell = get_arrow_drawing(colors.HexColor("#64748B"))
                else:
                    last_epic = display_epic
                    epic_cell = Paragraph(display_epic, cell_body_style)
                    
                row_data = [
                    epic_cell,
                    Paragraph(str(row['Key']), cell_body_bold_style),
                    Paragraph(str(row['Summary']), cell_body_style),
                    Paragraph(fv_val, cell_body_style)
                ]
                
                if has_custom_col:
                    custom_label_val = get_custom_label(row.get('Labels', ''), custom_col_labels)
                    row_data.insert(3, Paragraph(custom_label_val, cell_body_style))
                    
                bug_data.append(row_data)
                
            if has_custom_col:
                col_widths = [115, 95, 174, 70, 50]
            else:
                col_widths = [115, 95, 244, 50]
                
            bugs_table = Table(
                bug_data,
                colWidths=col_widths
            )
            bugs_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), primary_color),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('TOPPADDING', (0, 0), (-1, -1), 3),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                ('LEFTPADDING', (0, 0), (-1, -1), 7),
                ('RIGHTPADDING', (0, 0), (-1, -1), 7),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
            ]))
            bugs_flowables.append(bugs_table)
            bugs_flowables.append(Spacer(1, 10))
            story.append(SmartKeepTogether(bugs_flowables))
        
    # Render custom extra tables (Release Notes, portrait)
    for t in config.get('custom_tables'):
        df_ext = t["df"]
        if df_ext is not None and not df_ext.empty:
            df_render = df_ext.drop(columns=["Select"]) if "Select" in df_ext.columns else df_ext
            story.append(PageBreak())
            extra_title = t["title"] if t["title"].strip() != "" else "Special Metrics Overview"
            extra_blocks = build_custom_extra_table_pdf_block(df_render, primary_color, styles, is_landscape=False)
            if extra_blocks:
                story.append(SmartKeepTogether([
                    Paragraph(extra_title, section_title_style),
                    Spacer(1, 10)
                ] + extra_blocks))
            

        
    doc.build(story, canvasmaker=NumberedCanvas)
    pdf_buffer.seek(0)
    return pdf_buffer



# ---------------------------------------------------------

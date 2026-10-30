import truststore
truststore.inject_into_ssl()  # verify TLS with the OS trust store (corporate proxies such as Zscaler)
from fastapi import FastAPI, Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
import hmac
import logging
import os
import uuid
from typing import Optional
from dotenv import load_dotenv
import re

def build_search_query(query: str, project_or_space_key: str, key_name: str = "project") -> str:
    """Helper to convert plain text into a valid JQL/CQL query with the correct project/space key."""
    if "=" not in query and "~" not in query:
        stop_words = {"topic", "topics", "info", "information", "about", "what", "is", "the", "a", "an", "and", "or", "for", "search", "find", "show", "me", "explain"}
        words = [w for w in re.split(r'\W+', query) if w and w.lower() not in stop_words]
        if words:
            query = " AND ".join([f'text ~ "{w}*"' for w in words])
        else:
            query = f'text ~ "{query}"'
    
    if project_or_space_key:
        if key_name in query.lower():
            query = re.sub(rf'\b{key_name}\s*=\s*("[^"]+"|[^ )]+)', f'{key_name} = "{project_or_space_key}"', query, flags=re.IGNORECASE)
        else:
            query = f'{key_name} = "{project_or_space_key}" AND ({query})'
            
    return query

from utils.ai_helpers import generate_issue_with_ollama
from utils.jira_helpers import create_jira_issue

load_dotenv(override=True)

logger = logging.getLogger("po_tools.api")


def _internal_error(err) -> HTTPException:
    """Log the real error server-side and return a generic 500 with a reference id."""
    ref = uuid.uuid4().hex[:8]
    if isinstance(err, BaseException):
        logger.error("API error ref=%s", ref, exc_info=err)
    else:
        logger.error("API error ref=%s: %s", ref, err)
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"Internal error (ref {ref}). Check the server logs for details.",
    )


app = FastAPI(
    title="PO Tools API",
    description="API for Agentic interaction with PO Tools",
    version="1.0.0"
)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)

def get_api_key(api_key: str = Security(api_key_header)) -> str:
    expected_api_key = os.getenv("API_KEY")
    if not expected_api_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API_KEY not configured on the server environment.",
        )
    if not hmac.compare_digest(api_key.encode("utf-8"), expected_api_key.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key",
        )
    return api_key

class GenerateIssueRequest(BaseModel):
    model_name: str
    issue_type: str
    input_text: str

class GenerateIssueResponse(BaseModel):
    generated_description: str

@app.post("/api/v1/generate-issue", response_model=GenerateIssueResponse, tags=["AI Issue Creator"])
async def generate_issue(req: GenerateIssueRequest, api_key: str = Depends(get_api_key)):
    ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
    result = generate_issue_with_ollama(
        model_name=req.model_name,
        issue_type=req.issue_type,
        input_text=req.input_text,
        base_url=ollama_url
    )
    if result.startswith("Error") or result.startswith("Exception"):
        raise _internal_error(result)
    
    return GenerateIssueResponse(generated_description=result)

class CreateJiraIssueRequest(BaseModel):
    project_key: str
    issue_type: str
    summary: str
    description: str
    assignee: Optional[str] = ""
    labels: Optional[str] = ""
    parent: Optional[str] = ""
    story_points: Optional[str] = ""
    fix_version: Optional[str] = ""
    linked_issue: Optional[str] = ""
    sprint: Optional[str] = ""

class CreateJiraIssueResponse(BaseModel):
    issue_key: str
    issue_link: str

@app.post("/api/v1/create-jira-issue", response_model=CreateJiraIssueResponse, tags=["AI Issue Creator"])
async def create_jira_issue_endpoint(req: CreateJiraIssueRequest, api_key: str = Depends(get_api_key)):
    jira_server = os.getenv("JIRA_SERVER", "")
    jira_token = os.getenv("JIRA_API_TOKEN", "")
    jira_auth_method = os.getenv("JIRA_AUTH_METHOD", "Personal Access Token (Bearer PAT)")
    jira_email = os.getenv("JIRA_EMAIL", "")

    if not jira_server or not jira_token:
        raise HTTPException(status_code=400, detail="Jira Server or Token not configured on the server environment.")

    result = create_jira_issue(
        server=jira_server,
        token=jira_token,
        auth_type=jira_auth_method,
        email=jira_email,
        project_key=req.project_key,
        issue_type=req.issue_type,
        summary=req.summary,
        description=req.description,
        assignee=req.assignee,
        labels=req.labels,
        parent_key=req.parent,
        story_points=req.story_points,
        fix_version=req.fix_version,
        linked_issue=req.linked_issue,
        sprint=req.sprint
    )

    if result.startswith("Error") or result.startswith("Exception"):
        raise _internal_error(result)

    issue_link = f"{jira_server.rstrip('/')}/browse/{result}"
    return CreateJiraIssueResponse(issue_key=result, issue_link=issue_link)

from utils.kpi_helpers import fetch_sprint_kpi_data, publish_kpis_to_confluence
from typing import List

class CalculateKPIsRequest(BaseModel):
    sprint_query: str
    selected_types: Optional[List[str]] = None

@app.post("/api/v1/kpis/calculate", tags=["Sprint KPIs"])
async def calculate_kpis(req: CalculateKPIsRequest, api_key: str = Depends(get_api_key)):
    jira_server = os.getenv("JIRA_SERVER", "")
    jira_token = os.getenv("JIRA_API_TOKEN", "")
    jira_auth_method = os.getenv("JIRA_AUTH_METHOD", "Personal Access Token (Bearer PAT)")
    jira_email = os.getenv("JIRA_EMAIL", "")

    if not jira_server or not jira_token:
        raise HTTPException(status_code=400, detail="Jira Server or Token not configured.")

    selected_types = req.selected_types
    if selected_types is None:
        default_types_str = os.getenv("DEFAULT_INCLUDED_TYPES", "User Story, Task, Improvement, Bug")
        selected_types = [t.strip() for t in default_types_str.split(",") if t.strip()]

    res = fetch_sprint_kpi_data(
        server=jira_server,
        token=jira_token,
        auth_type=jira_auth_method,
        email=jira_email,
        sprint_query=req.sprint_query,
        selected_types=selected_types
    )
    
    if res.get("error"):
        raise _internal_error(res["error"])
        
    import numpy as np
    df_records = []
    df = res.get("df")
    if df is not None and not df.empty:
        df = df.replace({np.nan: None})
        df_records = df.to_dict(orient="records")
    
    return {
        "sprint_name": res.get("sprint_name"),
        "sprint_start": res.get("sprint_start"),
        "sprint_end": res.get("sprint_end"),
        "releases_count": res.get("releases_count"),
        "global_open_bugs": res.get("global_open_bugs"),
        "global_critical_bugs": res.get("global_critical_bugs"),
        "gh_added_keys": res.get("gh_added_keys"),
        "issues": df_records
    }

class PublishKPIsRequest(BaseModel):
    space_key: str
    page_title: str
    sprint_val: str
    sprint_name: str
    metrics: dict

@app.post("/api/v1/kpis/publish", tags=["Sprint KPIs"])
async def publish_kpis(req: PublishKPIsRequest, api_key: str = Depends(get_api_key)):
    conf_server = os.getenv("CONFLUENCE_SERVER", "")
    conf_token = os.getenv("CONFLUENCE_API_TOKEN", "")
    jira_auth_method = os.getenv("JIRA_AUTH_METHOD", "Personal Access Token (Bearer PAT)")
    jira_email = os.getenv("JIRA_EMAIL", "")

    if not conf_server or not conf_token:
        raise HTTPException(status_code=400, detail="Confluence Server or Token not configured.")

    try:
        page_url = publish_kpis_to_confluence(
            server_url=conf_server,
            auth_type=jira_auth_method,
            token=conf_token,
            email=jira_email,
            space_key=req.space_key,
            page_title=req.page_title,
            sprint_val=req.sprint_val,
            sprint_name=req.sprint_name,
            metrics=req.metrics
        )
        return {"success": True, "page_url": page_url}
    except Exception as e:
        raise _internal_error(e) from e

from utils.release_helpers import prepare_release_notes_from_version_url, publish_release_note_to_history, build_prepared_release_notes_pdf
import base64

class PrepareReleaseNotesRequest(BaseModel):
    version_url: str
    
@app.post("/api/v1/release-notes/prepare", tags=["Release Notes"])
async def prepare_release_notes(req: PrepareReleaseNotesRequest, api_key: str = Depends(get_api_key)):
    jira_server = os.getenv("JIRA_SERVER", "")
    jira_token = os.getenv("JIRA_API_TOKEN", "")
    conf_server = os.getenv("CONFLUENCE_SERVER", "")
    conf_token = os.getenv("CONFLUENCE_API_TOKEN", "")
    jira_auth_method = os.getenv("JIRA_AUTH_METHOD", "Personal Access Token (Bearer PAT)")
    jira_email = os.getenv("JIRA_EMAIL", "")
    
    if not jira_server or not jira_token or not conf_server or not conf_token:
        raise HTTPException(status_code=400, detail="Jira or Confluence credentials missing in server config.")
        
    config = {
        "jira_server": jira_server,
        "jira_token": jira_token,
        "conf_server": conf_server,
        "conf_token": conf_token,
        "jira_auth_method": jira_auth_method,
        "jira_email": jira_email
    }
    
    try:
        prepared = prepare_release_notes_from_version_url(req.version_url, config)
        
        # Convert DataFrames to dicts so FastAPI can serialize them
        if 'resolved' in prepared and not prepared['resolved'].empty:
            prepared['resolved'] = prepared['resolved'].to_dict(orient="records")
        else:
            prepared['resolved'] = []
            
        if 'residual' in prepared and not prepared['residual'].empty:
            prepared['residual'] = prepared['residual'].to_dict(orient="records")
        else:
            prepared['residual'] = []
            
        return prepared
    except Exception as e:
        raise _internal_error(e) from e

class PublishReleaseNotesRequest(BaseModel):
    version: str
    pdf_base64: str
    filename: str

@app.post("/api/v1/release-notes/publish", tags=["Release Notes"])
async def publish_release_notes(req: PublishReleaseNotesRequest, api_key: str = Depends(get_api_key)):
    conf_server = os.getenv("CONFLUENCE_SERVER", "")
    conf_token = os.getenv("CONFLUENCE_API_TOKEN", "")
    
    if not conf_server or not conf_token:
        raise HTTPException(status_code=400, detail="Confluence credentials missing.")
        
    config = {
        "conf_server": conf_server,
        "conf_token": conf_token
    }
    
    try:
        pdf_bytes = base64.b64decode(req.pdf_base64)
        prepared_stub = {"version": req.version}
        page_url = publish_release_note_to_history(prepared_stub, pdf_bytes, req.filename, config)
        return {"success": True, "page_url": page_url}
    except Exception as e:
        raise _internal_error(e) from e

from typing import Dict, Any

class GenerateReleaseNotesPdfRequest(BaseModel):
    prepared: Dict[str, Any]
    primary_color: Optional[str] = "#0EA5E9"
    project_name: Optional[str] = "MyProject"
    app_version: Optional[str] = "v1.3.0"
    release_purpose: Optional[str] = ""

@app.post("/api/v1/release-notes/generate-pdf", tags=["Release Notes"])
async def generate_release_notes_pdf(req: GenerateReleaseNotesPdfRequest, api_key: str = Depends(get_api_key)):
    config = {
        "primary_color": req.primary_color,
        "project_name": req.project_name,
        "app_version": req.app_version,
        "release_purpose": req.release_purpose or req.prepared.get("purpose_draft", "")
    }
    
    # We need to convert list of dicts back to pandas DataFrames
    import pandas as pd
    prepared = req.prepared.copy()
    if 'resolved' in prepared and isinstance(prepared['resolved'], list):
        prepared['resolved'] = pd.DataFrame(prepared['resolved'])
    if 'residual' in prepared and isinstance(prepared['residual'], list):
        prepared['residual'] = pd.DataFrame(prepared['residual'])
        
    try:
        pdf_buffer = build_prepared_release_notes_pdf(prepared, config)
        pdf_bytes = pdf_buffer.getvalue()
        pdf_base64 = base64.b64encode(pdf_bytes).decode('utf-8')
        return {"success": True, "pdf_base64": pdf_base64}
    except Exception as e:
        raise _internal_error(e) from e

from utils.sprint_review_helpers import build_sprint_review_pdf
from utils.release_helpers import upload_pdf_to_confluence

class GenerateSprintReviewPdfRequest(BaseModel):
    overview_df: list
    outlook_df: list
    primary_color: Optional[str] = "#3B82F6"
    project_name: Optional[str] = "PO Tools Enterprise"
    sprint_number: Optional[str] = "Sprint 14"
    sprint_welcome_message: Optional[str] = "Welcome to the Sprint Review session."
    sr_logo_temp_path: Optional[str] = None
    sr_cover_temp_path: Optional[str] = None
    extra_table_df: Optional[list] = None
    extra_table_title: Optional[str] = ""
    sprint_review_label_order: Optional[list] = ["Frontend_Team", "Backend_Team", "Architecture_Team"]
    custom_tables: Optional[list] = []

@app.post("/api/v1/sprint-review/generate-pdf", tags=["Sprint Review"])
async def generate_sprint_review_pdf(req: GenerateSprintReviewPdfRequest, api_key: str = Depends(get_api_key)):
    config = req.dict()
    import pandas as pd
    
    overview_df = pd.DataFrame(req.overview_df) if req.overview_df else pd.DataFrame()
    outlook_df = pd.DataFrame(req.outlook_df) if req.outlook_df else pd.DataFrame()
    if req.extra_table_df:
        config["extra_table_df"] = pd.DataFrame(req.extra_table_df)
        
    if config.get("custom_tables"):
        for ct in config["custom_tables"]:
            if "df" in ct and isinstance(ct["df"], list):
                ct["df"] = pd.DataFrame(ct["df"])
                
    try:
        pdf_buffer = build_sprint_review_pdf(overview_df, outlook_df, config)
        pdf_bytes = pdf_buffer.getvalue()
        pdf_base64 = base64.b64encode(pdf_bytes).decode('utf-8')
        return {"success": True, "pdf_base64": pdf_base64}
    except Exception as e:
        raise _internal_error(e) from e

class PublishSprintReviewRequest(BaseModel):
    pdf_base64: str
    filename: str
    space_key: str
    page_title: str

@app.post("/api/v1/sprint-review/publish", tags=["Sprint Review"])
async def publish_sprint_review(req: PublishSprintReviewRequest, api_key: str = Depends(get_api_key)):
    conf_server = os.getenv("CONFLUENCE_SERVER", "")
    conf_token = os.getenv("CONFLUENCE_API_TOKEN", "")
    jira_auth_method = os.getenv("JIRA_AUTH_METHOD", "Personal Access Token (Bearer PAT)")
    jira_email = os.getenv("JIRA_EMAIL", "")
    
    if not conf_server or not conf_token:
        raise HTTPException(status_code=400, detail="Confluence credentials missing.")
        
    try:
        pdf_bytes = base64.b64decode(req.pdf_base64)
        page_url = upload_pdf_to_confluence(
            server_url=conf_server,
            auth_type=jira_auth_method,
            token=conf_token,
            email=jira_email,
            space_key=req.space_key,
            page_title=req.page_title,
            pdf_bytes=pdf_bytes,
            filename=req.filename
        )
        return {"success": True, "page_url": page_url}
    except Exception as e:
        raise _internal_error(e) from e

from utils.quarterly_planner_helpers import (
    build_delivery_roadmap_slide_pdf,
    build_quarterly_progress_slide_pdf,
    build_quarterly_progress_pptx,
    build_quarterly_plan_pdf
)
import pandas as pd

class GenerateRoadmapPdfRequest(BaseModel):
    roadmap_df: list
    title: str
    primary_color: Optional[str] = "#3B82F6"

@app.post("/api/v1/quarterly-planner/generate-roadmap-pdf", tags=["Quarterly Planner"])
async def generate_roadmap_pdf(req: GenerateRoadmapPdfRequest, api_key: str = Depends(get_api_key)):
    try:
        df = pd.DataFrame(req.roadmap_df) if req.roadmap_df else pd.DataFrame()
        pdf_bytes = build_delivery_roadmap_slide_pdf(df, req.title, req.primary_color)
        pdf_base64 = base64.b64encode(pdf_bytes).decode('utf-8')
        return {"success": True, "pdf_base64": pdf_base64}
    except Exception as e:
        raise _internal_error(e) from e

class GenerateProgressPdfRequest(BaseModel):
    progress_df: list
    title: str
    primary_color: Optional[str] = "#3B82F6"

@app.post("/api/v1/quarterly-planner/generate-progress-pdf", tags=["Quarterly Planner"])
async def generate_progress_pdf(req: GenerateProgressPdfRequest, api_key: str = Depends(get_api_key)):
    try:
        df = pd.DataFrame(req.progress_df) if req.progress_df else pd.DataFrame()
        pdf_bytes = build_quarterly_progress_slide_pdf(df, req.title, req.primary_color)
        pdf_base64 = base64.b64encode(pdf_bytes).decode('utf-8')
        return {"success": True, "pdf_base64": pdf_base64}
    except Exception as e:
        raise _internal_error(e) from e

class GenerateProgressPptxRequest(BaseModel):
    progress_df: list
    roadmap_df: list
    progress_title: str
    progress_subtitle: str
    roadmap_title: str
    roadmap_subtitle: str
    primary_color: Optional[str] = "#3B82F6"

@app.post("/api/v1/quarterly-planner/generate-progress-pptx", tags=["Quarterly Planner"])
async def generate_progress_pptx(req: GenerateProgressPptxRequest, api_key: str = Depends(get_api_key)):
    try:
        prog_df = pd.DataFrame(req.progress_df) if req.progress_df else pd.DataFrame()
        road_df = pd.DataFrame(req.roadmap_df) if req.roadmap_df else pd.DataFrame()
        pptx_bytes = build_quarterly_progress_pptx(prog_df, road_df, req.progress_title, req.progress_subtitle, req.roadmap_title, req.roadmap_subtitle, req.primary_color)
        pptx_base64 = base64.b64encode(pptx_bytes).decode('utf-8')
        return {"success": True, "pptx_base64": pptx_base64}
    except Exception as e:
        raise _internal_error(e) from e

class GeneratePlanPdfRequest(BaseModel):
    plan_df: list
    primary_color: Optional[str] = "#3B82F6"

@app.post("/api/v1/quarterly-planner/generate-plan-pdf", tags=["Quarterly Planner"])
async def generate_plan_pdf(req: GeneratePlanPdfRequest, api_key: str = Depends(get_api_key)):
    try:
        df = pd.DataFrame(req.plan_df) if req.plan_df else pd.DataFrame()
        pdf_bytes = build_quarterly_plan_pdf(df, req.primary_color)
        pdf_base64 = base64.b64encode(pdf_bytes).decode('utf-8')
        return {"success": True, "pdf_base64": pdf_base64}
    except Exception as e:
        raise _internal_error(e) from e

from utils.jira_helpers import search_jira_issues
from utils.confluence_helpers import search_confluence_pages

class SearchJiraRequest(BaseModel):
    jql: Optional[str] = None
    query: Optional[str] = None

@app.post("/api/v1/search/jira", tags=["Search"])
async def search_jira_api(req: SearchJiraRequest, api_key: str = Depends(get_api_key)):
    jira_server = os.getenv("JIRA_SERVER", "")
    jira_token = os.getenv("JIRA_API_TOKEN", "")
    jira_auth_method = os.getenv("JIRA_AUTH_METHOD", "Personal Access Token (Bearer PAT)")
    jira_email = os.getenv("JIRA_EMAIL", "")
    project_key = os.getenv("AI_ISSUE_PROJECT_KEY") or os.getenv("PROJECT_NAME")

    if not jira_server or not jira_token:
        raise HTTPException(status_code=400, detail="Jira Server or Token not configured.")

    jql = req.jql or req.query
    if not jql:
        raise HTTPException(status_code=400, detail="Missing jql or query parameter")
        
    jql = build_search_query(req.jql or req.query, project_key, "project")

    res = search_jira_issues(jira_server, jira_token, jira_auth_method, jira_email, jql)
    if "error" in res and "400" in res["error"]:
        # JQL error (e.g. invalid status), try a pure text fallback
        fallback = build_search_query(req.jql or req.query, project_key, "project")
        res_fallback = search_jira_issues(jira_server, jira_token, jira_auth_method, jira_email, fallback)
        if "error" not in res_fallback:
            res = res_fallback

    if "error" in res:
        raise _internal_error(res["error"])
    return res

class SearchConfluenceRequest(BaseModel):
    cql: Optional[str] = None
    query: Optional[str] = None

@app.post("/api/v1/search/confluence", tags=["Search"])
async def search_confluence_api(req: SearchConfluenceRequest, api_key: str = Depends(get_api_key)):
    conf_server = os.getenv("CONFLUENCE_SERVER", "")
    conf_token = os.getenv("CONFLUENCE_API_TOKEN", "")
    auth_method = os.getenv("JIRA_AUTH_METHOD", "Personal Access Token (Bearer PAT)")
    email = os.getenv("JIRA_EMAIL", "")
    space_key = os.getenv("CONFLUENCE_SPACE", "")

    if not conf_server or not conf_token:
        raise HTTPException(status_code=400, detail="Confluence Server or Token not configured.")

    cql = req.cql or req.query
    if not cql:
        raise HTTPException(status_code=400, detail="Missing cql or query parameter")
        
    cql = build_search_query(req.cql or req.query, space_key, "space")

    res = search_confluence_pages(conf_server, conf_token, auth_method, email, cql)
    if "error" in res:
        raise _internal_error(res["error"])
    return res


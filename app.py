import truststore
truststore.inject_into_ssl()  # verify TLS with the OS trust store (corporate proxies such as Zscaler)
import streamlit as st
import os
import requests
from dotenv import load_dotenv
from utils.tunnel import start_tunnel, get_tunnel_url, get_tunnel_error, set_tunnel_url
from utils.ai_helpers import get_ollama_models
from utils.sonar_helpers import check_sonar_connection
from utils.sbom_helpers import check_sbom_connection
from utils.vanguard_helpers import check_vanguard_connection
from utils.access import is_remote_request, check_access_password
from utils.connection_checks import check_jira_connection, check_confluence_connection

# Load environment variables
load_dotenv(override=True)
default_primary_color = os.getenv("PRIMARY_COLOR", "#3B82F6")
if 'primary_color' not in st.session_state or st.session_state.get('prev_env_color') != default_primary_color:
    st.session_state.primary_color = default_primary_color
    st.session_state.prev_env_color = default_primary_color


def _request_headers():
    """Headers of the current browser request ({} when unavailable)."""
    try:
        if hasattr(st, "context") and hasattr(st.context, "headers"):
            return dict(st.context.headers or {})
        from streamlit.web.server.websocket_headers import _get_websocket_headers
        return dict(_get_websocket_headers() or {})
    except Exception:
        return {}


# Requests arriving through the Cloudflare tunnel (or any proxy) are "remote":
# they must authenticate and can never read or change the host credentials.
LOCAL_API_URL = os.getenv("LOCAL_API_URL", "http://localhost:8000").rstrip("/")
request_headers = _request_headers()
IS_REMOTE = is_remote_request(request_headers)


def _secret_input(label, state_key, widget_key):
    """Password input that never sends the stored secret to the browser.

    Leaving the field empty keeps the stored value; typing a new value replaces it.
    Returns the effective secret.
    """
    stored = st.session_state.get(state_key, "") or ""
    entered = st.text_input(
        label,
        value="",
        type="password",
        key=widget_key,
        placeholder="•••••••• stored (leave blank to keep)" if stored else "",
    )
    return entered if entered else stored


def save_credentials_to_env():
    if IS_REMOTE:
        raise PermissionError("Credentials can only be changed from the host machine.")
    env_path = ".env"
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            
    kv = {}
    for line in lines:
        line_strip = line.strip()
        if line_strip and not line_strip.startswith("#") and "=" in line_strip:
            parts = line_strip.split("=", 1)
            kv[parts[0].strip()] = parts[1].strip()
            
    kv["JIRA_SERVER"] = st.session_state.jira_server
    kv["JIRA_API_TOKEN"] = st.session_state.jira_token
    kv["JIRA_AUTH_METHOD"] = st.session_state.jira_auth_method
    kv["JIRA_EMAIL"] = st.session_state.jira_email
    kv["CONFLUENCE_SERVER"] = st.session_state.conf_server
    kv["CONFLUENCE_API_TOKEN"] = st.session_state.conf_token
    kv["OLLAMA_URL"] = st.session_state.ollama_url
    kv["SONAR_SERVER"] = st.session_state.sonar_server
    kv["SONAR_TOKEN"] = st.session_state.sonar_token
    kv["SONAR_COMPONENT"] = st.session_state.sonar_component
    kv["SBOM_SERVER"] = st.session_state.sbom_server
    kv["SBOM_API_KEY"] = st.session_state.sbom_api_key
    kv["SBOM_API_SECRET"] = st.session_state.sbom_api_secret
    kv["SBOM_NAMESPACE_ID"] = st.session_state.sbom_namespace_id
    kv["SBOM_APP_ID"] = st.session_state.sbom_app_id
    kv["VANGUARD_PROJECT_ID"] = st.session_state.vanguard_project_id
    kv["VANGUARD_CLIENT_ID"] = st.session_state.vanguard_client_id
    kv["VANGUARD_CLIENT_SECRET"] = st.session_state.vanguard_client_secret

    new_lines = []
    keys_written = set()
    for line in lines:
        line_strip = line.strip()
        if line_strip and not line_strip.startswith("#") and "=" in line_strip:
            parts = line_strip.split("=", 1)
            k_clean = parts[0].strip()
            if k_clean in kv:
                new_lines.append(f"{k_clean}={kv[k_clean]}\n")
                keys_written.add(k_clean)
                continue
        new_lines.append(line)
        
    for k, v in kv.items():
        if k not in keys_written:
            new_lines.append(f"{k}={v}\n")
            
    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

# Initialize central credentials in session state if not set
if "jira_server" not in st.session_state:
    st.session_state.jira_server = os.getenv("JIRA_SERVER", "")
if "jira_token" not in st.session_state:
    st.session_state.jira_token = os.getenv("JIRA_API_TOKEN", "")
if "jira_email" not in st.session_state:
    st.session_state.jira_email = os.getenv("JIRA_EMAIL", "")
if "jira_auth_method" not in st.session_state:
    st.session_state.jira_auth_method = os.getenv("JIRA_AUTH_METHOD", "Personal Access Token (Bearer PAT)")
if "conf_server" not in st.session_state:
    st.session_state.conf_server = os.getenv("CONFLUENCE_SERVER", "")
if "conf_token" not in st.session_state:
    st.session_state.conf_token = os.getenv("CONFLUENCE_API_TOKEN", "")
if "ollama_url" not in st.session_state:
    st.session_state.ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
if "ollama_models" not in st.session_state:
    st.session_state.ollama_models = []

if "sonar_server" not in st.session_state:
    st.session_state.sonar_server = os.getenv("SONAR_SERVER", "https://sonar.your-company.com")
if "sonar_token" not in st.session_state:
    st.session_state.sonar_token = os.getenv("SONAR_TOKEN", "")
if "sonar_component" not in st.session_state:
    st.session_state.sonar_component = os.getenv("SONAR_COMPONENT", "")

if "sbom_server" not in st.session_state:
    st.session_state.sbom_server = os.getenv("SBOM_SERVER", "https://sbom.your-company.com")
if "sbom_api_key" not in st.session_state:
    st.session_state.sbom_api_key = os.getenv("SBOM_API_KEY", "")
if "sbom_api_secret" not in st.session_state:
    st.session_state.sbom_api_secret = os.getenv("SBOM_API_SECRET", "")
if "sbom_namespace_id" not in st.session_state:
    st.session_state.sbom_namespace_id = os.getenv("SBOM_NAMESPACE_ID", "")
if "sbom_app_id" not in st.session_state:
    st.session_state.sbom_app_id = os.getenv("SBOM_APP_ID", "")

if "vanguard_project_id" not in st.session_state:
    st.session_state.vanguard_project_id = os.getenv("VANGUARD_PROJECT_ID", "")
if "vanguard_client_id" not in st.session_state:
    st.session_state.vanguard_client_id = os.getenv("VANGUARD_CLIENT_ID", "")
if "vanguard_client_secret" not in st.session_state:
    st.session_state.vanguard_client_secret = os.getenv("VANGUARD_CLIENT_SECRET", "")

QUALITY_INTEGRATIONS = (
    ("sonar", "SonarQube", check_sonar_connection),
    ("sbom", "SBOM", check_sbom_connection),
    ("vanguard", "Vanguard", check_vanguard_connection),
)

def check_quality_integrations(args_by_key, skip_unconfigured=False):
    """Tests SonarQube, SBOM and Vanguard, stores each status in session state and returns (label, status, msg)."""
    results = []
    for key, label, check in QUALITY_INTEGRATIONS:
        args = args_by_key[key]
        if not all(a and str(a).strip() for a in args):
            status, msg = ("Not checked", "") if skip_unconfigured else ("Failed", "Configuration incomplete")
        else:
            ok, msg = check(*args)
            status = "Success" if ok else "Failed"
        st.session_state[f"{key}_connection_status"] = status
        st.session_state[f"{key}_connection_msg"] = msg
        results.append((label, status, msg))
    return results

def quality_integration_args():
    return {
        "sonar": (st.session_state.sonar_server, st.session_state.sonar_token, st.session_state.sonar_component),
        "sbom": (st.session_state.sbom_server, st.session_state.sbom_api_key, st.session_state.sbom_api_secret,
                 st.session_state.sbom_namespace_id, st.session_state.sbom_app_id),
        "vanguard": (st.session_state.vanguard_project_id, st.session_state.vanguard_client_id,
                     st.session_state.vanguard_client_secret),
    }

# Perform automatic connection checks on session initialization
if "jira_connection_status" not in st.session_state:
    st.session_state.jira_connection_status = "Not checked"
    st.session_state.jira_connection_msg = ""
    st.session_state.conf_connection_status = "Not checked"
    st.session_state.conf_connection_msg = ""
    st.session_state.ollama_connection_status = "Not checked"
    st.session_state.ollama_connection_msg = ""
    st.session_state.api_connection_status = "Not checked"
    st.session_state.api_connection_msg = ""

    # Test Jira and Confluence connections (shared helpers in utils/connection_checks.py)
    auth_method = st.session_state.jira_auth_method
    email = st.session_state.jira_email
    if st.session_state.jira_server and st.session_state.jira_token:
        ok, msg = check_jira_connection(st.session_state.jira_server, st.session_state.jira_token,
                                        auth_method, email, timeout=5)
        st.session_state.jira_connection_status = "Success" if ok else "Failed"
        st.session_state.jira_connection_msg = msg
    if st.session_state.conf_server and st.session_state.conf_token:
        ok, msg = check_confluence_connection(st.session_state.conf_server, st.session_state.conf_token,
                                              auth_method, email, timeout=5)
        st.session_state.conf_connection_status = "Success" if ok else "Failed"
        st.session_state.conf_connection_msg = msg

    # Test Ollama Connection
    ourl = st.session_state.ollama_url
    if ourl:
        try:
            models = get_ollama_models(ourl)
            if models:
                st.session_state.ollama_connection_status = "Success"
                st.session_state.ollama_connection_msg = f"{len(models)} models found"
                st.session_state.ollama_models = models
            else:
                st.session_state.ollama_connection_status = "Failed"
                st.session_state.ollama_connection_msg = "No models or offline"
        except Exception as e:
            st.session_state.ollama_connection_status = "Failed"
            st.session_state.ollama_connection_msg = f"Connection Failed ({e})"

    # Test API Connection
    try:
        api_resp = requests.get(f"{LOCAL_API_URL}/openapi.json", timeout=2)
        if api_resp.status_code == 200:
            st.session_state.api_connection_status = "Success"
            st.session_state.api_connection_msg = "Connected"
        else:
            st.session_state.api_connection_status = "Failed"
            st.session_state.api_connection_msg = f"HTTP {api_resp.status_code}"
    except Exception:
        st.session_state.api_connection_status = "Failed"
        st.session_state.api_connection_msg = "Offline"

    # Test SonarQube, SBOM and Vanguard (only those fully configured)
    check_quality_integrations(quality_integration_args(), skip_unconfigured=True)

# 1. Define the page objects first so they are globally accessible
planner_page = st.Page("tools/1_Quarterly_Planner.py", title="Quarterly Planner", icon="📅")
sprint_review_page = st.Page("tools/2_Sprint_Review.py", title="Sprint Review", icon="📋")
release_notes_page = st.Page("tools/3_Release_Notes.py", title="Release Notes", icon="📣")
sprint_kpis_page = st.Page("tools/4_Sprint_KPIs.py", title="Sprint KPIs", icon="📊")
ai_creator_page = st.Page("tools/5_AI_Issue_Creator.py", title="AI Issue Creator", icon="🤖")
ai_agent_page = st.Page("tools/6_AI_Agent.py", title="AI Agent", icon="💬")
from utils.navigation import RELEASE_PLAN_PAGES
_release_plan_icon_map = {
    0: "🔎",
    7: "📋",
    8: "📨",
}
_release_plan_titles = {
    0: "Overview",
    7: "Post Actions",
    8: "Notifications",
}
_rp_pages = {
    n: (
        st.Page(
            path,
            title=_release_plan_titles.get(n, title.replace(f"{n} · ", "")),
            url_path=url,
            icon=_release_plan_icon_map.get(n, "📄"),
        ) if url else st.Page(
            path,
            title=_release_plan_titles.get(n, title.replace(f"{n} · ", "")),
            icon=_release_plan_icon_map.get(n, "📄"),
        )
    )
    for n, path, title, url in RELEASE_PLAN_PAGES
    if n in (0, 7, 8)
}
rr_overview_page = _rp_pages[0]
release_plan_nav_pages = [_rp_pages[n] for n in (0, 7, 8)]

# 2. Define the Home Page rendering function
def show_home():

    # Custom CSS for Premium Dark Mode Theme matching all tools
    st.markdown("""
        <style>
            /* General dark theme override */
            .main, .block-container, [data-testid="stAppViewContainer"], [data-testid="stHeader"] {
                background-color: #27272E !important;
                color: #F8FAFC !important;
            }
            
            /* Typography high-contrast styles */
            h1, h2, h3, h4, h5, h6 {
                color: #FFFFFF !important;
                font-weight: 700 !important;
            }
            .stMarkdown p, .stMarkdown li, .stMarkdown span {
                color: #F8FAFC !important;
            }
            
            /* Sidebar dark theme improvements */
            section[data-testid="stSidebar"] {
                background-color: #18181D !important;
                border-right: 1px solid #3E3E4A !important;
            }
            section[data-testid="stSidebar"] [data-testid="stSidebarNavItems"] {
                gap: 0.2rem !important;
            }
            section[data-testid="stSidebar"] [data-testid="stSidebarNavItems"] > div,
            section[data-testid="stSidebar"] [data-testid="stSidebarNavItems"] > a,
            section[data-testid="stSidebar"] [data-testid="stSidebarNavItems"] > button,
            section[data-testid="stSidebar"] [data-testid="stSidebarNavItems"] [data-testid="stSidebarNavMenu"] {
                margin: 0 !important;
                padding-left: 0.1rem !important;
            }
            section[data-testid="stSidebar"] [data-testid="stSidebarNavItems"] [data-testid="stSidebarNavMenu"] {
                margin-left: 0.35rem !important;
            }
            section[data-testid="stSidebar"] [data-testid="stSidebarNavItems"] .stSidebarNavSectionHeader {
                padding-left: 0.15rem !important;
                margin-bottom: 0.1rem !important;
            }

            /* Sidebar Toggle Button */
            button[data-testid="collapsedControl"], button[kind="header"] {
                color: #60A5FA !important;
            }
            button[data-testid="collapsedControl"] svg, button[kind="header"] svg {
                fill: #60A5FA !important;
            }
            
            /* Premium Card Layouts applied directly to the Column container */
            div[data-testid="column"]:has(.hub-card) {
                background-color: #18181D !important;
                border: 1px solid #3E3E4A !important;
                border-radius: 12px !important;
                padding: 2rem !important;
                box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.5) !important;
                margin-bottom: 1.5rem;
                transition: all 0.3s ease-in-out;
            }
            div[data-testid="column"]:has(.hub-card):hover {
                border-color: #60A5FA !important;
                transform: translateY(-2px);
            }
            .hub-card {
                background-color: transparent !important;
                border: none !important;
                border-radius: 0 !important;
                padding: 0 !important;
                box-shadow: none !important;
                margin: 0 !important;
            }
            .hub-badge {
                background-color: #60A5FA22; 
                color: #60A5FA; 
                border: 1px solid #60A5FA; 
                padding: 3px 10px; 
                border-radius: 12px; 
                font-size: 11px; 
                font-weight: 600; 
                display: inline-block;
                margin-bottom: 10px;
            }
            
            /* Style st.page_link to be a premium pill button inside the card container */
            div[data-testid="column"]:has(.hub-card) [data-testid="stPageLink"],
            div[data-testid="column"]:has(.hub-card) [data-testid="stPageLink"] a,
            div[data-testid="column"]:has(.hub-card) .stPageLink,
            div[data-testid="column"]:has(.hub-card) .stPageLink a,
            div[data-testid="column"]:has(.hub-card) a[class*="stPageLink"],
            div[data-testid="column"]:has(.hub-card) div[class*="stPageLink"] a,
            [data-testid="stPageLink"] a,
            .stPageLink a,
            a[class*="stPageLink"] {
                background: linear-gradient(135deg, #3B82F6 0%, #1D4ED8 100%) !important;
                color: #FFFFFF !important;
                padding: 0.5rem 1.5rem !important;
                border-radius: 24px !important;
                font-weight: 600 !important;
                font-size: 14px !important;
                text-decoration: none !important;
                display: inline-flex !important;
                align-items: center !important;
                justify-content: center !important;
                border: none !important;
                box-shadow: none !important;
                transition: all 0.2s ease-in-out !important;
                margin-top: 15px !important;
                width: fit-content !important;
                height: 2.25rem !important;
                box-sizing: border-box !important;
            }
            
            div[data-testid="column"]:has(.hub-card) [data-testid="stPageLink"]:hover,
            div[data-testid="column"]:has(.hub-card) [data-testid="stPageLink"] a:hover,
            div[data-testid="column"]:has(.hub-card) .stPageLink:hover,
            div[data-testid="column"]:has(.hub-card) .stPageLink a:hover,
            div[data-testid="column"]:has(.hub-card) a[class*="stPageLink"]:hover,
            div[data-testid="column"]:has(.hub-card) div[class*="stPageLink"] a:hover,
            [data-testid="stPageLink"] a:hover,
            .stPageLink a:hover,
            a[class*="stPageLink"]:hover {
                background: linear-gradient(135deg, #2563EB 0%, #1D4ED8 100%) !important;
                color: #FFFFFF !important;
                border: none !important;
                transform: translateY(-1px) !important;
                box-shadow: none !important;
            }
            
            /* Force the inner page link text to be white and bold */
            div[data-testid="column"]:has(.hub-card) [data-testid="stPageLink"] p,
            div[data-testid="column"]:has(.hub-card) .stPageLink p,
            div[data-testid="column"]:has(.hub-card) a[class*="stPageLink"] p,
            div[data-testid="column"]:has(.hub-card) div[class*="stPageLink"] a p,
            [data-testid="stPageLink"] a p,
            .stPageLink a p,
            a[class*="stPageLink"] p {
                color: #FFFFFF !important;
                margin: 0 !important;
                padding: 0 !important;
                font-weight: 600 !important;
                font-size: 14px !important;
                line-height: 1 !important;
            }
            
            /* Hide page link icon completely */
            div[data-testid="column"]:has(.hub-card) [data-testid="stPageLink"] img,
            div[data-testid="column"]:has(.hub-card) [data-testid="stPageLink"] svg,
            div[data-testid="column"]:has(.hub-card) [data-testid="stPageLink"] [data-testid="stIcon"],
            div[data-testid="column"]:has(.hub-card) .stPageLink img,
            div[data-testid="column"]:has(.hub-card) .stPageLink svg,
            div[data-testid="column"]:has(.hub-card) .stPageLink [data-testid="stIcon"],
            [data-testid="stPageLink"] img,
            [data-testid="stPageLink"] svg,
            [data-testid="stPageLink"] [data-testid="stIcon"],
            .stPageLink img,
            .stPageLink svg,
            .stPageLink [data-testid="stIcon"] {
                display: none !important;
            }
            
            /* Premium button styles without box-shadow */
            .stButton>button, .stDownloadButton>button { 
                border-radius: 24px; 
                border: none; 
                background: linear-gradient(135deg, #3B82F6 0%, #1D4ED8 100%) !important; 
                color: #FFFFFF !important; 
                padding: 0.5rem 1.5rem; 
                font-weight: 600;
                box-shadow: none !important;
                transition: all 0.2s ease-in-out;
            }
            .stButton>button:hover, .stDownloadButton>button:hover { 
                background: linear-gradient(135deg, #2563EB 0%, #1D4ED8 100%) !important;
                color: #FFFFFF !important;
                box-shadow: none !important;
                transform: translateY(-1px);
            }
        </style>
    """, unsafe_allow_html=True)

    # Main Title and Welcome
    st.title("💼 Product Owner Suite Hub")
    st.markdown("Welcome to the **Product Owner & Project Management Tools Suite**. Use the sidebar to seamlessly navigate between the different tools available in this workspace.")

    st.divider()

    tab_tools, tab_integrations = st.tabs(["🛠️ Available Tools", "🔌 Centralized Integrations"])

    with tab_tools:
        st.subheader("🛠️ Available Tools")
        r1c1, r1c2, r1c3 = st.columns(3)
        
        with r1c1:
            st.markdown("""
                <div class="hub-card">
                    <span class="hub-badge">ACTIVE 🚀</span>
                    <h3>🎯 Quarterly Planner</h3>
                    <p>Track committed quarterly Epics from Jira, add delivery context and planned milestones, then export progress and delivery-roadmap slides.</p>
                </div>
            """, unsafe_allow_html=True)
            st.page_link(planner_page, label="Open Planner", icon="🎯")

        with r1c2:
            st.markdown("""
                <div class="hub-card">
                    <span class="hub-badge">ACTIVE 🚀</span>
                    <h3>📋 Sprint Review</h3>
                    <p>Extract Jira sprint data, edit items in a workbook, apply custom branding, and export PDF Sprint Review reports.</p>
                </div>
            """, unsafe_allow_html=True)
            st.page_link(sprint_review_page, label="Open Sprint Review", icon="📋")

        with r1c3:
            st.markdown("""
                <div class="hub-card">
                    <span class="hub-badge">ACTIVE 🚀</span>
                    <h3>📣 Release Notes</h3>
                    <p>Create a Release Note from a Jira version: load release data, review the editable purpose and ticket tables, then export or publish the PDF.</p>
                </div>
            """, unsafe_allow_html=True)
            st.page_link(release_notes_page, label="Open Release Notes", icon="📣")

        r2c1, r2c2, r2c3 = st.columns(3)

        with r2c1:
            st.markdown("""
                <div class="hub-card">
                    <span class="hub-badge">ACTIVE 🚀</span>
                    <h3>📊 Sprint KPIs</h3>
                    <p>Calculate specific KPIs from Jira such as cycle times, committed vs achieved points, and releases.</p>
                </div>
            """, unsafe_allow_html=True)
            st.page_link(sprint_kpis_page, label="Open Sprint KPIs", icon="📊")

        with r2c2:
            st.markdown("""
                <div class="hub-card">
                    <span class="hub-badge">NEW ✨</span>
                    <h3>🤖 AI Issue Creator</h3>
                    <p>Draft, format and upload Jira issues effortlessly using a local Ollama AI model.</p>
                </div>
            """, unsafe_allow_html=True)
            st.page_link(ai_creator_page, label="Open AI Creator", icon="🤖")

        with r2c3:
            pass # AI Agent card removed temporarily while under development

        r3c1, r3c2, r3c3 = st.columns(3)

        with r3c1:
            st.markdown("""
                <div class="hub-card">
                    <span class="hub-badge">NEW ✨</span>
                    <h3>✅ Release Readiness</h3>
                    <p>Check a release before go-live: scope, approvals, documentation table, critical changes, PRE testing and unassigned issues, with copy-ready notifications. Read-only.</p>
                </div>
            """, unsafe_allow_html=True)
            st.page_link(rr_overview_page, label="Open Release Overview", icon="✅")

    with tab_integrations:
        st.subheader("🔌 Centralized Integrations")
        st.write("Configure connection configurations for Jira, Confluence, Ollama, SonarQube, SBOM and Vanguard below.")

        # Display current status in nice columns
        stat_col1, stat_col2, stat_col3, stat_col4 = st.columns(4)
        with stat_col1:
            j_status = st.session_state.get("jira_connection_status", "Not checked")
            j_msg = st.session_state.get("jira_connection_msg", "")
            if j_status == "Success":
                st.success(f"🟢 **Jira**: {j_msg}")
            elif j_status == "Failed":
                st.error(f"🔴 **Jira**: {j_msg}")
            else:
                st.warning(f"🟡 **Jira**: {j_status}")
        with stat_col2:
            c_status = st.session_state.get("conf_connection_status", "Not checked")
            c_msg = st.session_state.get("conf_connection_msg", "")
            if c_status == "Success":
                st.success(f"🟢 **Confluence**: {c_msg}")
            elif c_status == "Failed":
                st.error(f"🔴 **Confluence**: {c_msg}")
            else:
                st.warning(f"🟡 **Confluence**: {c_status}")
        with stat_col3:
            o_status = st.session_state.get("ollama_connection_status", "Not checked")
            o_msg = st.session_state.get("ollama_connection_msg", "")
            if o_status == "Success":
                st.success(f"🟢 **Ollama**: {o_msg}")
            elif o_status == "Failed":
                st.error(f"🔴 **Ollama**: {o_msg}")
            else:
                st.warning(f"🟡 **Ollama**: {o_status}")
        with stat_col4:
            a_status = st.session_state.get("api_connection_status", "Not checked")
            a_msg = st.session_state.get("api_connection_msg", "")
            if a_status == "Success":
                st.success(f"🟢 **Local API**: {a_msg}")
            elif a_status == "Failed":
                st.error(f"🔴 **Local API**: {a_msg}")
            else:
                st.warning(f"🟡 **Local API**: {a_status}")

        for col, (key, label, _) in zip(st.columns(4), QUALITY_INTEGRATIONS):
            with col:
                q_status = st.session_state.get(f"{key}_connection_status", "Not checked")
                q_msg = st.session_state.get(f"{key}_connection_msg", "")
                if q_status == "Success":
                    st.success(f"🟢 **{label}**: {q_msg}")
                elif q_status == "Failed":
                    st.error(f"🔴 **{label}**: {q_msg}")
                else:
                    st.warning(f"🟡 **{label}**: {q_status}")

        st.markdown("---")

        if IS_REMOTE:
            st.info("🔒 Integration settings can only be viewed or changed from the host machine (http://localhost:8501).")
            return

        col_jserv, col_jtok = st.columns(2)
        with col_jserv:
            srv_in = st.text_input("Jira Server Base URL:", value=st.session_state.jira_server, key="central_jira_server")
        with col_jtok:
            tok_in = _secret_input("Jira Personal Access Token (PAT):", "jira_token", "central_jira_token")

        col_jauth, col_jmail = st.columns(2)
        with col_jauth:
            auth_options = [
                "Personal Access Token (Bearer PAT)",
                "Corporate Login (Username + Password)",
                "Jira Cloud/Server Basic (Email/User + Token)"
            ]
            auth_in = st.selectbox(
                "Jira Auth Method:",
                options=auth_options,
                index=auth_options.index(st.session_state.jira_auth_method) if st.session_state.jira_auth_method in auth_options else 0,
                key="central_jira_auth"
            )
        with col_jmail:
            mail_in = st.text_input("Jira Email/Username (Optional):", value=st.session_state.jira_email, key="central_jira_email")

        st.markdown("---")
        col_cserv, col_ctok = st.columns(2)
        with col_cserv:
            csrv_in = st.text_input("Confluence Server URL:", value=st.session_state.conf_server, key="central_conf_server")
        with col_ctok:
            ctok_in = _secret_input("Confluence Personal Access Token (PAT):", "conf_token", "central_conf_token")

        st.markdown("---")
        st.markdown("##### 🤖 Local AI (Ollama)")
        col_ourl, _ = st.columns(2)
        with col_ourl:
            ourl_in = st.text_input("Ollama API URL:", value=st.session_state.ollama_url, key="central_ollama_url")

        st.markdown("---")
        st.markdown("##### 📡 Quality & Security Integrations (KPIs)")
        
        st.markdown("**SonarQube**")
        sq_col1, sq_col2, sq_col3 = st.columns(3)
        with sq_col1:
            sonar_server_in = st.text_input("SonarQube Server:", value=st.session_state.sonar_server, key="central_sonar_server")
        with sq_col2:
            sonar_token_in = _secret_input("SonarQube Token:", "sonar_token", "central_sonar_token")
        with sq_col3:
            sonar_comp_in = st.text_input("SonarQube Component:", value=st.session_state.sonar_component, key="central_sonar_component")

        st.markdown("**SBOM Inventory**")
        sb_col1, sb_col2, sb_col3 = st.columns(3)
        with sb_col1:
            sbom_server_in = st.text_input("SBOM Server:", value=st.session_state.sbom_server, key="central_sbom_server")
            sbom_namespace_in = st.text_input("SBOM Namespace ID:", value=st.session_state.sbom_namespace_id, key="central_sbom_namespace")
        with sb_col2:
            sbom_api_key_in = _secret_input("SBOM API Key:", "sbom_api_key", "central_sbom_api_key")
            sbom_app_in = st.text_input("SBOM App ID:", value=st.session_state.sbom_app_id, key="central_sbom_app_id")
        with sb_col3:
            sbom_api_secret_in = _secret_input("SBOM API Secret:", "sbom_api_secret", "central_sbom_api_secret")

        st.markdown("**Vanguard**")
        vg_col1, vg_col2, vg_col3 = st.columns(3)
        with vg_col1:
            vg_project_in = st.text_input("Vanguard Project ID:", value=st.session_state.vanguard_project_id, key="central_vg_project")
        with vg_col2:
            vg_client_id_in = st.text_input("Vanguard Client ID:", value=st.session_state.vanguard_client_id, key="central_vg_client_id")
        with vg_col3:
            vg_client_secret_in = _secret_input("Vanguard Client Secret:", "vanguard_client_secret", "central_vg_secret")

        settings_changed = (
            st.session_state.jira_server != srv_in or
            st.session_state.jira_token != tok_in or
            st.session_state.jira_auth_method != auth_in or
            st.session_state.jira_email != mail_in or
            st.session_state.conf_server != csrv_in or
            st.session_state.conf_token != ctok_in or
            st.session_state.ollama_url != ourl_in or
            st.session_state.sonar_server != sonar_server_in or
            st.session_state.sonar_token != sonar_token_in or
            st.session_state.sonar_component != sonar_comp_in or
            st.session_state.sbom_server != sbom_server_in or
            st.session_state.sbom_api_key != sbom_api_key_in or
            st.session_state.sbom_api_secret != sbom_api_secret_in or
            st.session_state.sbom_namespace_id != sbom_namespace_in or
            st.session_state.sbom_app_id != sbom_app_in or
            st.session_state.vanguard_project_id != vg_project_in or
            st.session_state.vanguard_client_id != vg_client_id_in or
            st.session_state.vanguard_client_secret != vg_client_secret_in
        )

        if settings_changed:
            st.session_state.jira_server = srv_in
            st.session_state.jira_token = tok_in
            st.session_state.jira_auth_method = auth_in
            st.session_state.jira_email = mail_in
            st.session_state.conf_server = csrv_in
            st.session_state.conf_token = ctok_in
            st.session_state.ollama_url = ourl_in
            st.session_state.sonar_server = sonar_server_in
            st.session_state.sonar_token = sonar_token_in
            st.session_state.sonar_component = sonar_comp_in
            st.session_state.sbom_server = sbom_server_in
            st.session_state.sbom_api_key = sbom_api_key_in
            st.session_state.sbom_api_secret = sbom_api_secret_in
            st.session_state.sbom_namespace_id = sbom_namespace_in
            st.session_state.sbom_app_id = sbom_app_in
            st.session_state.vanguard_project_id = vg_project_in
            st.session_state.vanguard_client_id = vg_client_id_in
            st.session_state.vanguard_client_secret = vg_client_secret_in
            save_credentials_to_env()
            st.toast("Integrations updated and saved centrally! ✅")

        st.markdown("<br/>", unsafe_allow_html=True)
        if st.button("🔌 Check Integration Status", use_container_width=True):
            st.markdown("### 🔍 Connection Test Results")
            
            # 1. Test Jira Connection
            if srv_in and tok_in:
                with st.spinner("Testing Jira Connection..."):
                    ok, msg = check_jira_connection(srv_in, tok_in, auth_in, mail_in, timeout=8)
                st.session_state.jira_connection_status = "Success" if ok else "Failed"
                st.session_state.jira_connection_msg = msg
                if ok:
                    st.success(f"✅ **Jira Connection Successful!** {msg}.")
                else:
                    st.error(f"❌ **Jira Connection Failed**: {msg}. Check server URL and API token.")
            else:
                st.session_state.jira_connection_status = "Failed"
                st.session_state.jira_connection_msg = "Server URL or Token empty"
                st.warning("⚠️ **Jira not tested**: Server URL or token is empty.")

            # 2. Test Confluence Connection
            if csrv_in and ctok_in:
                with st.spinner("Testing Confluence Connection..."):
                    ok, msg = check_confluence_connection(csrv_in, ctok_in, auth_in, mail_in, timeout=8)
                st.session_state.conf_connection_status = "Success" if ok else "Failed"
                st.session_state.conf_connection_msg = msg
                if ok:
                    st.success("✅ **Confluence Connection Successful!**")
                else:
                    st.error(f"❌ **Confluence Connection Failed**: {msg}. Check URL and token.")
            else:
                st.session_state.conf_connection_status = "Failed"
                st.session_state.conf_connection_msg = "Server URL or Token empty"
                st.warning("⚠️ **Confluence not tested**: Server URL or token is empty.")

            # 3. Test Ollama Connection
            if ourl_in:
                with st.spinner("Testing Ollama Connection..."):
                    models = get_ollama_models(ourl_in)
                    if models:
                        st.session_state.ollama_connection_status = "Success"
                        st.session_state.ollama_connection_msg = f"{len(models)} models found"
                        st.session_state.ollama_models = models
                        st.success(f"✅ **Ollama Connection Successful!** Found {len(models)} models.")
                    else:
                        st.session_state.ollama_connection_status = "Failed"
                        st.session_state.ollama_connection_msg = "No models or offline"
                        st.session_state.ollama_models = []
                        st.error(f"❌ **Ollama Connection Failed**: Could not fetch models from {ourl_in}.")
            else:
                st.session_state.ollama_connection_status = "Failed"
                st.session_state.ollama_connection_msg = "URL empty"
                st.warning("⚠️ **Ollama not tested**: URL is empty.")

            # 4. Test Local API Connection
            with st.spinner("Testing Local API Connection..."):
                try:
                    api_resp = requests.get(f"{LOCAL_API_URL}/openapi.json", timeout=2)
                    if api_resp.status_code == 200:
                        st.session_state.api_connection_status = "Success"
                        st.session_state.api_connection_msg = "Connected"
                        st.success("�� **Local API Connection Successful!**")
                    else:
                        st.session_state.api_connection_status = "Failed"
                        st.session_state.api_connection_msg = f"HTTP {api_resp.status_code}"
                        st.error(f"❌ **Local API Connection Failed (HTTP {api_resp.status_code})**")
                except Exception:
                    st.session_state.api_connection_status = "Failed"
                    st.session_state.api_connection_msg = "Offline"
                    st.error(f"❌ **Local API Connection Failed**: Could not connect to {LOCAL_API_URL}.")

            # 5-7. Test SonarQube, SBOM and Vanguard Connections
            with st.spinner("Testing SonarQube, SBOM and Vanguard Connections..."):
                quality_results = check_quality_integrations(quality_integration_args())
            for label, q_status, q_msg in quality_results:
                if q_status == "Success":
                    st.success(f"✅ **{label} Connection Successful!** {q_msg}.")
                elif q_msg == "Configuration incomplete":
                    st.warning(f"⚠️ **{label} not tested**: configuration is incomplete.")
                else:
                    st.error(f"❌ **{label} Connection Failed**: {q_msg}")
# 3. Define the page listing for navigation
home_page = st.Page(show_home, title="Home Hub", icon="🏠", default=True)

# 4. Setup and run navigation
pg = st.navigation({
    "": [home_page, planner_page, sprint_review_page, release_notes_page, sprint_kpis_page, ai_creator_page, ai_agent_page],
    "🎯 Release Plan": release_plan_nav_pages,
})
st.set_page_config(page_title="Product Owner Suite Hub", layout="wide")

# Remote access gate: tunnel/proxy visitors must provide REMOTE_ACCESS_PASSWORD.
if IS_REMOTE and not st.session_state.get("remote_authenticated", False):
    expected_remote_password = os.getenv("REMOTE_ACCESS_PASSWORD", "")
    st.title("🔒 Product Owner Suite Hub")
    if not expected_remote_password:
        st.error("Remote access is disabled. The host must set REMOTE_ACCESS_PASSWORD in its .env file to allow it.")
        st.stop()
    with st.form("remote_login"):
        provided_password = st.text_input("Access password", type="password")
        submitted = st.form_submit_button("Enter")
    if submitted:
        if check_access_password(provided_password, expected_remote_password):
            st.session_state.remote_authenticated = True
            st.rerun()
        st.error("Invalid password.")
    st.stop()

# Hide the AI Agent from the sidebar menu globally
st.markdown("""
    <style>
        [data-testid="stSidebarNavItems"] a[href$="AI_Agent"] {
            display: none !important;
        }
    </style>
""", unsafe_allow_html=True)

# Display live collaboration tunnel link in the sidebar
st.sidebar.markdown("### 🤝 Live Collaboration")

# Auto-detect tunnel URL from request headers if accessed via Cloudflare
headers = request_headers
host = headers.get("Host", "")
x_forwarded_host = headers.get("X-Forwarded-Host", "")
tunnel_host = None
if "trycloudflare.com" in host:
    tunnel_host = host
elif "trycloudflare.com" in x_forwarded_host:
    tunnel_host = x_forwarded_host

if tunnel_host:
    detected_url = f"https://{tunnel_host}"
    set_tunnel_url(detected_url)

tunnel_url = get_tunnel_url()

if tunnel_url:
    st.session_state.tunnel_enabled = True

if "tunnel_enabled" not in st.session_state:
    st.session_state.tunnel_enabled = False

if IS_REMOTE:
    # Remote collaborators can use the tools but never control the host tunnel.
    st.sidebar.caption("Connected remotely through the collaboration tunnel.")
elif st.session_state.tunnel_enabled:
    if not tunnel_url:
        tunnel_url = start_tunnel(8501)
        
    if tunnel_url:
        st.sidebar.success("Tunnel Active 🟢")
        st.sidebar.code(tunnel_url, language="text")
        st.sidebar.link_button("🌐 Open Link", tunnel_url, use_container_width=True)
        st.sidebar.caption("Share this URL with team members to collaborate in real-time.")
        if not os.getenv("REMOTE_ACCESS_PASSWORD"):
            st.sidebar.warning("Set REMOTE_ACCESS_PASSWORD in .env: remote visitors are blocked until a password is configured.")
    else:
        tunnel_err = get_tunnel_error()
        if tunnel_err:
            st.sidebar.error("❌ Tunnel Error")
            st.sidebar.warning(tunnel_err)
            st.sidebar.caption("You can still use the app locally at http://localhost:8501")
        else:
            st.sidebar.info("🌀 Starting tunnel...")
            from streamlit_autorefresh import st_autorefresh
            st_autorefresh(interval=1000, limit=10, key="tunnel_startup_refresher")
            
    if st.sidebar.button("🛑 Stop Tunnel", use_container_width=True):
        from utils.tunnel import stop_tunnel
        stop_tunnel()
        st.session_state.tunnel_enabled = False
        st.rerun()
else:
    if tunnel_url:
        from utils.tunnel import stop_tunnel
        stop_tunnel()
    st.sidebar.info("Cloudflare tunnel is disabled.")
    if st.sidebar.button("🚀 Start Collaboration Tunnel", use_container_width=True):
        st.session_state.tunnel_enabled = True
        st.rerun()

pg.run()

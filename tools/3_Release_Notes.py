import streamlit as st
import pandas as pd
from utils.persistence import (
    load_df_from_csv, save_df_to_csv, 
    load_custom_tables_from_json, save_custom_tables_to_json, 
    get_last_mtime, enable_auto_save, setup_autorefresh
)
from utils.jira_helpers import fetch_jira_tickets_dataset, map_jira_status
import requests
import os
import io
import re
import base64
from html import unescape
from datetime import datetime
from urllib.parse import quote
from dotenv import load_dotenv
from PIL import Image as PILImage

# ReportLab imports for beautiful PDF generation
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether, Image, Flowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfgen import canvas
from reportlab.graphics.shapes import Drawing, Line, PolyLine

# Extracted PDF helper functions
from utils.pdf_helpers import (
    SmartKeepTogether, hex_to_reportlab_color, convert_markdown_to_pdf_rich_text,
    split_bugs_and_topics, sort_items_by_label_priority, get_team_label, sort_items_by_type_and_epic,
    draw_background_landscape, NumberedCanvas, build_demos_pdf_block, build_next_releases_pdf_block,
    format_status_with_emoji, build_custom_extra_table_pdf_block, get_arrow_drawing,
    extract_numeric_version
)

RN_FILES = [
    "release_notes_overview.csv",
    "release_notes_custom_tables.json",
    "release_notes_next_release.csv"
]

def save_release_notes_shared():
    save_df_to_csv(st.session_state.get("overview_df"), "release_notes_overview.csv")
    save_custom_tables_to_json(st.session_state.get("custom_tables"), "release_notes_custom_tables.json")
    save_df_to_csv(st.session_state.get("next_release_df"), "release_notes_next_release.csv")
    st.session_state.release_notes_last_sync = get_last_mtime(RN_FILES)

# Enable automatic background saves on st.rerun()
enable_auto_save(save_release_notes_shared)


# ---------------------------------------------------------
# Callback synchronization helpers for st.data_editor
# ---------------------------------------------------------
def check_any_selected(df_key, editor_key, display_df_indices):
    if st.session_state.get(df_key) is None:
        return False
    df = st.session_state[df_key]
    if "Select" in df.columns and df["Select"].any():
        return True
    edits = st.session_state.get(editor_key, {})
    for idx_str, col_changes in edits.get("edited_rows", {}).items():
        if col_changes.get("Select") == True:
            return True
    return False

def check_any_selected_custom(table_idx, editor_key, display_df_indices):
    if table_idx >= len(st.session_state.custom_tables):
        return False
    df = st.session_state.custom_tables[table_idx]["df"]
    if "Select" in df.columns and df["Select"].any():
        return True
    edits = st.session_state.get(editor_key, {})
    for idx_str, col_changes in edits.get("edited_rows", {}).items():
        if col_changes.get("Select") == True:
            return True
    return False

def get_selected_rows(df_key, editor_key, display_df_indices):
    if st.session_state.get(df_key) is None:
        return pd.DataFrame()
    df = st.session_state[df_key].copy()
    edits = st.session_state.get(editor_key, {})
    for idx_str, col_changes in edits.get("edited_rows", {}).items():
        if "Select" in col_changes:
            idx = int(idx_str)
            if idx < len(display_df_indices):
                actual_idx = display_df_indices[idx]
                df.at[actual_idx, "Select"] = col_changes["Select"]
    return df[df["Select"] == True]

def get_selected_rows_custom(table_idx, editor_key, display_df_indices):
    if table_idx >= len(st.session_state.custom_tables):
        return pd.DataFrame()
    df = st.session_state.custom_tables[table_idx]["df"].copy()
    edits = st.session_state.get(editor_key, {})
    for idx_str, col_changes in edits.get("edited_rows", {}).items():
        if "Select" in col_changes:
            idx = int(idx_str)
            if idx < len(display_df_indices):
                actual_idx = display_df_indices[idx]
                df.at[actual_idx, "Select"] = col_changes["Select"]
    return df[df["Select"] == True]

def sync_editor_changes(df_key, editor_key, display_df_indices):
    edits = st.session_state.get(editor_key)
    if not edits or st.session_state.get(df_key) is None:
        return
    df = st.session_state[df_key].copy()
    
    has_real_edits = False
    
    # Edited rows
    for idx_str, col_changes in edits.get("edited_rows", {}).items():
        idx = int(idx_str)
        if idx < len(display_df_indices):
            actual_idx = display_df_indices[idx]
            for col, val in col_changes.items():
                if col != "Select":
                    df.at[actual_idx, col] = val
                    has_real_edits = True
                
    # Deleted rows
    deleted_indices = edits.get("deleted_rows", [])
    if deleted_indices:
        indices_to_drop = [display_df_indices[i] for i in deleted_indices if i < len(display_df_indices)]
        df = df.drop(index=indices_to_drop)
        df = df.reset_index(drop=True)
        has_real_edits = True
        
    # Added rows
    added_rows = edits.get("added_rows", [])
    if added_rows:
        new_rows_df = pd.DataFrame(added_rows)
        for col in df.columns:
            if col not in new_rows_df.columns:
                new_rows_df[col] = pd.NA
        new_rows_df = new_rows_df[df.columns]
        df = pd.concat([df, new_rows_df], ignore_index=True)
        has_real_edits = True
        
    if has_real_edits:
        st.session_state[df_key] = df
        save_release_notes_shared()

def sync_custom_table_changes(table_idx, editor_key, display_df_indices):
    edits = st.session_state.get(editor_key)
    if not edits or table_idx >= len(st.session_state.custom_tables):
        return
    df = st.session_state.custom_tables[table_idx]["df"].copy()
    
    has_real_edits = False
    
    # Edited rows
    for idx_str, col_changes in edits.get("edited_rows", {}).items():
        idx = int(idx_str)
        if idx < len(display_df_indices):
            actual_idx = display_df_indices[idx]
            for col, val in col_changes.items():
                if col != "Select":
                    df.at[actual_idx, col] = val
                    has_real_edits = True
                
    # Deleted rows
    deleted_indices = edits.get("deleted_rows", [])
    if deleted_indices:
        indices_to_drop = [display_df_indices[i] for i in deleted_indices if i < len(display_df_indices)]
        df = df.drop(index=indices_to_drop)
        df = df.reset_index(drop=True)
        has_real_edits = True
        
    # Added rows
    added_rows = edits.get("added_rows", [])
    if added_rows:
        new_rows_df = pd.DataFrame(added_rows)
        for col in df.columns:
            if col not in new_rows_df.columns:
                new_rows_df[col] = pd.NA
        new_rows_df = new_rows_df[df.columns]
        df = pd.concat([df, new_rows_df], ignore_index=True)
        has_real_edits = True
        
    if has_real_edits:
        st.session_state.custom_tables[table_idx]["df"] = df
        save_release_notes_shared()

# ---------------------------------------------------------
# 1. Environment Loading & Session Setup
# ---------------------------------------------------------
load_dotenv(override=True)

# Configuration: Presets for item types to include during ingestion
env_types = os.getenv("DEFAULT_INCLUDED_TYPES")
if env_types:
    DEFAULT_INCLUDED_TYPES = [t.strip() for t in env_types.split(",") if t.strip()]
else:
    DEFAULT_INCLUDED_TYPES = ["User Story", "Task", "Technical Task", "Technical Sub-task", "Bug"]

DEFAULT_FIX_VERSION = os.getenv("DEFAULT_FIX_VERSION", "v1.3.0")

# Read default environment variables if provided
default_jira_server = os.getenv("JIRA_SERVER", "")
default_jira_token = os.getenv("JIRA_API_TOKEN", "")
default_jira_auth_method = os.getenv("JIRA_AUTH_METHOD", "Jira Server Token (Bearer)")
default_jira_email = os.getenv("JIRA_EMAIL", "")

# Initialize all required Session State keys
default_conf_server = os.getenv("CONFLUENCE_SERVER", "")
default_conf_token = os.getenv("CONFLUENCE_API_TOKEN", "")
default_conf_space = os.getenv("CONFLUENCE_SPACE", "DS")
default_conf_page = os.getenv("CONFLUENCE_PAGE", "Release Notes")

default_project_name = os.getenv("PROJECT_NAME", "PO Tools Enterprise")
default_primary_color = os.getenv("PRIMARY_COLOR", "#3B82F6")

default_filename_sr = os.getenv("DEFAULT_FILENAME_SPRINT_REVIEW", "Sprint_Review_Report")
default_filename_rn = os.getenv("DEFAULT_FILENAME_RELEASE_NOTES", "Release_Notes")

default_release_notes_intro = os.getenv("RELEASE_NOTES_INTRO", """### Dear Users & Partners,

We are excited to share the **Release Notes** for our latest development cycle. In this iteration, our engineering team focused on bolstering platform security, optimizing database transaction queries, and releasing crucial merchant billing integrations.

**Delivered Highlights:**
* **Enhanced Security:** Implemented Google & GitHub OAuth2 Single Sign-On (SSO).
* **Payment Processing:** Integrated recurrent merchant subscription flows via Stripe.
* **Document Services:** Finalized robust PDF invoicing exporters inside staging.

Thank you for your continuous feedback, which drives our iterative development process.

*Best Regards,*  
**The Product Management Team**""")
if default_release_notes_intro:
    default_release_notes_intro = default_release_notes_intro.replace("\\n", "\n")

default_sprint_number = os.getenv("COVER_SPRINT_NAME", "Sprint 14")
default_sprint_welcome_message = os.getenv("SPRINT_WELCOME_MESSAGE", "Welcome to the Sprint Review session. This document consolidates our delivered sprint capabilities, scheduled target releases, and interactive product demo presentations. We thank our engineering team and product stakeholders for their collaborative efforts.")
if default_sprint_welcome_message:
    default_sprint_welcome_message = default_sprint_welcome_message.replace("\\n", "\n")

default_logo_temp_path = os.getenv("LOGO_IMAGE_PATH", "")
if default_logo_temp_path == "":
    default_logo_temp_path = None

default_cover_temp_path = os.getenv("COVER_IMAGE_PATH", "")
if default_cover_temp_path == "":
    default_cover_temp_path = None

default_ov_sprint_num = os.getenv("OVERVIEW_SPRINT_NUM", "1")
default_ov_fix_version = os.getenv("DEFAULT_FIX_VERSION", DEFAULT_FIX_VERSION)

# Initialize all required Session State keys
if 'jira_server' not in st.session_state:
    st.session_state.jira_server = default_jira_server
if 'jira_token' not in st.session_state:
    st.session_state.jira_token = default_jira_token
if 'jira_auth_method' not in st.session_state:
    st.session_state.jira_auth_method = default_jira_auth_method
if 'jira_email' not in st.session_state:
    st.session_state.jira_email = default_jira_email

if 'conf_server' not in st.session_state:
    st.session_state.conf_server = default_conf_server
if 'conf_token' not in st.session_state:
    st.session_state.conf_token = default_conf_token
if 'conf_space_key' not in st.session_state:
    st.session_state.conf_space_key = default_conf_space
if 'conf_page_name' not in st.session_state:
    st.session_state.conf_page_name = default_conf_page

if 'overview_df' not in st.session_state:
    st.session_state.overview_df = None

if 'project_name' not in st.session_state:
    st.session_state.project_name = default_project_name
if 'filename_sr' not in st.session_state:
    st.session_state.filename_sr = default_filename_sr
if 'filename_rn' not in st.session_state:
    st.session_state.filename_rn = default_filename_rn
if 'app_version' not in st.session_state:
    st.session_state.app_version = os.getenv("APP_VERSION", "v1.3.0")
if 'next_release_df' not in st.session_state:
    st.session_state.next_release_df = pd.DataFrame([
        {
            "Version": "v1.3.0",
            "Target Date": "2026-06-15",
            "Comments": "Major central dashboard UI redesign, EKS migration, and compliance preparations."
        }
    ])
if 'primary_color' not in st.session_state or st.session_state.get('prev_env_color') != default_primary_color:
    st.session_state.primary_color = default_primary_color
    st.session_state.prev_env_color = default_primary_color
if 'release_notes_intro' not in st.session_state:
    st.session_state.release_notes_intro = default_release_notes_intro

# Page state for dynamic programmatic navigation
if 'active_tab' not in st.session_state:
    st.session_state.active_tab = "🔌 Ingestion"

# Temporary logo file management
if 'rn_logo_temp_path' not in st.session_state:
    st.session_state.rn_logo_temp_path = default_logo_temp_path

# Starting Cover Page welcome message and cover image states (NEW requested states)
if 'sprint_number' not in st.session_state:
    st.session_state.sprint_number = default_sprint_number

if 'sprint_welcome_message' not in st.session_state:
    st.session_state.sprint_welcome_message = default_sprint_welcome_message

if 'rn_cover_temp_path' not in st.session_state:
    st.session_state.rn_cover_temp_path = default_cover_temp_path

if 'ov_sprint_num' not in st.session_state:
    st.session_state.ov_sprint_num = default_ov_sprint_num
if 'ov_fix_version' not in st.session_state:
    st.session_state.ov_fix_version = default_ov_fix_version

if 'extra_table_df' not in st.session_state:
    st.session_state.extra_table_df = None
if 'extra_table_title' not in st.session_state:
    st.session_state.extra_table_title = ""
if 'custom_tables' not in st.session_state:
    st.session_state.custom_tables = []

if 'change_request' not in st.session_state:
    st.session_state.change_request = ""
if 'release_date' not in st.session_state:
    import datetime
    st.session_state.release_date = datetime.date.today()
if 'show_items_resume' not in st.session_state:
    st.session_state.show_items_resume = True
if 'prepared_release_notes' not in st.session_state:
    st.session_state.prepared_release_notes = None
if 'release_purpose' not in st.session_state:
    st.session_state.release_purpose = "The purpose of this release is to rollout the following functionalities:"

if 'custom_bug_column' not in st.session_state:
    st.session_state.custom_bug_column = {
        "enabled": False,
        "name": os.getenv("BUGS_CUSTOM_COLUMN_NAME", "Severity"),
        "labels": [l.strip() for l in os.getenv("BUGS_CUSTOM_COLUMN_LABELS", "Severity_A;Severity_B;Severity_C").split(";") if l.strip()]
    }

# ---------------------------------------------------------
# Shared Collaboration Sync Logic
# ---------------------------------------------------------
def load_release_notes_shared():
    ov_df = load_df_from_csv("release_notes_overview.csv")
    if ov_df is not None:
        st.session_state.overview_df = ov_df
    cust = load_custom_tables_from_json("release_notes_custom_tables.json")
    if cust:
        st.session_state.custom_tables = cust
    nr_df = load_df_from_csv("release_notes_next_release.csv")
    if nr_df is not None:
        st.session_state.next_release_df = nr_df

# Only run auto-refresh on the Workbook tab to prevent resource overhead and iframe blinking on other tabs
if st.session_state.get("active_tab", "🔌 Ingestion") == "✍️ Workbook":
    setup_autorefresh(
        sync_interval_ms=10000, 
        last_sync_key="release_notes_last_sync", 
        watched_files=RN_FILES, 
        load_callback=load_release_notes_shared
    )



# ---------------------------------------------------------
# 2. Custom CSS styling (Premium dark SaaS UI)
# ---------------------------------------------------------

st.markdown("""
    <style>
         /* General dark theme override */
         .main, .block-container, [data-testid="stAppViewContainer"], [data-testid="stHeader"] {
             background-color: #27272E !important;
             color: #F8FAFC !important;
         }
         
         /* High visibility and premium SaaS styling for file uploader */
         [data-testid="stFileUploadDropzone"] {
             background-color: #18181D !important;
             border: 1px dashed #60A5FA !important;
             border-radius: 12px !important;
         }
         [data-testid="stFileUploadDropzone"] * {
             color: #F8FAFC !important;
         }
         [data-testid="stFileUploaderFileName"], 
         .stFileUploaderFileName, 
         .uploadedFileName {
             color: #FFFFFF !important;
             font-weight: 600 !important;
         }
         [data-testid="stFileUploader"] section {
             background-color: #18181D !important;
             border: 1px solid #3E3E4A !important;
             border-radius: 12px !important;
         }
         [data-testid="stFileUploader"] section * {
             color: #FFFFFF !important;
         }
         
         /* Typography high-contrast styles */
         h1, h2, h3, h4, h5, h6 {
             color: #FFFFFF !important;
             font-weight: 700 !important;
         }
         .stMarkdown p, .stMarkdown li, .stMarkdown span {
             color: #F8FAFC !important;
         }
         label, .stWidgetLabel {
             color: #FFFFFF !important;
             font-weight: 600 !important;
         }
         
         /* Sidebar Toggle Button */
         button[data-testid="collapsedControl"], button[kind="header"] {
             color: #60A5FA !important;
         }
         button[data-testid="collapsedControl"] svg, button[kind="header"] svg {
             fill: #60A5FA !important;
         }
         
         /* File Uploader small limit text */
         [data-testid="stFileUploadDropzone"] small {
             color: #94A3B8 !important;
             font-weight: 500 !important;
         }
         
         /* Sidebar dark theme label improvements */
         section[data-testid="stSidebar"] {
             background-color: #18181D !important;
             border-right: 1px solid #3E3E4A !important;
         }
         section[data-testid="stSidebar"] h1,
         section[data-testid="stSidebar"] h2,
         section[data-testid="stSidebar"] h3,
         section[data-testid="stSidebar"] h4,
         section[data-testid="stSidebar"] label,
         section[data-testid="stSidebar"] .stMarkdown {
             color: #FFFFFF !important;
         }
         
         /* Rounded borders and shadows for dataframes and tables */
         div[data-testid="stDataFrame"] { 
             border-radius: 12px; 
             overflow: hidden; 
             border: 1px solid #3E3E4A !important;
             box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.5);
             background-color: #18181D;
         }
         div[data-testid="stExpander"] { 
             border-radius: 12px !important; 
             border: 1px solid #3E3E4A !important; 
             background-color: #18181D !important;
             box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.4);
             margin-bottom: 1rem;
             overflow: hidden;
         }
         div[data-testid="stExpander"] > details {
             background-color: #18181D !important;
         }
         div[data-testid="stExpander"] summary {
             background-color: #18181D !important;
             color: #FFFFFF !important;
             font-weight: 600;
         }
         div[data-testid="stExpander"] summary:hover {
             background-color: #2D2D38 !important;
         }
         div[data-testid="stExpander"] .stMarkdown {
             color: #F8FAFC !important;
         }
         
         /* General bordered container style */
         div[data-testid="stVerticalBlockBorderWrapper"],
         div.stVerticalBlockBorder {
             border: 1px solid #3E3E4A !important;
             border-radius: 12px !important;
             background-color: #18181D !important;
             padding: 1.5rem !important;
             box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.5) !important;
         }
         
         /* Premium button styles with interactive glow and forced high-contrast text */
         .stButton>button, .stDownloadButton>button, button[data-testid="stBaseButton-secondary"] { 
             border-radius: 24px; 
             border: none; 
             background: linear-gradient(135deg, #3B82F6 0%, #1D4ED8 100%) !important; 
             color: #FFFFFF !important; 
             padding: 0.5rem 0.75rem; 
             font-weight: 600;
             box-shadow: none !important;
             transition: all 0.2s ease-in-out;
         }
         .stButton>button:hover, .stDownloadButton>button:hover, button[data-testid="stBaseButton-secondary"]:hover { 
             background: linear-gradient(135deg, #2563EB 0%, #1D4ED8 100%) !important;
             color: #FFFFFF !important;
             box-shadow: none !important;
             transform: translateY(-1px);
         }
         .stButton>button:disabled, .stDownloadButton>button:disabled, button[data-testid="stBaseButton-secondary"]:disabled {
             background: #3E3E4A !important;
             color: #8E8E9F !important;
             cursor: not-allowed;
             opacity: 0.6;
             transform: none !important;
         }
         
         /* Custom card layouts for exports */
         .export-card {
             background-color: #18181D !important;
             border: 1px solid #3E3E4A !important;
             border-radius: 12px !important;
             padding: 1.2rem !important;
             margin-bottom: 0.8rem;
             transition: all 0.2s ease-in-out;
             box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.4);
         }
         .export-card h3 {
             margin-top: 0 !important;
             color: #FFFFFF !important;
         }
    </style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------
# 3. High-Quality Sprint Mock Datasets
# ---------------------------------------------------------
def generate_mock_overview_data():
    return pd.DataFrame([
        {
            "Key": "PROJ-101",
            "Summary": "Integrate Google & GitHub Social Login (OAuth2)",
            "Epic": "Authentication & Identity",
            "Status": "Done",
            "Fix Version": "v1.2.0-beta",
            "Outlook": "Completed successfully. Ready for deployment.",
            "Sprint Review": True,
            "Release Notes": True,
            "Assignee": "Ana Martinez",
            "Demo": True,
            "Type": "User Story",
            "Labels": "security, frontend, oauth"
        },
        {
            "Key": "PROJ-103",
            "Summary": "Set up Stripe recurrent billing and subscriptions",
            "Epic": "Billing & Core Payments",
            "Status": "Done",
            "Fix Version": "v1.2.0-beta",
            "Outlook": "Finance confirmed production API sandbox keys.",
            "Sprint Review": True,
            "Release Notes": True,
            "Assignee": "Elena Gomez",
            "Demo": True,
            "Type": "User Story",
            "Labels": "billing, stripe, backend"
        },
        {
            "Key": "PROJ-106",
            "Summary": "Develop monthly client invoice PDF generator",
            "Epic": "Billing & Core Payments",
            "Status": "Done",
            "Fix Version": "v1.2.0-beta",
            "Outlook": "Resolved minor page layout issues in Staging environment.",
            "Sprint Review": True,
            "Release Notes": True,
            "Assignee": "Sofia Torres",
            "Demo": True,
            "Type": "Task",
            "Labels": "billing, pdf-generation"
        },
        {
            "Key": "PROJ-110",
            "Summary": "Resolve security vulnerabilities and patch NPM packages",
            "Epic": "Technical Debt",
            "Status": "Done",
            "Fix Version": "v1.1.9",
            "Outlook": "Completed technical debt ticket without incident.",
            "Sprint Review": False,
            "Release Notes": False,
            "Assignee": "Diego Diaz",
            "Demo": False,
            "Type": "Technical Task",
            "Labels": "security, tech-debt"
        },
        {
            "Key": "PROJ-111",
            "Summary": "Fix session expiration memory leak on staging site",
            "Epic": "Authentication & Identity",
            "Status": "Done",
            "Fix Version": "v1.2.0-beta",
            "Outlook": "Patched express-session cookie expiration handlers.",
            "Sprint Review": True,
            "Release Notes": True,
            "Assignee": "Ana Martinez",
            "Demo": False,
            "Type": "Bug",
            "Labels": "bugfix, auth, memory-leak"
        }
    ])



# Callback to load mock sprint datasets securely before widget instantiation
def load_mock_sprint_data():
    st.session_state.overview_df = generate_mock_overview_data()
    
    # Filter by user checkboxes if key exists, otherwise use DEFAULT_INCLUDED_TYPES preset
    inc_all = st.session_state.get("inc_all", False)
    if not inc_all:
        selected_types = []
        if st.session_state.get("inc_story", "User Story" in DEFAULT_INCLUDED_TYPES): selected_types.append("User Story")
        if st.session_state.get("inc_task", "Task" in DEFAULT_INCLUDED_TYPES): selected_types.append("Task")
        if st.session_state.get("inc_tech", "Technical Task" in DEFAULT_INCLUDED_TYPES): selected_types.append("Technical Task")
        if st.session_state.get("inc_subtask", "Technical Sub-task" in DEFAULT_INCLUDED_TYPES):
            selected_types.extend(["Technical Sub-task", "Sub-task"])
        if st.session_state.get("inc_bug", "Bug" in DEFAULT_INCLUDED_TYPES): selected_types.append("Bug")
        
        if st.session_state.overview_df is not None and not st.session_state.overview_df.empty:
            st.session_state.overview_df = st.session_state.overview_df[st.session_state.overview_df["Type"].isin(selected_types)].reset_index(drop=True)

    if 'Fix Version' in st.session_state.overview_df.columns:
        st.session_state.overview_df['Fix Version'] = st.session_state.overview_df['Fix Version'].apply(extract_numeric_version)
    st.session_state.ov_sprint_num = "12"
    st.session_state.custom_tables = [
        {
            "title": "Special Performance Metrics",
            "df": pd.DataFrame([
                {"Select": False, "Key": "PROJ-201", "Metric Name": "API Response Time (p95)", "Value": "180ms", "Status": "Done"},
                {"Select": False, "Key": "PROJ-202", "Metric Name": "Database CPU Load", "Value": "24%", "Status": "In Progress"}
            ]),
            "position": "Before Demo Table"
        }
    ]
    st.toast("🚀 Loaded mock datasets successfully!", icon="🔥")
    save_release_notes_shared()

# ---------------------------------------------------------

# 4. Jira Connection API Fetcher (Bearer Token Auth PAT)
# ---------------------------------------------------------
from utils.release_helpers import (
    fetch_jira_tickets_dataset as _fetch_jira,
    jira_request, extract_release_version,
    find_history_value, format_release_date, build_release_purpose_draft,
    parse_confluence_history_rows, fetch_release_history,
    prepare_release_notes_from_version_url as _prepare_rn,
    publish_release_note_to_history as _publish_rn,
    upload_pdf_to_confluence, build_prepared_release_notes_pdf as _build_prep_rn,
    build_release_notes_pdf as _build_rn
)

def fetch_jira_tickets_dataset(server, token, query_val, query_mode="sprint", auth_type="Personal Access Token (Bearer PAT)", email=""):
    return _fetch_jira(server, token, query_val, query_mode, auth_type, email, config=st.session_state)

def prepare_release_notes_from_version_url(version_url):
    return _prepare_rn(version_url, config=st.session_state)

def publish_release_note_to_history(prepared, pdf_bytes, filename):
    return _publish_rn(prepared, pdf_bytes, filename, config=st.session_state)

def build_prepared_release_notes_pdf(prepared):
    return _build_prep_rn(prepared, config=st.session_state)

def build_release_notes_pdf(overview_df):
    return _build_rn(overview_df, config=st.session_state)

# 8. Main Application Interface Rendering
# ---------------------------------------------------------
st.title("📣 Release Notes Generator")
st.markdown("Automate and customize Release Notes reports securely by connecting directly to **Jira** or importing local files.")

# Programmatic Navigation Sidebar
st.sidebar.markdown("### 🧭 Navigation Panel")
options_en = ["🔌 Ingestion", "✍️ Workbook", "💾 Exporter"]

if 'active_tab' not in st.session_state or st.session_state.active_tab not in options_en:
    st.session_state.active_tab = "🔌 Ingestion"

selected_nav = st.sidebar.radio(
    "Select current step:",
    options=options_en,
    index=options_en.index(st.session_state.active_tab)
)

if selected_nav != st.session_state.active_tab:
    st.session_state.active_tab = selected_nav
    st.rerun()



# ---------------------------------------------------------
# STEP 1: Ingestion & Connection
# ---------------------------------------------------------
if st.session_state.active_tab == "🔌 Ingestion":
    st.subheader("🔌 Jira Backlog Ingestion")
    st.write("Configure connection details below to load the **Overview** (What We Did) ticket dataset.")
    
    # Ingestion Flash Feedback
    if "ingestion_feedback" in st.session_state:
        fb = st.session_state.ingestion_feedback
        if fb.get("type") == "success":
            st.success(fb.get("text"))
        elif fb.get("type") == "error":
            st.error(fb.get("text"))
        elif fb.get("type") == "warning":
            st.warning(fb.get("text"))
        del st.session_state.ingestion_feedback
    
    # Credentials block

    # Centralized Integration Status
    jira_status = st.session_state.get("jira_connection_status")
    jira_msg = st.session_state.get("jira_connection_msg", "Not checked yet")
    conf_status = st.session_state.get("conf_connection_status")
    conf_msg = st.session_state.get("conf_connection_msg", "Not checked yet")

    col_stat1, col_stat2 = st.columns([1, 1])
    with col_stat1:
        if not st.session_state.jira_server:
            st.warning("⚠️ **Jira**: Not configured.")
        elif jira_status == "Success":
            st.success(f"🟢 **Jira Connected**: `{st.session_state.jira_server}` ({jira_msg})")
        elif jira_status == "Failed":
            st.error(f"🔴 **Jira Connection Failed**: `{st.session_state.jira_server}` ({jira_msg})")
        else:
            st.info(f"🟡 **Jira Configured**: `{st.session_state.jira_server}` (Status: {jira_msg})")
            
    with col_stat2:
        if not st.session_state.conf_server:
            st.warning("⚠️ **Confluence**: Not configured.")
        elif conf_status == "Success":
            st.success(f"🟢 **Confluence Connected**: `{st.session_state.conf_server}` ({conf_msg})")
        elif conf_status == "Failed":
            st.error(f"🔴 **Confluence Connection Failed**: `{st.session_state.conf_server}` ({conf_msg})")
        else:
            st.info(f"🟡 **Confluence Configured**: `{st.session_state.conf_server}` (Status: {conf_msg})")
            
    st.caption("Credentials can be configured centrally on the **Home Hub** page under the **Centralized Integrations** tab.")


    st.subheader("📝 Prepare Release Note")
    st.write("Paste the Jira version link to load the release metadata, resolved issues, and known residual anomalies.")
    jira_version_link_base = os.getenv("JIRA_VERSION_LINK_BASE", "https://your-company.atlassian.net/jira/projects/MYPROJECT/versions/")
    release_version_url = st.text_input(
        "Jira version link",
        value=jira_version_link_base,
        placeholder=f"{jira_version_link_base}543216",
        key="release_version_url"
    )
    purpose_in = st.text_area(
        "Release purpose",
        value=st.session_state.release_purpose,
        height=120
    )
    st.session_state.release_purpose = purpose_in
    st.caption("The Jira and Confluence fields are prefilled from your local `.env` file. Any change here is used only in the current session.")
    if st.button("✨ Prepare Release Note", use_container_width=True):
        if not st.session_state.conf_token:
            st.error("Enter a Confluence Personal Access Token to load the release history.")
            st.stop()
        try:
            with st.spinner("Loading release metadata, tickets, and residual anomalies..."):
                prepared = prepare_release_notes_from_version_url(release_version_url)
            st.session_state.prepared_release_notes = prepared
            st.session_state.app_version = prepared["version"]
            st.session_state.overview_df = prepared["resolved"].copy()
            st.session_state.release_purpose = prepared["purpose_draft"]
            st.success(f"Release {prepared['version']} is ready. Review the generated purpose text and export the PDF.")
            st.rerun()
        except ValueError as error:
            st.error(str(error))

    # The version-link workflow replaces the legacy generic ingestion controls below.
    st.stop()

    st.divider()

    # Issue Type Selection Checkboxes
    st.markdown("**2. Filter Ingested Issue Types**")
    st.write("Select which issue types should be loaded into the workspace:")
    col_cb1, col_cb2, col_cb3, col_cb4, col_cb5, col_cb6 = st.columns(6)
    with col_cb1:
        inc_all = st.checkbox("All / Everything", value=False, key="inc_all")
    with col_cb2:
        inc_story = st.checkbox("User Story", value=("User Story" in DEFAULT_INCLUDED_TYPES), key="inc_story", disabled=inc_all)
    with col_cb3:
        inc_task = st.checkbox("Task", value=("Task" in DEFAULT_INCLUDED_TYPES), key="inc_task", disabled=inc_all)
    with col_cb4:
        inc_tech = st.checkbox("Technical Task", value=("Technical Task" in DEFAULT_INCLUDED_TYPES), key="inc_tech", disabled=inc_all)
    with col_cb5:
        inc_subtask = st.checkbox("Sub-task", value=("Technical Sub-task" in DEFAULT_INCLUDED_TYPES), key="inc_subtask", disabled=inc_all)
    with col_cb6:
        inc_bug = st.checkbox("Bug", value=("Bug" in DEFAULT_INCLUDED_TYPES), key="inc_bug", disabled=inc_all)

    selected_types = []
    if not inc_all:
        if inc_story: selected_types.append("User Story")
        if inc_task: selected_types.append("Task")
        if inc_tech: selected_types.append("Technical Task")
        if inc_subtask:
            selected_types.extend(["Technical Sub-task", "Sub-task"])
        if inc_bug: selected_types.append("Bug")

    st.divider()
    
    # Query extraction panel
    st.markdown("**3. Ingestion Query Configuration**")
    with st.container(border=True):
        st.markdown("#### 🚀 Overview (Delivered Items)")
        st.write("Load tickets representing completed work from the current cycle.")
        
        ov_query_type = st.radio(
            "Overview Query Type:",
            options=["Overview Fix Version", "Overview JQL Query"],
            horizontal=True,
            key="ov_query_type"
        )
        
        if ov_query_type == "Overview Fix Version":
            ov_val = st.text_input("Fix Version:", value=st.session_state.ov_fix_version, key="ov_fix_version")
            query_mode = "fix_version"
        else:
            ov_val = st.text_area("JQL Query:", value="project = 'PROJ' AND status = 'Done' AND fixVersion = 'v1.3.0'", key="ov_jql_text")
            query_mode = "custom"
            
        if st.button("🔌 Fetch Overview", use_container_width=True):
            with st.spinner("Downloading delivered tickets from Jira..."):
                res_df = fetch_jira_tickets_dataset(
                    st.session_state.jira_server,
                    st.session_state.jira_token,
                    ov_val,
                    query_mode=query_mode,
                    auth_type=st.session_state.jira_auth_method,
                    email=st.session_state.jira_email
                )
                if res_df is not None:
                    if not res_df.empty and not inc_all:
                        res_df = res_df[res_df["Type"].isin(selected_types)].reset_index(drop=True)
                    st.session_state.overview_df = res_df
                    if res_df.empty:
                        st.session_state.ingestion_feedback = {"type": "warning", "text": f"⚠️ JQL/FixVersion query returned 0 tickets for Overview."}
                    else:
                        st.session_state.ingestion_feedback = {"type": "success", "text": f"🟢 Successfully loaded {len(res_df)} Overview tickets!"}
                    st.rerun()
                    st.session_state.ingestion_feedback = {"type": "error", "text": "❌ Failed to connect or load Overview tickets from Jira. Check server credentials or query parameters."}
                    st.rerun()
    
    # Ingestion of Additional Custom Report Tables (Jira or CSV)
    st.subheader("📊 Ingestion of Additional Custom Table (Optional)")
    st.write("Retrieve custom data from Jira or upload a CSV file to render a custom table in a dedicated section of the document.")
    
    col_add_title, col_add_pos, col_add_source = st.columns([2, 1, 1])
    with col_add_title:
        extra_table_title = st.text_input("Additional Table Title:", value="Special Project Metrics", key="extra_table_title_input")
    with col_add_pos:
        extra_table_position = st.selectbox("Position in Report:", options=["Before Demo Table", "After Demo Table"], key="extra_table_pos_input")
    with col_add_source:
        extra_source_type = st.radio(
            "Custom Table Data Source:",
            options=["CSV Upload", "Jira JQL Query"],
            horizontal=True,
            key="extra_source_type"
        )
        
    if extra_source_type == "CSV Upload":
        up_extra = st.file_uploader("Upload Additional Table CSV File:", type=["csv"], key=f"uploader_extra_{len(st.session_state.custom_tables)}")
        if up_extra:
            if st.button("➕ Import Custom Table", use_container_width=True):
                try:
                    extra_df = pd.read_csv(up_extra)
                    if 'Select' not in extra_df.columns:
                        extra_df.insert(0, 'Select', False)
                    if 'Labels' not in extra_df.columns:
                        extra_df['Labels'] = ""
                    new_table = {
                        "title": extra_table_title if extra_table_title.strip() != "" else "Special Project Metrics",
                        "df": extra_df,
                        "position": extra_table_position
                    }
                    st.session_state.custom_tables.append(new_table)
                    ext_idx = len(st.session_state.custom_tables) - 1
                    for key_prefix in ["rename_ext_", "pos_ext_", "editor_ext_", "cols_select_", "sort_ext_", "filter_ext_"]:
                        for key in list(st.session_state.keys()):
                            if key.startswith(f"{key_prefix}{ext_idx}"):
                                del st.session_state[key]
                    st.toast(f"Custom table '{new_table['title']}' uploaded successfully!", icon="📊")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error parsing Additional CSV: {str(e)}")
    else:
        # Jira JQL Query
        custom_jql = st.text_area("JQL Query for Custom Table:", value="project = 'PROJ' AND status = 'In Progress'", key="extra_jql_text")
        if st.button("🔌 Fetch Custom Table", use_container_width=True):
            with st.spinner("Downloading custom tickets from Jira..."):
                res_df = fetch_jira_tickets_dataset(
                    st.session_state.jira_server,
                    st.session_state.jira_token,
                    custom_jql,
                    is_sprint=False,
                    auth_type=st.session_state.jira_auth_method,
                    email=st.session_state.jira_email
                )
                if res_df is not None:
                    # Filter to standard presentation columns so it renders beautifully in ReportLab
                    cols_to_keep = ["Key", "Summary", "Epic", "Status", "Fix Version", "Assignee", "Labels"]
                    cols_to_keep = [c for c in cols_to_keep if c in res_df.columns]
                    extra_df = res_df[cols_to_keep]
                    if 'Select' not in extra_df.columns:
                        extra_df.insert(0, 'Select', False)
                    if 'Labels' not in extra_df.columns:
                        extra_df['Labels'] = ""
                    new_table = {
                        "title": extra_table_title if extra_table_title.strip() != "" else "Special Project Metrics",
                        "df": extra_df,
                        "position": extra_table_position
                    }
                    st.session_state.custom_tables.append(new_table)
                    ext_idx = len(st.session_state.custom_tables) - 1
                    for key_prefix in ["rename_ext_", "pos_ext_", "editor_ext_", "cols_select_", "sort_ext_", "filter_ext_"]:
                        for key in list(st.session_state.keys()):
                            if key.startswith(f"{key_prefix}{ext_idx}"):
                                del st.session_state[key]
                    st.toast(f"Success! Loaded {len(res_df)} tickets for custom table '{new_table['title']}'.", icon="📊")
                    st.rerun()

    st.divider()
    
    # Offline Sandbox / local imports
    st.subheader("💡 Sandbox & Mock Playground")
    st.write("Don't have a live connection? Use these sandbox tools to instantly populate the Overview dataset:")
    
    col_sand1, col_sand2 = st.columns([1, 1])
    
    with col_sand1:
        st.button("🚀 Load Mock Sprint Datasets", use_container_width=True, on_click=load_mock_sprint_data)
 
    with col_sand2:
        st.write("Or import raw local CSV files:")
        up_ov = st.file_uploader("Upload Overview CSV File:", type=["csv"], key="rn_uploader_ov")
        
        # Ingest Overview CSV
        if up_ov:
            try:
                csv_df = pd.read_csv(up_ov)
                # Normalization
                rename_map = {
                    'key': 'Key',
                    'issue key': 'Key',
                    'summary': 'Summary',
                    'status': 'Status',
                    'fix version': 'Fix Version',
                    'fix version/s': 'Fix Version',
                    'fix versions': 'Fix Version',
                    'assignee': 'Assignee',
                    'type': 'Type',
                    'issue type': 'Type',
                    'issuetype': 'Type',
                    'labels': 'Labels'
                }
                norm_cols = {}
                epic_col_detected = None
                for c in csv_df.columns:
                    c_lower = c.strip().lower()
                    if c_lower == 'epic':
                        epic_col_detected = c
                        break
                    elif c_lower == 'custom field (epic link)' or c_lower == 'epic link':
                        epic_col_detected = c
                    elif c_lower == 'custom field (epic name)' or c_lower == 'epic name':
                        if not epic_col_detected:
                            epic_col_detected = c
                for c in csv_df.columns:
                    for k_map, v_map in rename_map.items():
                        if c.strip().lower() == k_map:
                            norm_cols[c] = v_map
                if epic_col_detected:
                    norm_cols[epic_col_detected] = 'Epic'
                if norm_cols:
                    csv_df = csv_df.rename(columns=norm_cols)
                for req in ['Key', 'Summary', 'Epic', 'Status', 'Fix Version']:
                    if req not in csv_df.columns:
                        csv_df[req] = "N/A"
                if 'Assignee' not in csv_df.columns:
                    csv_df['Assignee'] = "Unassigned"
                if 'Type' not in csv_df.columns:
                    csv_df['Type'] = "User Story"
                if 'Labels' not in csv_df.columns:
                    csv_df['Labels'] = ""
                csv_df['Outlook'] = ""
                csv_df['Sprint Review'] = True
                csv_df['Release Notes'] = True
                csv_df['Demo'] = False
                if 'Fix Version' in csv_df.columns:
                    csv_df['Fix Version'] = csv_df['Fix Version'].apply(extract_numeric_version)
                if 'Epic' in csv_df.columns:
                     csv_df['Epic'] = csv_df['Epic'].apply(lambda x: "-" if pd.isna(x) or str(x).strip() in ["", "No Epic", "nan"] else str(x).strip())
                     if st.session_state.get("jira_server") and st.session_state.get("jira_token"):
                         try:
                             epic_keys = set(csv_df['Epic'].dropna().unique()) - {"-"}
                             epic_keys = {k for k in epic_keys if " - " not in k}
                             if epic_keys:
                                 keys_str = ",".join([f"'{k}'" for k in epic_keys])
                                 url = f"{st.session_state.jira_server.rstrip('/')}/rest/api/2/search"
                                 headers = {"Accept": "application/json", "Content-Type": "application/json"}
                                 if st.session_state.get("jira_auth_method") == "Corporate Login (Username + Password)" or st.session_state.get("jira_auth_method") == "Jira Cloud/Server Basic (Email/User + Token)":
                                     auth = (st.session_state.get("jira_email", "").strip(), st.session_state.get("jira_token", "").strip())
                                 else:
                                     headers["Authorization"] = f"Bearer {st.session_state.jira_token.strip()}"
                                     auth = None
                                 
                                 params = {"jql": f"key in ({keys_str})", "fields": "key,summary", "maxResults": 100}
                                 if auth:
                                     resp = requests.get(url, headers=headers, params=params, auth=auth, timeout=10)
                                 else:
                                     resp = requests.get(url, headers=headers, params=params, timeout=10)
                                     
                                 if resp.status_code == 200:
                                     epic_map = {}
                                     for issue in resp.json().get("issues", []):
                                         ek = issue.get("key")
                                         es = (issue.get("fields") or {}).get("summary")
                                         if ek and es:
                                             epic_map[ek] = f"{ek} - {es}"
                                     csv_df['Epic'] = csv_df['Epic'].apply(lambda x: epic_map.get(x, x))
                         except Exception:
                             pass
                
                if not inc_all:
                    csv_df = csv_df[csv_df["Type"].isin(selected_types)].reset_index(drop=True)
                st.session_state.overview_df = csv_df[['Key', 'Summary', 'Epic', 'Status', 'Fix Version', 'Labels', 'Outlook', 'Sprint Review', 'Release Notes', 'Assignee', 'Demo', 'Type']]
                st.success("Overview CSV loaded.")
                st.rerun()
            except Exception as e:
                st.error(f"Error parsing Overview CSV: {str(e)}")

# ---------------------------------------------------------
# STEP 2: Worktable Workspace (Tabbed Data Editor)
# ---------------------------------------------------------
elif st.session_state.active_tab == "✍️ Workbook":
    st.markdown(
        f"<div style='background-color:#1E3A8A; padding:8px 12px; border-radius:8px; border:1px solid #3B82F6; margin-bottom:12px; font-size:14px; color:#F8FAFC; display:flex; align-items:center; gap:8px;'>"
        f"<span style='color:#10B981; font-weight:bold;'>🟢 Connected to Shared Workbook</span> "
        f"<span style='color:#94A3B8;'>|</span> "
        f"<span style='color:#E2E8F0;'>Simultaneous collaboration enabled (auto-refresh active)</span>"
        f"</div>",
        unsafe_allow_html=True
    )
    
    label_sources = [st.session_state.overview_df]
    found_labels = {
        label.strip()
        for df in label_sources
        if df is not None and "Labels" in df.columns
        for labels in df["Labels"].dropna()
        for label in str(labels).split(",")
        if label.strip()
    }
    available_labels = sorted(list(found_labels), key=str.lower)
    
    st.markdown("#### 🐛 Bugs Table Configuration")
    st.caption("Enable a custom column in the Bugs tables by selecting the exact labels you want to extract.")
    
    col_bug1, col_bug2 = st.columns([1, 2])
    with col_bug1:
        bug_col_name = st.text_input("Custom Column Name:", value=st.session_state.custom_bug_column.get("name", "Severity"), key="rn_bug_col_name")
        if bug_col_name != st.session_state.custom_bug_column.get("name"):
            st.session_state.custom_bug_column["name"] = bug_col_name
            
    with col_bug2:
        current_labels = st.session_state.custom_bug_column.get("labels", [])
        for l in current_labels:
            if l not in available_labels:
                available_labels.append(l)
                
        bug_col_labels = st.multiselect(
            "Labels to Match",
            options=available_labels,
            default=current_labels,
            placeholder="Choose labels to extract...",
            key="rn_bug_col_labels_ms"
        )
        if bug_col_labels != st.session_state.custom_bug_column.get("labels"):
            st.session_state.custom_bug_column["labels"] = bug_col_labels
            st.session_state.custom_bug_column["enabled"] = len(bug_col_labels) > 0
            st.rerun()
            
    st.divider()

    if st.session_state.get("prepared_release_notes") is not None:
        st.subheader("✍️ Review Release Note data")
        st.write("Remove or adjust rows before export. These are the only two ticket tables used in the Release Note.")
        release_tickets_tab, residual_bugs_tab = st.tabs(["✅ Release tickets", "⚠️ Known residual bugs"])
        with release_tickets_tab:
            prepared = st.session_state.prepared_release_notes
            release_df = prepared["resolved"].copy()
            if "Select" not in release_df.columns:
                release_df.insert(0, "Select", False)
            release_df = st.data_editor(
                release_df,
                num_rows="fixed",
                use_container_width=True,
                key="prepared_release_tickets_editor",
                disabled=[column for column in release_df.columns if column != "Select"],
                column_order=["Select", "Type", "Key", "Summary", "Epic", "Status"]
            )
            if st.button("Remove selected release tickets", use_container_width=True):
                prepared["resolved"] = release_df[~release_df["Select"]].drop(columns=["Select"]).reset_index(drop=True)
                st.rerun()
        with residual_bugs_tab:
            prepared = st.session_state.prepared_release_notes
            residual_df = prepared["residual"].copy()
            if "Select" not in residual_df.columns:
                residual_df.insert(0, "Select", False)
            residual_df = st.data_editor(
                residual_df,
                num_rows="fixed",
                use_container_width=True,
                key="prepared_residual_bugs_editor",
                disabled=[column for column in residual_df.columns if column != "Select"],
                column_order=["Select", "Key", "Summary", "Epic", "Status", "Labels"]
            )
            if st.button("Remove selected residual bugs", use_container_width=True):
                prepared["residual"] = residual_df[~residual_df["Select"]].drop(columns=["Select"]).reset_index(drop=True)
                st.rerun()
        st.stop()
    st.subheader("✍️ Workspace Workbooks: Commercial Fine-Tuning")
    st.write("Convert technical Jira summaries into elegant commercial feature descriptions, assign demo presenters, and toggle report targets.")
    if st.session_state.overview_df is None and not st.session_state.custom_tables:
        st.info("⚠️ Workspace empty. Please complete step **🔌 1. Ingestion & Connection** by fetching Jira data or clicking 'Load Mock Sprint Datasets'.")
    else:
        # Sorting callback functions
        def trigger_sort_ov():
            col = st.session_state.sort_ov_col
            direction = st.session_state.sort_ov_dir
            if col != "🔍 Sort by..." and st.session_state.overview_df is not None:
                ascending = (direction == "Ascending")
                st.session_state.overview_df = st.session_state.overview_df.sort_values(
                    by=col, ascending=ascending
                ).reset_index(drop=True)
                st.session_state.overview_df["Select"] = False

        def trigger_sort_ext(table_idx):
            col = st.session_state.get(f"sort_ext_{table_idx}_col", "🔍 Sort by...")
            direction = st.session_state.get(f"sort_ext_{table_idx}_dir", "Ascending")
            if col != "🔍 Sort by..." and len(st.session_state.custom_tables) > table_idx:
                ascending = (direction == "Ascending")
                df = st.session_state.custom_tables[table_idx]["df"]
                st.session_state.custom_tables[table_idx]["df"] = df.sort_values(
                    by=col, ascending=ascending
                ).reset_index(drop=True)
                st.session_state.custom_tables[table_idx]["df"]["Select"] = False

        # Subtabs for separating Overview and optional Custom Table editing
        tabs_list = [
            "🚀 Overview Dataset (What We Have Done)"
        ]
        for idx, table in enumerate(st.session_state.custom_tables):
            tabs_list.append(f"📊 Custom: {table['title']}")
            
        tabs = st.tabs(tabs_list)
        work_subtab_ov = tabs[0]
        work_subtabs_ext = tabs[1:]
        
        # Ensure 'Select' and 'Labels' columns are initialized
        if st.session_state.overview_df is not None:
            if 'Select' not in st.session_state.overview_df.columns:
                st.session_state.overview_df.insert(0, 'Select', False)
            if 'Labels' not in st.session_state.overview_df.columns:
                st.session_state.overview_df['Labels'] = ""
        
        # Overview Dataset Workspace
        with work_subtab_ov:
            st.markdown("#### 🚀 Delivered Sprint Deliverables (Overview)")
            st.write("Edit completed features. Check the **Demo** box to assign a presentation scheduled for the final PDF demos block.")
            
            if st.session_state.overview_df is None:
                st.warning("Overview dataset is empty. Load it from Step 1.")
            else:
                ov_statuses = sorted(list(set(st.session_state.overview_df["Status"].dropna().unique()).union({"To Do", "In Progress", "Done", "Blocked", "Open", "Closed"})))
                col_cfg_ov = {
                    "Select": st.column_config.CheckboxColumn("Select 🗑️", default=False, width="small"),
                    "Key": st.column_config.TextColumn("Key 🔑", disabled=False),
                    "Type": st.column_config.SelectboxColumn("Type 🏷️", options=["User Story", "Task", "Technical Task", "Technical Sub-task", "Bug", "Epic", "Improvement", "Sub-task", "Other"], width="medium", default="User Story"),
                    "Summary": st.column_config.TextColumn("Summary ✍️", width="large"),
                    "Epic": st.column_config.TextColumn("Epic 🎯", width="medium"),
                    "Status": st.column_config.SelectboxColumn("Status 🚥", options=ov_statuses, width="small"),
                    "Fix Version": st.column_config.TextColumn("Fix Version 📦", width="small"),
                    "Labels": st.column_config.TextColumn("Labels 🏷️", width="medium"),
                    "Assignee": st.column_config.TextColumn("Responsible 👤", width="medium"),
                    "Demo": st.column_config.CheckboxColumn("Demo 📌", default=False, width="small"),
                    "Sprint Review": st.column_config.CheckboxColumn("Sprint Review 📋", default=True, width="small"),
                    "Release Notes": st.column_config.CheckboxColumn("Release Notes 📣", default=True, width="small")
                }
                
                # Check selection state dynamically
                has_selected_ov = check_any_selected("overview_df", "editor_ov_refined", list(st.session_state.overview_df.index))
                
                # Sorting & Filtering controls
                sc_col1, sc_col2, sc_col3 = st.columns([4.0, 4.0, 4.0])
                with sc_col1:
                    sort_col = st.selectbox("Sort by:", ["🔍 Sort by...", "Key", "Epic", "Status", "Type", "Fix Version", "Labels"], key="sort_ov_col", label_visibility="collapsed", on_change=trigger_sort_ov)
                with sc_col2:
                    sort_dir = st.selectbox("Order:", ["Ascending", "Descending"], key="sort_ov_dir", label_visibility="collapsed", on_change=trigger_sort_ov)
                with sc_col3:
                    filter_label = st.text_input("Filter by Label:", key="filter_ov_label", placeholder="🔍 Filter by label...", label_visibility="collapsed")
  
                # Apply filter to display
                display_df = st.session_state.overview_df
                if "Labels" in display_df.columns and filter_label.strip() != "":
                    display_df = display_df[display_df["Labels"].str.contains(filter_label.strip(), case=False, na=False)]
 
                st.data_editor(
                    display_df,
                    num_rows="dynamic",
                    use_container_width=True,
                    column_config=col_cfg_ov,
                    key="editor_ov_refined",
                    on_change=sync_editor_changes,
                    args=("overview_df", "editor_ov_refined", list(display_df.index))
                )
                
                # Bottom action controls
                has_selected_ov_visible = bool(display_df["Select"].any()) if "Select" in display_df.columns else False
                toggle_icon_ov = "☑️" if has_selected_ov_visible else "⬜"
                
                bot_col_sel, bot_col1, bot_col2 = st.columns([1.0, 2.5, 8.5])
                with bot_col_sel:
                    if st.button(toggle_icon_ov, key="btn_toggle_sel_ov", use_container_width=True, help="Select/Deselect All"):
                        if has_selected_ov_visible:
                            st.session_state.overview_df.loc[display_df.index, "Select"] = False
                        else:
                            st.session_state.overview_df.loc[display_df.index, "Select"] = True
                        st.rerun()
                    
                with bot_col1:
                    if st.button("Delete", key="btn_del_ov", use_container_width=True, disabled=not has_selected_ov):
                        # Filter out selected keys from master dataframe
                        selected_rows = get_selected_rows("overview_df", "editor_ov_refined", list(display_df.index))
                        selected_keys = selected_rows["Key"].tolist()
                        st.session_state.overview_df = st.session_state.overview_df[
                            ~st.session_state.overview_df["Key"].isin(selected_keys)
                        ].reset_index(drop=True)
                        st.toast("Deleted selected rows!", icon="🗑️")
                        st.rerun()
                with bot_col2:
                    if st.button("Clear", key="btn_clear_ov", use_container_width=True):
                        st.session_state.overview_df = None
                        st.rerun()
 
                # Duplicate in: dropdown menu
                dest_options = ["Select destination..."]
                if st.session_state.custom_tables:
                    dest_options.extend([f"Custom Table: {t['title']}" for t in st.session_state.custom_tables])
                
                dup_dest = st.selectbox(
                    "Duplicate in:",
                    options=dest_options,
                    key="dup_dest_ov",
                    disabled=not has_selected_ov or len(dest_options) <= 1
                )
                
                if dup_dest != "Select destination...":
                    selected_rows = get_selected_rows("overview_df", "editor_ov_refined", list(display_df.index))
                    if dup_dest.startswith("Custom Table: "):
                        target_title = dup_dest.replace("Custom Table: ", "")
                        target_idx = None
                        for idx, t in enumerate(st.session_state.custom_tables):
                            if t["title"] == target_title:
                                target_idx = idx
                                break
                        if target_idx is not None:
                            target_df = st.session_state.custom_tables[target_idx]["df"]
                            cols_to_keep = [col for col in target_df.columns if col != "Select"]
                            for col in cols_to_keep:
                                if col not in selected_rows.columns:
                                    selected_rows[col] = "-"
                            selected_rows = selected_rows[cols_to_keep]
                            selected_rows.insert(0, "Select", False)
                            
                            st.session_state.custom_tables[target_idx]["df"] = pd.concat([target_df, selected_rows], ignore_index=True)
                            st.session_state.overview_df["Select"] = False
                            st.toast(f"Duplicated selected rows to Custom Table '{target_title}'!", icon="📊")
                            st.session_state.dup_dest_ov = "Select destination..."
                            st.rerun()
                 # Custom Table Dataset Workspace
        for ext_idx, ext_tab in enumerate(work_subtabs_ext):
            with ext_tab:
                table_data = st.session_state.custom_tables[ext_idx]
                title_val = table_data["title"]
                display_title = title_val if title_val.strip() != "" else f"Custom Table {ext_idx + 1}"
                
                st.markdown(f"#### 📊 Custom Table: {display_title}")
                
                # Title, Position inputs
                edit_col_title, edit_col_pos = st.columns([3, 2])
                with edit_col_title:
                    new_title = st.text_input("Rename Table Title:", value=title_val, key=f"rename_ext_{ext_idx}")
                    if new_title != title_val:
                        st.session_state.custom_tables[ext_idx]["title"] = new_title
                        st.rerun()
                with edit_col_pos:
                    new_pos = st.selectbox("Position in Report:", options=["Before Demo Table", "After Demo Table"], index=0 if table_data["position"] == "Before Demo Table" else 1, key=f"pos_ext_{ext_idx}")
                    if new_pos != table_data["position"]:
                        st.session_state.custom_tables[ext_idx]["position"] = new_pos
                        st.rerun()
                
                df_ext = table_data["df"]
                if 'Select' not in df_ext.columns:
                    df_ext.insert(0, 'Select', False)
                
                col_cfg_ext = {
                    "Select": st.column_config.CheckboxColumn("Select 🗑️", default=False, width="small"),
                }
                if "Status" in df_ext.columns:
                    ext_statuses = sorted(list(set(df_ext["Status"].dropna().unique()).union({"To Do", "In Progress", "Done", "Blocked", "Open", "Closed"})))
                    col_cfg_ext["Status"] = st.column_config.SelectboxColumn("Status 🚥", options=ext_statuses, width="small")
                
                # Sorting & Filtering controls
                sc_col1, sc_col2, sc_col3 = st.columns([4.0, 4.0, 4.0])
                with sc_col1:
                    ext_cols = ["🔍 Sort by..."] + [col for col in df_ext.columns if col != "Select"]
                    sort_col = st.selectbox("Sort by:", ext_cols, key=f"sort_ext_{ext_idx}_col", label_visibility="collapsed", on_change=trigger_sort_ext, args=(ext_idx,))
                with sc_col2:
                    sort_dir = st.selectbox("Order:", ["Ascending", "Descending"], key=f"sort_ext_{ext_idx}_dir", label_visibility="collapsed", on_change=trigger_sort_ext, args=(ext_idx,))
                with sc_col3:
                    filter_label_ext = st.text_input("Filter by Label:", key=f"filter_ext_{ext_idx}_label", placeholder="🔍 Filter by label...", label_visibility="collapsed")
  
                # Apply filter to display
                display_df_ext = df_ext
                if filter_label_ext.strip() != "" and "Labels" in display_df_ext.columns:
                    display_df_ext = display_df_ext[display_df_ext["Labels"].str.contains(filter_label_ext.strip(), case=False, na=False)]
 
                has_selected_ext = check_any_selected_custom(ext_idx, f"editor_ext_{ext_idx}_refined", list(display_df_ext.index))
 
                st.data_editor(
                    display_df_ext,
                    num_rows="dynamic",
                    use_container_width=True,
                    column_config=col_cfg_ext,
                    key=f"editor_ext_{ext_idx}_refined",
                    on_change=sync_custom_table_changes,
                    args=(ext_idx, f"editor_ext_{ext_idx}_refined", list(display_df_ext.index))
                )
                
                # Bottom action controls
                has_selected_ext_visible = bool(display_df_ext["Select"].any()) if "Select" in display_df_ext.columns else False
                toggle_icon_ext = "☑️" if has_selected_ext_visible else "⬜"
                
                bot_col_sel, bot_col1, bot_col2, bot_col3 = st.columns([1.0, 2.5, 2.5, 6.0])
                with bot_col_sel:
                    if st.button(toggle_icon_ext, key=f"btn_toggle_sel_ext_{ext_idx}", use_container_width=True, help="Select/Deselect All"):
                        if has_selected_ext_visible:
                            st.session_state.custom_tables[ext_idx]["df"].loc[display_df_ext.index, "Select"] = False
                        else:
                            st.session_state.custom_tables[ext_idx]["df"].loc[display_df_ext.index, "Select"] = True
                        st.rerun()
                with bot_col1:
                    if st.button("Delete", key=f"btn_del_ext_{ext_idx}", use_container_width=True, disabled=not has_selected_ext):
                        selected_rows = get_selected_rows_custom(ext_idx, f"editor_ext_{ext_idx}_refined", list(display_df_ext.index))
                        if "Key" in df_ext.columns:
                            selected_keys = selected_rows["Key"].tolist()
                            st.session_state.custom_tables[ext_idx]["df"] = df_ext[
                                ~df_ext["Key"].isin(selected_keys)
                            ].reset_index(drop=True)
                        else:
                            st.session_state.custom_tables[ext_idx]["df"] = df_ext.drop(index=selected_rows.index).reset_index(drop=True)
                        st.toast("Deleted selected rows!", icon="🗑️")
                        st.rerun()
                with bot_col2:
                    if st.button("Delete Table", key=f"del_ext_tab_{ext_idx}", use_container_width=True):
                        st.session_state.custom_tables.pop(ext_idx)
                        st.toast(f"Custom table '{display_title}' deleted!", icon="🗑️")
                        st.rerun()
                with bot_col3:
                    if st.button("Clear Data", key=f"btn_clear_ext_{ext_idx}", use_container_width=True):
                        st.session_state.custom_tables[ext_idx]["df"] = pd.DataFrame(columns=df_ext.columns)
                        st.toast("Custom table dataset cleared!", icon="🧹")
                        st.rerun()
# ---------------------------------------------------------
elif st.session_state.active_tab == "🎨 Branding":
    st.subheader("🎨 Visual Customization & Documentation Branding")
    st.write("Configure document metadata, Next Target Release targets, custom logos, and rich welcomes.")
    
    col_br_left, col_br_right = st.columns([1, 1])
    
    with col_br_left:
        st.markdown("**1. Document Metadata & Visuals**")
        
        # Project Name input
        p_name = st.text_input(
            "Project / Company Name:",
            value=st.session_state.project_name,
            placeholder="e.g. PO Tools Enterprise"
        )
        if p_name != st.session_state.project_name:
            st.session_state.project_name = p_name
            st.rerun()
            
        # App Version input
        a_version = st.text_input(
            "App Version:",
            value=st.session_state.app_version,
            placeholder="e.g. v1.3.0"
        )
        if a_version != st.session_state.app_version:
            st.session_state.app_version = a_version
            st.rerun()
            
        # 10 Predefined Premium Corporate Colors Presets
        COLOR_PRESETS_MAP = {
            "Tech Blue": "#3B82F6",
            "Indigo Purple": "#6366F1",
            "Deep Emerald": "#059669",
            "Teal Breeze": "#0D9488",
            "Sunset Gold": "#D97706",
            "Royal Violet": "#7C3AED",
            "Warm Orange": "#EA580C",
            "Crimson Red": "#DC2626",
            "Sleek Charcoal": "#475569",
            "Mint Green": "#10B981"
        }
        
        # Build dropdown options: "Name — #HEX"
        preset_options = [f"{name} — {hex_val}" for name, hex_val in COLOR_PRESETS_MAP.items()]
        preset_options.insert(0, "Custom (use color picker below)")
        
        # Find which preset matches the current color, if any
        current_color = st.session_state.primary_color.upper()
        default_idx = 0  # "Custom" by default
        for i, (name, hex_val) in enumerate(COLOR_PRESETS_MAP.items()):
            if hex_val.upper() == current_color:
                default_idx = i + 1  # +1 because "Custom" is at index 0
                break
        
        st.markdown("**Primary Color Theme**")
        selected_preset = st.selectbox(
            "Select a Color Preset:",
            options=preset_options,
            index=default_idx,
            key="color_preset_selector",
            help="Choose a predefined corporate color or select 'Custom' to pick your own."
        )
        
        # Apply the selected preset
        if selected_preset != "Custom (use color picker below)":
            # Extract hex from "Name — #HEX"
            chosen_hex = selected_preset.split(" — ")[1]
            if chosen_hex.upper() != st.session_state.primary_color.upper():
                st.session_state.primary_color = chosen_hex
                st.rerun()
        
        # Show active color swatch + fine-tune picker
        st.markdown(
            f"<div style='background-color:{st.session_state.primary_color}; width:15px; height:15px; border-radius:3px; border:1px solid #CBD5E1; display:inline-block; vertical-align:middle; margin-right:6px;'></div><span style='font-size:12px; font-weight:bold; color:#FFFFFF; vertical-align:middle;'>Active: {st.session_state.primary_color}</span>",
            unsafe_allow_html=True
        )
        
        c_picker = st.color_picker(
            "Fine-tune Accent Color (HEX):",
            value=st.session_state.primary_color
        )
        if c_picker != st.session_state.primary_color:
            st.session_state.primary_color = c_picker
            st.rerun()
 
            
        # Scan logos folder for presets
        logo_dir = "assets/logos"
        default_logos = []
        if os.path.exists(logo_dir):
            default_logos = [f for f in os.listdir(logo_dir) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg'))]
            
        if "rn_logo_preset" not in st.session_state:
            st.session_state.rn_logo_preset = "None (Upload only or blank)"

        if default_logos:
            st.markdown("**Logo Preset Options**")
            logo_options = ["None (Upload only or blank)"] + default_logos
            
            logo_index = 0
            if st.session_state.rn_logo_preset in logo_options:
                logo_index = logo_options.index(st.session_state.rn_logo_preset)

            selected_logo_opt = st.selectbox(
                "Select a default Logo Preset from 'assets/logos/':",
                options=logo_options,
                index=logo_index,
                key="rn_logo_preset_selector",
                help="Add image files inside the 'assets/logos/' directory to populate this list dynamically"
            )
            
            if selected_logo_opt != st.session_state.rn_logo_preset:
                st.session_state.rn_logo_preset = selected_logo_opt
                if selected_logo_opt != "None (Upload only or blank)":
                    st.session_state.rn_logo_temp_path = os.path.join(logo_dir, selected_logo_opt)
                else:
                    st.session_state.rn_logo_temp_path = None
                st.rerun()
                
        # Brand Logo Image uploader
        logo_file = st.file_uploader(
            "Or upload a new Custom Project Brand Logo (PNG, JPEG):",
            type=["png", "jpg", "jpeg"],
            key="rn_logo_uploader"
        )
        
        if logo_file:
            try:
                logo_image = PILImage.open(logo_file)
                logo_temp = "rn_temp_logo.png"
                logo_image.save(logo_temp)
                st.session_state.rn_logo_temp_path = logo_temp
                st.session_state.rn_logo_preset = "Uploaded Logo"
            except Exception as e:
                st.error(f"Error processing upload image logo: {str(e)}")

        # Active Logo Preview & Controls
        if st.session_state.get("rn_logo_temp_path") and os.path.exists(st.session_state.rn_logo_temp_path):
            try:
                active_logo_img = PILImage.open(st.session_state.rn_logo_temp_path)
                st.image(active_logo_img, caption=f"Active Brand Logo: {os.path.basename(st.session_state.rn_logo_temp_path)}", width=150)
                if st.button("🗑️ Clear / Remove Logo", key="btn_clear_rn_logo"):
                    if st.session_state.rn_logo_temp_path == "rn_temp_logo.png" and os.path.exists("rn_temp_logo.png"):
                        try:
                            os.remove("rn_temp_logo.png")
                        except Exception:
                            pass
                    st.session_state.rn_logo_temp_path = None
                    st.session_state.rn_logo_preset = "None (Upload only or blank)"
                    st.rerun()
            except Exception as e:
                st.error(f"Error displaying brand logo preview: {str(e)}")
                
        # Scan covers folder for presets
        cover_dir = "assets/covers"
        default_covers = []
        if os.path.exists(cover_dir):
            default_covers = [f for f in os.listdir(cover_dir) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg'))]
            
        if "rn_cover_preset" not in st.session_state:
            st.session_state.rn_cover_preset = "None (Upload only or blank)"

        if default_covers:
            st.markdown("**Cover Image Preset Options**")
            cover_options = ["None (Upload only or blank)"] + default_covers
            
            cover_index = 0
            if st.session_state.rn_cover_preset in cover_options:
                cover_index = cover_options.index(st.session_state.rn_cover_preset)

            selected_cover_opt = st.selectbox(
                "Select a default Cover Preset from 'assets/covers/':",
                options=cover_options,
                index=cover_index,
                key="rn_cover_preset_selector",
                help="Add image files inside the 'assets/covers/' directory to populate this list dynamically"
            )
            
            if selected_cover_opt != st.session_state.rn_cover_preset:
                st.session_state.rn_cover_preset = selected_cover_opt
                if selected_cover_opt != "None (Upload only or blank)":
                    st.session_state.rn_cover_temp_path = os.path.join(cover_dir, selected_cover_opt)
                else:
                    st.session_state.rn_cover_temp_path = None
                st.rerun()
                
        # Cover Page Hero Image uploader
        st.markdown("**Starting Page Hero / Cover Image**")
        cover_file = st.file_uploader(
            "Or upload a new Custom Starting Page Cover Image (PNG, JPEG):",
            type=["png", "jpg", "jpeg"],
            key="rn_cover_image_uploader"
        )
        
        if cover_file:
            try:
                cover_image = PILImage.open(cover_file)
                cover_temp = "rn_temp_cover.png"
                cover_image.save(cover_temp)
                st.session_state.rn_cover_temp_path = cover_temp
                st.session_state.rn_cover_preset = "Uploaded Cover"
            except Exception as e:
                st.error(f"Error processing cover image: {str(e)}")

        # Active Cover Preview & Controls
        if st.session_state.get("rn_cover_temp_path") and os.path.exists(st.session_state.rn_cover_temp_path):
            try:
                active_cover_img = PILImage.open(st.session_state.rn_cover_temp_path)
                st.image(active_cover_img, caption=f"Active Cover Image: {os.path.basename(st.session_state.rn_cover_temp_path)}", width=180)
                if st.button("🗑️ Clear / Remove Cover Image", key="btn_clear_rn_cover"):
                    if st.session_state.rn_cover_temp_path == "rn_temp_cover.png" and os.path.exists("rn_temp_cover.png"):
                        try:
                            os.remove("rn_temp_cover.png")
                        except Exception:
                            pass
                    st.session_state.rn_cover_temp_path = None
                    st.session_state.rn_cover_preset = "None (Upload only or blank)"
                    st.rerun()
            except Exception as e:
                st.error(f"Error displaying cover image preview: {str(e)}")
 
        # Document Export File Names Configuration (NEW requested configuration)
        st.markdown("**📄 Document Export File Names Base**")
        st.write("Customize default download file name (spaces will automatically be replaced by underscores):")
        fn_rn = st.text_input(
            "Release Notes Filename Base:",
            value=st.session_state.filename_rn,
            placeholder="Release_Notes"
        )
        if fn_rn != st.session_state.filename_rn:
            st.session_state.filename_rn = fn_rn
 
                
        with col_br_right:
            st.markdown("**Release purpose**")
            purpose_in = st.text_area("Release purpose text:", value=st.session_state.release_purpose, height=130)
            if purpose_in != st.session_state.release_purpose:
                st.session_state.release_purpose = purpose_in
            st.markdown("**Additional introduction (optional)**")
            st.write("Optional introductory content printed after the release purpose.")
            
            intro_in = st.text_area(
                "Introduction Content:",
                value=st.session_state.release_notes_intro,
                height=250
            )
            if intro_in != st.session_state.release_notes_intro:
                st.session_state.release_notes_intro = intro_in
                
            with st.expander("👁️ View Live Markdown Preview"):
                st.markdown(st.session_state.release_notes_intro)
                
            st.divider()
            st.markdown("**3. Release Notes Metadata & Resume Configuration**")
            st.write("Configure additional information to display after the welcome message in the Release Notes PDF.")
            
            cr_in = st.text_input(
                "Change Request:",
                value=st.session_state.change_request,
                placeholder="e.g. CR-10294"
            )
            if cr_in != st.session_state.change_request:
                st.session_state.change_request = cr_in
                
            rd_in = st.date_input(
                "Release Date:",
                value=st.session_state.release_date
            )
            if rd_in != st.session_state.release_date:
                st.session_state.release_date = rd_in
                
            sir_in = st.checkbox(
                "Show Items Resume Table",
                value=st.session_state.show_items_resume,
                help="If enabled, shows a table summarizing the counts of each item type included in this release."
            )
            if sir_in != st.session_state.show_items_resume:
                st.session_state.show_items_resume = sir_in
 
# ---------------------------------------------------------
# STEP 4: PDF Exporter
# ---------------------------------------------------------
elif st.session_state.active_tab == "💾 Exporter":
    st.subheader("💾 Branded PDF Exporters & Live Interactive Previews")
    st.write("Inspect base64 rendered preview before downloading the document directly to your system.")
    
    if st.session_state.overview_df is None:
        st.info("⚠️ No data loaded inside workbook. Complete Step 1: Ingestion & Connection first.")
    else:
        col_export_actions, col_preview_area = st.columns([2, 3])
        
        # Prepare filtered data
        rn_ov_df = st.session_state.overview_df[st.session_state.overview_df["Release Notes"] == True] if st.session_state.overview_df is not None else pd.DataFrame()
        
        # Prepare primary release version for metadata summaries
        primary_rel = "Next Releases"
        if 'next_release_df' in st.session_state and not st.session_state.next_release_df.empty:
            primary_rel = str(st.session_state.next_release_df.iloc[0]['Version'])
 
        with col_export_actions:
            st.markdown("### 📥 Document Downloads")
            
            # 2. Release Notes Card
            st.markdown(f"""
            <div class="export-card">
                <h4 style='margin-bottom:6px; color:#FFFFFF;'>📣 Release Notes</h4>
                <p style='font-size:11.5px; line-height:14px;'>Customer-facing release document featuring:
                <ul style='margin-top:2px; margin-bottom:2px; padding-left:15px; font-size:11px;'>
                    <li>Your custom rich welcome intro paragraph.</li>
                    <li><b>Highlights:</b> Consolidated delivered items single table.</li>
                    <li><b>Roadmap:</b> Consolidated next release highlights (Target: {primary_rel}).</li>
                </ul>
                </p>
            </div>
            """, unsafe_allow_html=True)
            
            try:
                rn_pdf = build_release_notes_pdf(rn_ov_df)
                clean_project_name = st.session_state.project_name.replace(' ', '_')
                rn_filename = f"{st.session_state.filename_rn.strip().replace(' ', '_')}_{clean_project_name}.pdf" if st.session_state.filename_rn.strip() != "" else f"Release_Notes_{clean_project_name}.pdf"
                st.download_button(
                    label="⬇️ Download PDF Release Notes",
                    data=rn_pdf,
                    file_name=rn_filename,
                    mime="application/pdf",
                    use_container_width=True
                )
            except Exception as e:
                st.error(f"Error compiling Release Notes PDF: {str(e)}")

            if st.session_state.get("prepared_release_notes") is not None:
                st.markdown("### 📌 Publish to Release History")
                st.info(
                    "After reviewing the PDF, this publishes it to **Release history Documentation**. "
                    "The PDF is attached to that page and its link is added to the **PO acceptance and release notes** column "
                    "of the row matching the prepared release version."
                )
                if st.button("🚀 Publish to Release History", use_container_width=True):
                    try:
                        with st.spinner("Uploading the PDF and updating the Release history row..."):
                            history_pdf = build_release_notes_pdf(rn_ov_df)
                            history_filename = f"Software_Release_Note_MyProject_v{st.session_state.prepared_release_notes['version']}.pdf"
                            history_url = publish_release_note_to_history(
                                st.session_state.prepared_release_notes,
                                history_pdf.getvalue(),
                                history_filename
                            )
                        st.success("Release Note published in the matching Release history row.")
                        st.markdown(f"[Open Release history Documentation]({history_url})")
                    except ValueError as error:
                        st.error(str(error))

        with col_preview_area:
            st.markdown("### 👁️ Live Document Previewer")
            st.write("Release Notes PDF live preview:")
            
            if "pdf_preview_trigger_rn" not in st.session_state:
                st.session_state.pdf_preview_trigger_rn = True
                
            if st.button("🔄 Render / Refresh PDF Preview", key="btn_refresh_pdf_preview_rn", use_container_width=True):
                st.session_state.pdf_preview_trigger_rn = True
                
            if st.session_state.get("pdf_preview_trigger_rn"):
                active_pdf_data = None
                try:
                    active_pdf_data = build_release_notes_pdf(rn_ov_df)
                except Exception as ex:
                    st.error(f"Failed to build PDF preview: {str(ex)}")
                        
                if active_pdf_data is not None:
                    try:
                        base64_pdf = base64.b64encode(active_pdf_data.getvalue()).decode('utf-8')
                        preview_iframe = f'<iframe src="data:application/pdf;base64,{base64_pdf}" width="100%" height="520" type="application/pdf" style="border: 1px solid #3E3E4A; border-radius: 12px; background-color: #ffffff;"></iframe>'
                        st.markdown(preview_iframe, unsafe_allow_html=True)
                    except Exception as e:
                        st.error(f"Failed to render interactive base64 PDF: {str(e)}")
                else:
                    st.info("Ingest data first to view live generated previews.")
            else:
                st.info("💡 PDF rendering is paused. Click the button above to render or update the preview.")

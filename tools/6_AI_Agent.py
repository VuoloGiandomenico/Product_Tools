import streamlit as st
import os
import json
import requests
import pandas as pd
import ollama

from utils.ai_helpers import get_ollama_models

st.set_page_config(page_title="AI Agent", page_icon="💬", layout="wide")

st.title("💬 AI Agent Hub")
st.markdown("Interact with your PO Tools via a conversational agent powered by Ollama. The agent can use internal tools to calculate KPIs, create Jira issues, and more.")

# Ensure session state variables
if "ollama_url" not in st.session_state:
    st.session_state.ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
if "ollama_models" not in st.session_state:
    st.session_state.ollama_models = get_ollama_models(st.session_state.ollama_url)
if "agent_messages" not in st.session_state:
    st.session_state.agent_messages = [
        {
            "role": "system",
            "content": (
                "You are an expert assistant for Product Owners. You have access to a full suite of API tools defined in openapi_copilot.json. "
                "Use these tools to generate issues, create jira tickets, calculate KPIs, generate PDFs and publish them to Confluence. "
                "You can also use the search tools to retrieve information from Jira and Confluence. "
                "CRITICAL: When a user asks you to search or gather information about a topic, you MUST call BOTH the Jira search tool AND the Confluence search tool concurrently to gather comprehensive information from both systems. Do not just search one. "
                "After receiving results from BOTH tools, prepare a single unified report summarizing the findings from both systems, and ALWAYS provide the direct URL links to the source contents. "
                "Always confirm with the user before actually creating a ticket in Jira."
            )
        }
    ]

# Credentials from session state or .env
API_KEY = os.getenv("API_KEY", "")
API_BASE_URL = os.getenv("LOCAL_API_URL", "http://localhost:8000").rstrip("/")

# ==========================================
# TOOL DEFINITIONS (Dynamic OpenAPI Parsing)
# ==========================================

@st.cache_data
def load_openapi_tools():
    # Cache busted to instruct LLM to use plain text keywords instead of JQL/CQL
    try:
        with open("openapi_copilot.json", "r") as f:
            spec = json.load(f)
            
        tools = []
        dispatch_map = {}
        
        for path, methods in spec.get('paths', {}).items():
            for method, details in methods.items():
                operation_id = details.get('operationId')
                summary = details.get('summary', '')
                
                params_schema = {"type": "object", "properties": {}, "required": []}
                req_body = details.get('requestBody', {})
                content = req_body.get('content', {}).get('application/json', {})
                schema_ref = content.get('schema', {}).get('$ref')
                schema_def = {}
                
                if schema_ref:
                    schema_name = schema_ref.split('/')[-1]
                    schema_def = spec.get('components', {}).get('schemas', {}).get(schema_name, {})
                elif 'schema' in content:
                    schema_def = content.get('schema', {})
                    
                if schema_def:
                    properties = schema_def.get('properties', {})
                    clean_props = {}
                    for k, v in properties.items():
                        if '$ref' in v:
                            continue # Skip complex types for simple LLM wrapper
                        if v.get('type') == 'array':
                            v['items'] = {'type': 'string'} # simplify
                        clean_props[k] = v
                        
                    params_schema['properties'] = clean_props
                    params_schema['required'] = schema_def.get('required', [])
                    
                tool_def = {
                    "type": "function",
                    "function": {
                        "name": operation_id,
                        "description": summary,
                        "parameters": params_schema
                    }
                }
                tools.append(tool_def)
                dispatch_map[operation_id] = {
                    "path": path,
                    "method": method
                }
                
        return tools, dispatch_map
    except Exception as e:
        st.error(f"Error loading openapi_copilot.json: {e}")
        return [], {}

TOOLS_SCHEMA, DISPATCH_MAP = load_openapi_tools()

def dispatch_tool_call(func_name: str, func_args: dict) -> str:
    if func_name not in DISPATCH_MAP:
        return f"Error: Tool {func_name} is not available in openapi_copilot.json"
        
    endpoint = DISPATCH_MAP[func_name]
    url = f"{API_BASE_URL}{endpoint['path']}"
    method = endpoint['method'].lower()
    
    headers = {
        "X-API-Key": API_KEY,
        "Content-Type": "application/json"
    }
    
    try:
        if method == "post":
            resp = requests.post(url, headers=headers, json=func_args, timeout=120)
        elif method == "get":
            resp = requests.get(url, headers=headers, params=func_args, timeout=120)
        else:
            return f"Unsupported HTTP method: {method}"
            
        if resp.status_code == 200:
            # Check if it's returning a huge base64 pdf, truncate it for LLM context
            data = resp.json()
            if isinstance(data, dict):
                for key in ["pdf_base64", "pptx_base64"]:
                    if key in data and len(data[key]) > 1000:
                        data[key] = f"[BASE64_DATA_TRUNCATED_FOR_LLM_CONTEXT - Length: {len(data[key])}]"
            return json.dumps(data, indent=2)
        else:
            return f"API Error {resp.status_code}: {resp.text}"
    except Exception as e:
        return f"HTTP Request failed: {str(e)}"

# ==========================================
# UI Layout
# ==========================================

if not st.session_state.ollama_models:
    st.error("🔴 Ollama Not Running or no models available. Please configure it in the Home Hub -> Integrations tab.")
else:
    col_model, _ = st.columns([1, 2])
    with col_model:
        selected_model = st.selectbox(
            "Select AI Model", 
            st.session_state.ollama_models, 
            index=0,
            key="agent_selected_model"
        )
    st.divider()

    def render_tool_output(tool_name, tool_result):
        if tool_name == "calculate_kpis_api_v1_kpis_calculate_post":
            try:
                data = json.loads(tool_result)
                if "error" in data and data["error"]:
                    st.error(data["error"])
                    return
                st.markdown(f"### 📊 Sprint KPIs: {data.get('sprint_name', 'Unknown Sprint')}")
                
                # Extract Metrics
                issues = data.get("issues", [])
                df = pd.DataFrame(issues)
                
                total_sp = df["Story Points"].sum() if not df.empty and "Story Points" in df.columns else 0
                resolved_df = df[df["Resolved"] == True] if not df.empty and "Resolved" in df.columns else pd.DataFrame()
                achieved_sp = resolved_df["Story Points"].sum() if not resolved_df.empty and "Story Points" in resolved_df.columns else 0
                
                m1, m2, m3, m4 = st.columns(4)
                start_dt = str(data.get('sprint_start', ''))[:10]
                end_dt = str(data.get('sprint_end', ''))[:10]
                m1.metric("Sprint Dates", f"{start_dt} to {end_dt}")
                m2.metric("Story Points", f"{achieved_sp} / {total_sp} SP")
                m3.metric("Open Bugs", data.get("global_open_bugs", 0))
                m4.metric("Critical Bugs", data.get("global_critical_bugs", 0))
                
                if not df.empty:
                    st.markdown("#### Sprint Issues")
                    display_cols = [c for c in ["Key", "Type", "Status", "Story Points", "Cycle Time (Days)", "Resolved"] if c in df.columns]
                    st.dataframe(df[display_cols], use_container_width=True)
                    
                    import altair as alt
                    st.markdown("#### Sprint Breakdown")
                    c1, c2 = st.columns(2)
                    with c1:
                        if "Type" in df.columns:
                            type_counts = df["Type"].value_counts().reset_index()
                            type_counts.columns = ["Type", "Count"]
                            chart = alt.Chart(type_counts).mark_arc(innerRadius=40).encode(
                                theta="Count",
                                color="Type",
                                tooltip=["Type", "Count"]
                            ).properties(height=300, title="Issue Types")
                            st.altair_chart(chart, use_container_width=True)
                    with c2:
                        if "Status" in df.columns:
                            status_counts = df["Status"].value_counts().reset_index()
                            status_counts.columns = ["Status", "Count"]
                            chart = alt.Chart(status_counts).mark_bar().encode(
                                x="Count",
                                y=alt.Y("Status", sort="-x"),
                                color=alt.Color("Status", legend=None),
                                tooltip=["Status", "Count"]
                            ).properties(height=300, title="Issue Statuses")
                            st.altair_chart(chart, use_container_width=True)
            except Exception as e:
                st.markdown(f"**Tool Output (Error rendering UI):**\n```json\n{str(tool_result)[:2000]}...\n```")
        else:
            try:
                data = json.loads(tool_result)
                formatted = json.dumps(data, indent=2)
                if len(formatted) > 3000:
                    st.markdown(f"**Tool Output ({tool_name}):**\n```json\n{formatted[:3000]}...\n```")
                else:
                    st.markdown(f"**Tool Output ({tool_name}):**\n```json\n{formatted}\n```")
            except:
                st.markdown(f"**Tool Output ({tool_name}):**\n```\n{str(tool_result)[:3000]}...\n```")

    # Display chat messages
    for message in st.session_state.agent_messages:
        if message["role"] == "system":
            continue
        elif message["role"] == "tool":
            with st.chat_message("tool", avatar="⚙️"):
                render_tool_output(message.get('name', 'tool'), message['content'])
        else:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])

    # Chat input
    if prompt := st.chat_input("Ask the PO Agent... (e.g. 'Calculate KPIs for Sprint 14')"):
        # Display user message in chat message container
        with st.chat_message("user"):
            st.markdown(prompt)
        
        # Add user message to chat history
        st.session_state.agent_messages.append({"role": "user", "content": prompt})

        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                try:
                    # Query Ollama with the current conversation history and tools
                    response = ollama.chat(
                        model=st.session_state.agent_selected_model,
                        messages=st.session_state.agent_messages,
                        tools=TOOLS_SCHEMA,
                    )
                    
                    # 1. First, check if the model wants to call tools
                    if response.message.tool_calls:
                        # Add the model's intermediate tool_call message to history
                        st.session_state.agent_messages.append(response.message)
                        
                        for tool_call in response.message.tool_calls:
                            func_name = tool_call.function.name
                            func_args = tool_call.function.arguments
                            
                            st.write(f"🔧 Invoking tool: **{func_name}**")
                            
                            tool_result = dispatch_tool_call(func_name, func_args)
                                
                            # Append tool result back to the conversation thread
                            tool_msg = {
                                "role": "tool",
                                "name": func_name,
                                "content": tool_result
                            }
                            st.session_state.agent_messages.append(tool_msg)
                            render_tool_output(func_name, tool_result)
                        
                        # 2. Query Ollama again to formulate the final answer using the tool output
                        with st.spinner("Formulating final response..."):
                            final_response = ollama.chat(
                                model=st.session_state.agent_selected_model,
                                messages=st.session_state.agent_messages
                            )
                            st.markdown(final_response.message.content)
                            st.session_state.agent_messages.append(final_response.message)
                    else:
                        # Model replied normally without tools
                        st.markdown(response.message.content)
                        st.session_state.agent_messages.append(response.message)
                        
                except Exception as e:
                    st.error(f"Error interacting with Ollama: {str(e)}")
                    st.session_state.agent_messages.append({"role": "assistant", "content": f"Sorry, I encountered an error: {str(e)}"})

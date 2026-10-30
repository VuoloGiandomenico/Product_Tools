@echo off
:: Always run from the project folder
cd /d "%~dp0"

echo ==========================================================
echo 🎯 Starting Product Owner Suite Hub
echo ==========================================================

:: 1. Create the virtual environment if it does not exist
if not exist .venv (
    echo 📦 Creating isolated virtual environment (.venv)...
    python -m venv .venv
)

:: 2. Activate the virtual environment
echo 🔋 Activating virtual environment...
call .venv\Scripts\activate

:: 3. Upgrade pip and install the pinned dependencies
echo 📥 Installing dependencies from requirements.txt...
python -m pip install --upgrade pip
pip install -r requirements.txt

:: 4. Start the API in the background (local only)
echo ⚙️ Starting API (FastAPI) on http://127.0.0.1:8000...
start /B uvicorn api:app --host 127.0.0.1 --port 8000

:: 5. Start the Streamlit app
echo 🚀 Starting Streamlit server...
streamlit run app.py

:: Stop the API listening on port 8000 when Streamlit exits
echo 🛑 Stopping the API on port 8000...
for /f "tokens=5" %%a in ('netstat -aon ^| find ":8000" ^| find "LISTENING"') do taskkill /F /PID %%a >nul 2>&1

pause

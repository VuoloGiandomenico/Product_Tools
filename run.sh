#!/bin/bash
# Always run from the project folder
cd "$(dirname "$0")"

echo "=========================================================="
echo "🎯 Starting Product Owner Suite Hub"
echo "=========================================================="

# 1. Create the virtual environment if it does not exist
if [ ! -d ".venv" ]; then
    echo "📦 Creating isolated virtual environment (.venv)..."
    python3 -m venv .venv
fi

# 2. Activate the virtual environment
echo "🔋 Activating virtual environment..."
source .venv/bin/activate

# 3. Upgrade pip and install the pinned dependencies
echo "📥 Installing dependencies from requirements.txt..."
python -m pip install --upgrade pip
pip install -r requirements.txt

# 4. Start the API in the background (local only)
echo "⚙️ Starting API (FastAPI) on http://127.0.0.1:8000..."
uvicorn api:app --host 127.0.0.1 --port 8000 &
API_PID=$!

# 5. Start the Streamlit app
echo "🚀 Starting Streamlit server..."
streamlit run app.py

# Stop the API when Streamlit exits
kill $API_PID

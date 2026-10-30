#!/bin/bash
# Resolve the folder that contains this script
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
DESKTOP_DIR="$HOME/Desktop"

echo "============================================="
echo "🖥️  Creating Desktop shortcut..."
echo "============================================="

# 1. Build a native launcher app on the Desktop pointing to this project's absolute path
osacompile -o "$DESKTOP_DIR/PO_Tools.app" -e "do shell script \"$PROJECT_DIR/run.sh > /dev/null 2>&1 &\""

# 2. Apply the custom icon if available
if [ -f "$PROJECT_DIR/assets/app_icon.icns" ]; then
    echo "🎨 Applying app icon..."
    cp "$PROJECT_DIR/assets/app_icon.icns" "$DESKTOP_DIR/PO_Tools.app/Contents/Resources/applet.icns"
    touch "$DESKTOP_DIR/PO_Tools.app"
fi

echo "✅ Done! PO_Tools is now on your Desktop and configured for this machine."

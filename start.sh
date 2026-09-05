#!/usr/bin/env bash
set -e

echo ""
echo "  Stats++ Launcher"
echo "  ================="
echo ""

# Find Python
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        if "$cmd" -c "import sys; exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "  [ERROR] Python 3.10+ not found."
    echo ""
    echo "  Install Python:"
    echo "    macOS:  brew install python3"
    echo "    Ubuntu: sudo apt install python3-full"
    echo "    Other:  https://www.python.org/downloads/"
    echo ""
    exit 1
fi

# Create virtual environment if needed
if [ ! -d ".venv" ]; then
    echo "  Creating virtual environment..."
    "$PYTHON" -m venv .venv
fi

# Install/update the package (editable). This installs Flask (from pyproject)
# AND puts the statsplusplus package (src/ layout) on the path — required, or
# the app fails with "No module named 'statsplusplus'".
echo "  Checking dependencies..."
.venv/bin/pip install -q -e . 2>/dev/null || {
    echo "  Installing dependencies..."
    .venv/bin/pip install -e .
}

# Prune stale files left over from a previous version (manifest-based; no-op in
# dev checkouts that lack MANIFEST.txt). See prune_stale.py.
.venv/bin/python3 prune_stale.py 2>/dev/null || true

echo ""
echo "  Starting Stats++..."
echo "  Open your browser to: http://localhost:5001"
echo ""
echo "  Press Ctrl+C to stop the server."
echo ""

# Open browser after a short delay (background)
(sleep 2 && {
    if command -v xdg-open &>/dev/null; then
        xdg-open "http://localhost:5001" 2>/dev/null
    elif command -v open &>/dev/null; then
        open "http://localhost:5001"
    fi
}) &

# Run the server
.venv/bin/python3 web/app.py

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

# Install/update dependencies
echo "  Checking dependencies..."
.venv/bin/pip install -q -r requirements.txt 2>/dev/null || {
    echo "  Installing dependencies..."
    .venv/bin/pip install -r requirements.txt
}

# Clean up dead files from pre-1.2.0 installs (safe — these are no longer used)
_DEAD_FILES=(
    scripts/league_config.py scripts/league_context.py scripts/log_config.py
    scripts/ratings.py scripts/constants.py scripts/player_utils.py
    scripts/evaluation_engine.py scripts/fv_calc.py scripts/calibrate.py
    scripts/refresh.py scripts/db.py scripts/arb_model.py scripts/war_model.py
    scripts/fv_model.py scripts/data.py
)
_cleaned=0
for f in "${_DEAD_FILES[@]}"; do
    if [ -f "$f" ]; then
        rm "$f"
        _cleaned=$((_cleaned + 1))
    fi
done
if [ $_cleaned -gt 0 ]; then
    echo "  Cleaned up $_cleaned legacy files from previous version."
fi

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

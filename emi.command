#!/bin/bash
# EmiAi Desktop Launcher — start server or open browser if already running
cd "$(dirname "$0")"

if lsof -i :8000 -sTCP:LISTEN >/dev/null 2>&1; then
    if command -v open >/dev/null 2>&1; then
        open "http://localhost:8000"
    else
        xdg-open "http://localhost:8000" 2>/dev/null || echo "Open http://localhost:8000 in your browser."
    fi
    exit 0
fi

python3 start.py &
SERVER_PID=$!

echo "Waiting for EmiAi to start..."
while ! lsof -i :8000 -sTCP:LISTEN >/dev/null 2>&1; do
    sleep 2
done

if command -v open >/dev/null 2>&1; then
    open "http://localhost:8000"
else
    xdg-open "http://localhost:8000" 2>/dev/null || echo "Open http://localhost:8000 in your browser."
fi

wait $SERVER_PID

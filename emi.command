#!/bin/bash
# EmiAi Desktop Launcher — start server or open browser if already running
cd "$(dirname "$0")"

PORT="${EMI_PORT:-8000}"
URL="http://localhost:${PORT}"

if lsof -i :${PORT} -sTCP:LISTEN >/dev/null 2>&1; then
    if command -v open >/dev/null 2>&1; then
        open "${URL}"
    else
        xdg-open "${URL}" 2>/dev/null || echo "Open ${URL} in your browser."
    fi
    exit 0
fi

python3 start.py &
SERVER_PID=$!

echo "Waiting for EmiAi to start on port ${PORT}..."
while ! lsof -i :${PORT} -sTCP:LISTEN >/dev/null 2>&1; do
    sleep 2
done

if command -v open >/dev/null 2>&1; then
    open "${URL}"
else
    xdg-open "${URL}" 2>/dev/null || echo "Open ${URL} in your browser."
fi

wait $SERVER_PID

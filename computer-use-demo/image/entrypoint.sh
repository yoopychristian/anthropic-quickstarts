#!/bin/bash
set -e

./start_all.sh
./novnc_startup.sh

python http_server.py > /tmp/server_logs.txt 2>&1 &

# Start FastAPI server that exposes the agent APIs
PYTHONPATH=. python -m uvicorn computer_use_demo.api.server:app --host 0.0.0.0 --port 9000 > /tmp/api_stdout.log 2>&1 &

echo "✨ Computer Use Demo is ready!"
echo "➡️  Open http://localhost:8080 in your browser to begin"
echo "   API available at http://localhost:9000"

# Keep the container running
tail -f /dev/null

touch /tmp/server_logs.txt /tmp/api_stdout.log /tmp/novnc.log
tail -F /tmp/server_logs.txt /tmp/api_stdout.log /tmp/novnc.log
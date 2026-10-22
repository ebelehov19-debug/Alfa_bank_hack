#!/usr/bin/env bash
set -euo pipefail

Xvfb :99 -screen 0 1365x900x24 -nolisten tcp &
display_pid=$!
vnc_pid=""
web_pid=""
cleanup() {
    kill "$display_pid" ${vnc_pid:+"$vnc_pid"} ${web_pid:+"$web_pid"} 2>/dev/null || true
}
trap cleanup EXIT INT TERM
for attempt in {1..50}; do
    if [ -S /tmp/.X11-unix/X99 ]; then break; fi
    sleep 0.1
done
x11vnc -display :99 -listen 127.0.0.1 -rfbport 5900 -nopw -forever -shared -quiet &
vnc_pid=$!
websockify --web=/usr/share/novnc 6080 127.0.0.1:5900 &
web_pid=$!
echo 'Open http://localhost:6080/vnc.html?autoconnect=true&resize=scale in your desktop browser.'
echo 'Keep this terminal open. Complete the visitor checks, then press Enter here when prompted.'
python -m news_parser.session "$@"

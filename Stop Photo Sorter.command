#!/bin/bash
# Double-click to stop the photo sorter. Safe to run even if it isn't running.
cd "$(dirname "$0")"
PORT="${PORT:-8765}"
PIDS=$(lsof -ti "tcp:$PORT" 2>/dev/null)
if [ -n "$PIDS" ]; then
  kill $PIDS 2>/dev/null
  echo "Photo sorter stopped."
else
  echo "Photo sorter was not running."
fi

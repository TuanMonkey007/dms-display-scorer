#!/bin/bash
cd "$(dirname "$0")"
if [ -d ".venv" ]; then
    .venv/bin/python3 app.py
else
    python3 app.py
fi

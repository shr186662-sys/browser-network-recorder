#!/bin/bash
set -e
cd "$(dirname "$0")"
if [ -n "${NETWORK_RECORDER_PYTHON:-}" ]; then
    PYTHON="$NETWORK_RECORDER_PYTHON"
else
    PYTHON=""
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; assert sys.version_info >= (3, 11); import tkinter' 2>/dev/null; then
            PYTHON="$candidate"
            break
        fi
    done
fi
if [ -z "$PYTHON" ]; then
    echo 'Install Python 3.11+ with Tkinter from https://www.python.org/downloads/macos/'
    read -r -p 'Press Enter to close.'
    exit 1
fi
if ! "$PYTHON" bootstrap.py; then
    echo 'Launch failed. Check Python/Tkinter, Chrome and your network. See README.md.'
    read -r -p 'Press Enter to close.'
    exit 1
fi

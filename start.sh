#!/bin/sh
cd "$(dirname "$0")"
if command -v python3 >/dev/null 2>&1; then python3 -m gqe; else echo "Python 3.10+ is required (https://www.python.org/downloads/)"; fi
echo
echo "Game Query Engine has stopped. Any error above tells you why. Press Enter to close."
read -r _

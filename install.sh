#!/bin/sh
# Install / update the Claude session board (see install.py; also `python install.py` on Windows).
exec python3 "$(dirname "$0")/install.py" "$@"

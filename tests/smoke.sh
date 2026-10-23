#!/bin/sh
# Compile every script, then run the whole test suite (throwaway HOME; never touches your ~/.claude).
set -e
cd "$(dirname "$0")/.."
python3 -m py_compile bin/ct board/*.py install.py
sh -n install.sh
python3 -m unittest discover -s tests -p "test_*.py" "$@"

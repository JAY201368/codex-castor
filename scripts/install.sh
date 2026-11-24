#!/bin/sh
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd) || exit 1
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'Error: python3 is required to install Codex Castor.' >&2
    exit 1
fi
exec python3 "$SCRIPT_DIR/install.py" "$@"

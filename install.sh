#!/bin/sh
set -eu

if ! python3 -c 'import curses' 2>/dev/null; then
    printf '%s\n' 'codemux requires python3 with curses support.' \
        'Use a Python build with curses support; see README.md under Install and run.' >&2
    exit 1
fi

codemux_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
mkdir -p "$HOME/.local/bin"
ln -sfnT "$codemux_dir/codemux" "$HOME/.local/bin/codemux"
printf 'Installed %s\n' "$HOME/.local/bin/codemux"

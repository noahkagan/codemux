#!/bin/sh
set -eu

codemux_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
mkdir -p "$HOME/.local/bin"
ln -sfnT "$codemux_dir/codemux" "$HOME/.local/bin/codemux"
printf 'Installed %s\n' "$HOME/.local/bin/codemux"

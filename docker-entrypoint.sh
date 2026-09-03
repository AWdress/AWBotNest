#!/bin/sh
set -eu

# getopt compatibility mode disables the quoting xvfb-run relies on.
unset GETOPT_COMPATIBLE

if [ "$#" -eq 0 ]; then
    set -- python -m awbotnest.main
fi

exec xvfb-run --auto-servernum --error-file=/dev/stderr \
    --server-args='-screen 0 1920x1080x24 -nolisten tcp' -- "$@"

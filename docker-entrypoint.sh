#!/bin/sh
set -eu
exec xvfb-run -a -s '-screen 0 1920x1080x24 -nolisten tcp' python -m awbotnest.main

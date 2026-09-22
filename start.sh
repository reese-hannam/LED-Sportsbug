#!/usr/bin/env bash
# Start the panel + control center, reachable on the local network.
#
# Works on both the laptop and the Pi without being told which: app/matrix.py
# uses the real rgbmatrix bindings if they're installed and the emulator if
# they aren't. Force it either way with MATRIX_EMULATE=1 / MATRIX_EMULATE=0.
#
# Frees the ports first: a previous run closed by shutting the terminal
# (rather than Ctrl-C) can leave the process alive and holding 8888, which
# shows up as "OSError: [Errno 48] Address already in use".
set -u
cd "$(dirname "$0")"

lsof -ti:8080 -ti:8888 2>/dev/null | xargs kill -9 2>/dev/null
sleep 1

echo "  (Ctrl-C to stop)"

exec .venv/bin/python -m app.main "$@"

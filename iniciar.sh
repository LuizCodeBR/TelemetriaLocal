#!/bin/sh
set -eu
project=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -x "$project/.venv/bin/python" ]; then
    exec "$project/.venv/bin/python" -B "$project/rodar.py" "$@"
fi
if [ -n "${TELEMETRIA_PYTHON:-}" ]; then
    exec "$TELEMETRIA_PYTHON" -B "$project/rodar.py" "$@"
fi
for candidate in python3.12 python3.13 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        exec "$candidate" -B "$project/rodar.py" "$@"
    fi
done
echo 'Instale Python 3.12 ou 3.13 com venv e pip.' >&2
exit 2

#!/bin/sh
# Double-click to open the dashboard (macOS). On Linux, run it from a terminal
# or use the .desktop file next to it.
#
# Everything this does beyond finding a Python lives in lib/serve.py --detach:
# backgrounding, logging, opening the browser, and exiting once the dashboard
# is closed. The Windows launcher is Notices Dashboard.bat and does the same.

here=$(cd "$(dirname "$0")" && pwd)

# A venv if there is one -- mirroring the Windows launcher -- otherwise
# whatever python3 is on PATH. Everything here is standard library, so the
# Python that ships with macOS is enough.
python=
for candidate in "$HOME/.venvs/base/bin/python3" "$(command -v python3)"; do
    if [ -x "$candidate" ]; then
        python=$candidate
        break
    fi
done

if [ -z "$python" ]; then
    message="No python3 found.

Install Python from python.org, or edit this file to point at your install."
    if command -v osascript >/dev/null 2>&1; then
        osascript -e "display alert \"Notices Dashboard\" message \"$message\" as critical" >/dev/null
    else
        echo "$message" >&2
    fi
    exit 1
fi

exec "$python" "$here/lib/serve.py" --detach "$@"

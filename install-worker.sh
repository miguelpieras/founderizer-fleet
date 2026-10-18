#!/bin/sh
# Bootstrap a registered worker from the private shared repository, or this checkout.
set -eu
fleet_source=""
fleet_agent=""
fleet_directory=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --repo) fleet_source="$2"; shift 2 ;;
        --agent) fleet_agent="$2"; shift 2 ;;
        --directory) fleet_directory="$2"; shift 2 ;;
        *) printf '%s\n' 'Usage: install-worker.sh --agent NAME [--repo PRIVATE_REPO --directory PATH]'; exit 1 ;;
    esac
done
[ -n "$fleet_agent" ] || { printf '%s\n' 'Pass --agent with the worker name registered on the hub.'; exit 1; }
command -v git >/dev/null || { printf '%s\n' 'Install git first.'; exit 1; }
command -v python3 >/dev/null || { printf '%s\n' 'Install Python 3.10 or later first.'; exit 1; }
if [ -n "$fleet_source" ]; then
    [ -n "$fleet_directory" ] || fleet_directory="$HOME/fleet-worker-$fleet_agent"
    [ ! -e "$fleet_directory" ] || { printf '%s\n' 'Target directory already exists. Update it with worker_install.py.'; exit 1; }
    git clone -- "$fleet_source" "$fleet_directory"
else
    fleet_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
fi
exec python3 "$fleet_directory/worker_install.py" --agent "$fleet_agent" --start

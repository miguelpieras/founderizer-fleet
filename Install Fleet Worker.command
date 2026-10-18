#!/bin/zsh
set -eu
cd "${0:A:h}"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
read 'fleet_worker_name?Registered Fleet worker name: '
python3 worker_install.py --agent "$fleet_worker_name" --start --authorize-screen
read 'fleet_finish?Press Return to close. '

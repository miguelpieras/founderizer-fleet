#!/bin/sh
# Founderizer join script (owner feature servers). The hub serves it at GET /join with its own address in HUB and REPO. The
# owner runs it with the one line from Founderizer (Servers, Add server); FLEET_JOIN holds the one-time join code.
#
#   A VPS (Ubuntu 22.04 or 24.04, or Debian 12), as root: base packages, the user fleet, then the join, security
#   updates and the firewall. The line has joined the private network (Tailscale) before this script.
#   A Mac (macOS 14 or later, with Tailscale and Homebrew), as the user that will run the agents: tmux and Python
#   with Homebrew, then the join.
#
# The join itself is server.py in a new checkout of the Founderizer repository: the server key, POST /join with the code, and
# the server service. The code goes to server.py on standard input, never in a command argument or a URL. The script
# is safe to run again: each step checks first, and the hub knows the same server again by its address and key.
#
# Then, on Linux, the root helper that makes an agent's OS user and one OS user for each agent this machine already
# has: each agent's session runs as its own user, so its checkout, its runtime, that runtime's credential and its
# vault token are in a home of mode 700 that no other agent can open (PRODUCT.md 4.1). The agents the owner adds
# later are installed under their own users by the server service, which has no root and uses that helper. The last
# step is
# `python3 fleet.py verify` (PRODUCT.md 4.1), whose lines this script prints: a join that half worked says which
# check failed and how to fix it, instead of looking finished. The line ends with a failure when a check failed.
set -eu

HUB="__FLEET_HUB__"
REPO="__FLEET_REPO__"
WORKER=fleet
PACKAGES="git tmux python3 curl ca-certificates ufw unattended-upgrades sudo"
ready=yes       # what the last step, verify, answered about this machine

say() { printf 'Founderizer: %s\n' "$*"; }
stop() { printf 'Founderizer join stopped: %s\n' "$*" >&2; exit 1; }

linux() {
    [ "$(id -u)" = 0 ] || stop "Run the line as root (for example after sudo -i)."
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-} ${VERSION_ID:-}" in
        "ubuntu 22.04"|"ubuntu 24.04"|"debian 12") ;;
        *) stop "This system is ${ID:-unknown} ${VERSION_ID:-}. Use Ubuntu 22.04 or 24.04, or Debian 12." ;;
    esac
    command -v systemctl >/dev/null 2>&1 || stop "systemd is required."
    reach
    missing=""
    for p in $PACKAGES; do
        dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'install ok installed' || missing="$missing $p"
    done
    if [ -n "$missing" ]; then
        say "installing$missing"
        export DEBIAN_FRONTEND=noninteractive
        apt-get -o DPkg::Lock::Timeout=300 update -q </dev/null >/dev/null
        # shellcheck disable=SC2086
        apt-get -o DPkg::Lock::Timeout=300 install -y -q --no-install-recommends $missing </dev/null >/dev/null
    fi
    say "the user $WORKER: locked password, no sudo, its services run after logout"
    id -u "$WORKER" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash --user-group "$WORKER"
    passwd -l "$WORKER" >/dev/null
    chmod 700 "/home/$WORKER"
    for g in sudo admin wheel; do gpasswd -d "$WORKER" "$g" >/dev/null 2>&1 || true; done
    loginctl enable-linger "$WORKER"
    uid=$(id -u "$WORKER")
    i=0
    while [ ! -d "/run/user/$uid" ] && [ "$i" -lt 30 ]; do sleep 1; i=$((i + 1)); done
    ports=$(sshd -T 2>/dev/null | awk '$1 == "port" {print $2}' | sort -u)
    first=$(printf '%s\n' "$ports" | head -n 1)
    checkout "/home/$WORKER/fleet-server" as_worker
    printf '%s\n' "$code" | as_worker python3 "/home/$WORKER/fleet-server/server.py" join --hub "$HUB" --kind linux \
        --ssh-port "${first:-22}"
    updates
    firewall "${ports:-22}"
    current "/home/$WORKER/fleet-server" as_worker
    agent_helper "/home/$WORKER/fleet-server"
    agent_users "/home/$WORKER/fleet-server"
    vault_broker "/home/$WORKER/fleet-server"
    verify_step "/home/$WORKER/fleet-server" as_worker
    say "done. Add an agent to this server in Founderizer: Agents, Add agent."
}

as_worker() {    # the command as the user of the service, from its home folder, with its user services reachable
    # shellcheck disable=SC2016
    runuser -u "$WORKER" -- env HOME="/home/$WORKER" XDG_RUNTIME_DIR="/run/user/$(id -u "$WORKER")" \
        PATH="/home/$WORKER/.local/bin:/usr/local/bin:/usr/bin:/bin" sh -c 'cd "$HOME" && exec "$@"' sh "$@"
}

as_self() { "$@"; }

reach() {
    curl -fsS -m 10 "$HUB/health" >/dev/null 2>&1 || stop "This machine cannot reach the Founderizer hub at $HUB. Check that \
Tailscale is connected, and the Tailscale access rules for the hub's ports 8765 and 9418."
}

checkout() {    # $1: the folder; $2: how to run a command as the user of the service
    if [ -d "$1/.git" ]; then
        say "the Founderizer repository in $1: up to date"
        "$2" git -C "$1" pull -q --ff-only </dev/null || say "$1 could not update; it stays as it is"
    else
        say "the Founderizer repository in $1"
        # On the configuration ref, which is the branch the hub writes and every machine's checkout stands on. A hub
        # from before the split of 2026-10-17 has no such ref, and its checkout starts where it always did.
        "$2" git clone -q -b config "$REPO" "$1" </dev/null \
            || "$2" git clone -q "$REPO" "$1" </dev/null \
            || stop "Could not get the Founderizer repository from $REPO. Check the Tailscale access rules for the hub's \
port 9418."
    fi
}

current() {     # $1: the checkout; $2: how to run a command as the user of the service. The join put this machine in
                # the fleet's team.json, so the checkout takes that commit before anything reads it.
    "$2" git -C "$1" pull -q --ff-only </dev/null || say "$1 could not take the hub's newest commit; it stays as it is"
}

agent_helper() { # $1: the checkout. The two root commands of this machine, and the sudoers file that says who may
                 # run them. The server service installs the agents the owner adds later and has no root of its
                 # own, so this is how an agent gets a user, how `fleet.py verify` reads one, and how a release
                 # that changed vault.py reaches this machine's vault broker, which no checkout holds.
    say "the root helpers that make an agent's OS user and update this machine's vault broker, and the sudoers \
lines for them"
    python3 "$1/hubsetup.py" agent-helper --user "$WORKER" --repo "$REPO" </dev/null \
        || stop "The root helpers of this machine's agent users and its vault broker could not be put in place."
}

agent_users() { # $1: the checkout whose team.json names this fleet's agents. One OS user for each agent of this
                # machine, so that no agent's home is readable by another agent's user. A machine that has no agent
                # yet makes none: each agent gets its own when it is added.
    name=$(as_worker python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("name") or "")' \
        "/home/$WORKER/.fleet/server.json" </dev/null 2>/dev/null || true)
    if [ -z "$name" ]; then
        say "this machine has no name from the hub yet, so no agent user is made"
        return 0
    fi
    python3 "$1/hubsetup.py" agent-users --team "$1/team.json" --machine "$name" </dev/null \
        || stop "The OS users of this machine's agents could not be made."
}

vault_broker() { # $1: the checkout. This machine's vault broker: its own user keeps the key of the store and the key
                 # that opens sealed handovers, so that no agent of this machine can read either and no answer of it
                 # gives a login. It belongs to the machine and not to one agent, so it is installed now, before the
                 # machine has its first agent; the agents the owner adds later reach it through the group of their
                 # OS users. A machine with no broker gets the fleet's own store, which needs no account and no
                 # token of the owner's. A broker that is already there keeps the store it has.
    say "this machine's vault, kept by its own user"
    python3 "$1/hubsetup.py" vault --hub "$HUB" --kit "$1" </dev/null \
        || stop "The vault of this machine could not be set up. Nothing of the owner's was read or changed."
}

verify_step() { # $1: the checkout; $2: how to run a command as the user of the service. The last step: the one health
                # check of the whole product (PRODUCT.md 4.1), in its readable form.
    say "verify: is this machine of the fleet healthy? (selftest.py takes about a minute)"
    if "$2" python3 "$1/fleet.py" verify </dev/null; then
        say "this machine of the fleet is healthy"
    else
        ready=no
        say "this machine is NOT ready: the lines above name each check that failed and the fix it carries"
    fi
}

updates() {     # unattended security updates on, then the pending ones once; never a restart of the server
    printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' \
        > /etc/apt/apt.conf.d/20auto-upgrades
    systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true
    eval "$(apt-config shell AUTO_REBOOT Unattended-Upgrade::Automatic-Reboot/b)"
    if [ "${AUTO_REBOOT:-false}" = true ]; then
        say "security updates are on; this server restarts by itself after updates, so they install in its daily run"
        return 0
    fi
    say "security updates are on; installing the pending ones (this can take some minutes)"
    apt-get -o DPkg::Lock::Timeout=600 -q install </dev/null >/dev/null 2>&1 || true
    unattended-upgrade </dev/null >/dev/null 2>&1 || say "some security updates did not install now; the daily run tries again"
    [ ! -f /var/run/reboot-required ] || say "this server needs a restart for its updates; restart it when it has no work"
}

firewall() {    # $1: the SSH ports. Incoming traffic only to SSH and on the private network. An active firewall keeps
                # its other rules and its defaults.
    active=no
    ufw status 2>/dev/null | grep -q '^Status: active' && active=yes
    if [ "$active" = no ]; then
        ufw default deny incoming >/dev/null
        ufw default allow outgoing >/dev/null
    fi
    for port in $1; do ufw allow "$port/tcp" comment 'fleet ssh' >/dev/null; done
    ufw allow in on tailscale0 comment 'fleet tailscale' >/dev/null
    [ "$active" = yes ] || ufw --force enable >/dev/null
    say "the firewall is on: SSH ($(printf '%s' "$1" | tr '\n' ' ' | sed 's/ $//')) and the private network only"
}

mac() {
    [ "$(id -u)" != 0 ] || stop "Run the line in Terminal as the Mac user that will run the agents, not as root."
    version=$(sw_vers -productVersion)
    [ "${version%%.*}" -ge 14 ] 2>/dev/null || stop "macOS $version is too old. Use macOS 14 or later."
    reach
    brew=""
    for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && { brew=$b; break; }; done
    [ -n "$brew" ] || stop "Homebrew is not installed. Install it from https://brew.sh, then run the line again."
    prefix=$("$brew" --prefix)
    PATH="$HOME/.local/bin:$prefix/bin:$PATH"
    export PATH
    need=""
    command -v tmux >/dev/null 2>&1 || need="$need tmux"
    "$prefix/bin/python3" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null || need="$need python"
    [ -x "$prefix/bin/git" ] || git --version >/dev/null 2>&1 || need="$need git"
    if [ -n "$need" ]; then
        say "installing with Homebrew:$need"
        # shellcheck disable=SC2086
        "$brew" install $need </dev/null || stop "Homebrew could not install$need."
    fi
    checkout "$HOME/fleet-server" as_self
    printf '%s\n' "$code" | "$prefix/bin/python3" "$HOME/fleet-server/server.py" join --hub "$HUB" --kind mac
    current "$HOME/fleet-server" as_self
    # No OS user for each agent on a Mac, and no root helper: a Mac agent's service is a LaunchAgent of a desktop
    # session, so a second user would have to be logged in at the screen. A Mac keeps one user for its agents
    # (PRODUCT.md P2), and its agents share that user's runtime credential.
    verify_step "$HOME/fleet-server" as_self
    say "done. Add an agent to this Mac in Founderizer: Agents, Add agent. Its agents run while this user is logged in."
}

main() {
    [ -n "${FLEET_JOIN:-}" ] || stop "No join code. Copy the whole line from Founderizer: Servers, Add server."
    code=$FLEET_JOIN
    unset FLEET_JOIN
    case "$(uname -s)" in
        Linux) linux ;;
        Darwin) mac ;;
        *) stop "This system is not supported. Use Ubuntu 22.04 or 24.04, Debian 12, or macOS 14 or later." ;;
    esac
    [ "$ready" = yes ] || exit 1
}

# The whole script is read before main runs: a command that reads standard input cannot take the rest of the script.
main

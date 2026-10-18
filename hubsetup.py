#!/usr/bin/env python3
"""The Python half of the Founderizer hub installer (hub.sh): the first team.json, the owner claim and the end checks.
Standard library only, and it reads no credential of a model provider.

    python3 hubsetup.py template --address IP [--port N] [--timezone Z] [--owner IP]
                                                    print the first team.json; write nothing
    python3 hubsetup.py repository --tree PATH --bare PATH --kit PATH
                                                    this hub's own repository: the bare one that every machine of the
                                                    fleet fetches from, and the hub's checkout on the configuration ref
    python3 hubsetup.py claim --address IP --out PATH [--port N] [--timezone Z] [--wait S] [--reclaim] [--commit]
                                                    the owner claim, then write team.json, and commit it on the ref
    python3 hubsetup.py runtime --runtime claude|codex
                                                    install the manager's runtime, so that the owner can sign in to
                                                    their own account from the app (it signs in to nothing itself)
    python3 hubsetup.py agent-users --team PATH --machine NAME [--check]
                                                    one OS user for each agent of this machine, so that no agent's
                                                    user can read another agent's home
    python3 hubsetup.py check --address IP [--port N] [--kit PATH]
                                                    the end checks on this machine: selftest.py and GET /health
    python3 hubsetup.py fleet-id [--db PATH]        this fleet's own id in the hub's key store, made once

Why the claim exists. The owner is a Tailscale address and no token (hub.py `allowed("owner")`), so a hub with the
wrong `addresses.owner` serves the owner's pages to the wrong machine, and one with none serves them to nobody. The
installer therefore proves the address before it finishes, with the same one-time code that join.sh uses, the other
way round: this machine makes the code, the owner's Mac sends it back, and the address of that call is the owner.

The claim listener holds the hub's own address and port, so the Tailscale access rules for port 8765 are proved
before the hub exists. It keeps only the code's SHA-256, never writes the code anywhere, takes one code once, and
stops as soon as it has the address. The script it serves at GET /claim then waits for the hub itself on the same
port and prints the line for the app's Settings, so the final check is made from the owner's own machine.

This program never signs in to Claude, Codex or Grok, never reads or writes a provider credential, and never makes
a vault entry. The customer signs in to their own account in their own session; `"vault": "none"` in the template
says that the manager holds no credential of ours.
"""
import argparse
import hashlib
import http.server
import json
import os
import pathlib
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zoneinfo

import hub
import lifecycle
import onboard

KIT = os.path.dirname(os.path.abspath(__file__))
CODE_LENGTH = 12                # hub.LINK_ALPHABET: 12 characters, 60 bits, as a join code
CLAIM_TRIES = hub.JOIN_TRIES    # wrong codes from one address; then the claim refuses that address
CLAIM_LIFE = hub.JOIN_LIFE      # seconds the claim waits for the owner's Mac
# The manager's start policy, as team.json has it on the pilot hub. The runtime signs in by itself, in the owner's
# own session: no argument of it carries a key, and the installer sets none.
STARTS = {"claude": "claude --permission-mode bypassPermissions --tools default {settings} {prompt}",
          "codex": "codex --no-daemon --sandbox danger-full-access --ask-for-approval never {prompt}"}
# A worker's or a lead's, for the first one of a fleet, where the hub has no other worker to copy from
# (hub.py `server_agent_entry`). It is narrower than the manager's on purpose: a Claude worker asks before it
# edits, so a fleet's first worker is not broader than every worker the owner adds after it. It carries no
# `allow`, so that agent gets Founderizer's own tools and nothing else (fleet.py `settings_arg`). Codex is the same
# line for both, as it is on the pilot's own workers: that runtime has no narrower mode Founderizer uses.
WORKER_STARTS = {"claude": "claude --permission-mode acceptEdits {chrome} {settings} {prompt}",
                 "codex": STARTS["codex"]}
FIRST_COMMIT = "The first team.json of this hub: one manager, and the owner's own Mac"
# One OS user for each agent (PRODUCT.md P2), in this one place: both installers call the command that uses it, so
# no second copy of the name can drift from it. The agent's name comes from the hub's own pattern.
AGENT_USER = "fleet-%s"
DB_PATH = os.path.join("~", ".fleet", "pilot.db")   # the hub's own records, where hub.py --db keeps them
USER_LIMIT = 32                                     # the longest user name this system makes (useradd)
AGENT_GROUP = "fleet-agents"                        # every agent user of this machine, and nobody else
HELPER = "/usr/local/sbin/fleet-agent-user"         # the one root command the server service may run
SUDOERS = "/etc/sudoers.d/fleet-agents"             # what that service may do, and nothing more
EXPORT_OK = "git-daemon-export-ok"                  # git daemon serves a repository only when this file is in it
PRIVATE = re.compile(r"100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.(?:[0-9]{1,3})\.(?:[0-9]{1,3})$")


class Stop(Exception):
    """Something the installer must say and then stop for."""


def say(text):
    print("Founderizer: " + text, flush=True)


def address(value, what):
    """An IPv4 address in plain dotted form, as team.json keeps one."""
    parts = str(value).split(".")
    if len(parts) != 4 or not all(re.fullmatch(r"0|[1-9][0-9]{0,2}", p) and int(p) < 256 for p in parts):
        raise Stop(f"{what} must be an IPv4 address, not {value!r}.")
    return str(value)


def machine_timezone():
    """The server's own time zone when it names one that Python knows, else UTC."""
    for path in ("/etc/timezone",):
        try:
            with open(path, encoding="utf-8") as f:
                name = f.read().strip()
        except OSError:
            continue
        try:
            zoneinfo.ZoneInfo(name)
            return name
        except (ValueError, zoneinfo.ZoneInfoNotFoundError, OSError):
            continue
    link = "/etc/localtime"
    if os.path.islink(link):
        name = os.path.realpath(link).split("/zoneinfo/")[-1]
        try:
            zoneinfo.ZoneInfo(name)
            return name
        except (ValueError, zoneinfo.ZoneInfoNotFoundError, OSError):
            pass
    return "UTC"


def template(hub_ip, port=8765, timezone=None, owner=None, runtime="claude", ssh_user="fleet", ssh_port=22):
    """The first team.json of a new hub: one manager on the hub machine, this machine's address, and the owner's Mac
    when the claim has proved it. Nothing here holds a credential: `"vault": "none"` says that the manager has no
    vault access, because its runtime is the customer's own subscription, signed in by the customer."""
    if runtime not in STARTS:
        raise Stop(f"the manager runs claude or codex, not {runtime!r}.")
    try:
        zoneinfo.ZoneInfo(timezone or "UTC")
    except (ValueError, zoneinfo.ZoneInfoNotFoundError, OSError):
        raise Stop(f"{timezone!r} is not a time zone that this machine knows.") from None
    addresses = {"hub": address(hub_ip, "the hub's address")}
    if owner is not None:
        addresses["owner"] = address(owner, "the owner's address")
    return {
        "hub": {"machine": "hub", "port": int(port)},
        "goals": {"sweep_minutes": 15, "review_hour": 9, "timezone": timezone or "UTC"},
        "addresses": addresses,
        "access": {"hub": {"ssh_user": ssh_user, "ssh_port": int(ssh_port)}},
        "agents": {
            "manager": {
                "role": "manager",
                "machine": "hub",
                "runtime": runtime,
                "can": ["shell", "web"],
                "vault": "none",
                "start": STARTS[runtime],
            },
        },
    }


def install_runtime(runtime):
    """The manager's runtime, from the vendor's own installer at the version Founderizer pins, into ~/.local/bin. It
    installs the program and nothing else: no sign-in, no key, no token. The owner signs in to their own account
    afterwards, from the app (UI-API.md, "Sign a machine in"), and only they can.

    Grok is refused: xAI's terms do not let a third party run it this way."""
    if runtime == "grok":
        raise Stop("Founderizer installs no Grok: xAI's terms do not let a third party run it this way.")
    if runtime not in STARTS:
        raise Stop(f"the manager runs claude or codex, not {runtime!r}.")
    here = lifecycle.exe(runtime)
    if here:
        say(f"{runtime} is installed on this machine ({here}).")
        return here
    pin = onboard.RUNTIME_PINS[runtime]
    say(f"installing {runtime} {pin} from its own installer, into ~/.local/bin.")
    done = subprocess.run(["sh", "-c", 'mkdir -p "$HOME/.local/bin" && ' + onboard.runtime_install(runtime, pin)],
                          capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL)
    here = lifecycle.exe(runtime)
    if done.returncode != 0 or not here:
        raise Stop(f"{runtime} did not install: {(done.stderr or done.stdout or '').strip()[-300:]}")
    if runtime == "claude":         # its updates follow the stable channel, as on every other Founderizer machine
        subprocess.run([sys.executable, "-c", onboard.CLAUDE_CHANNEL, here], capture_output=True, text=True, timeout=120)
    say(f"{runtime} is installed ({here}). Nobody is signed in to it: that is the owner's own step, in the app.")
    return here


def agent_user(agent):
    """The OS user that belongs to one agent: `fleet-<agent>`. Its home is where that agent's own runtime
    credential, vault token and session files go, and no other agent's user can open it."""
    if not hub.AGENT_NAME.fullmatch(str(agent or "")):
        raise Stop(f"{agent!r} is no agent name this fleet can give a user of its own.")
    user = AGENT_USER % agent
    if len(user) > USER_LIMIT:
        raise Stop(f"the agent {agent!r} needs the OS user {user!r}, which is longer than the {USER_LIMIT} characters "
                   f"this system allows. Rename the agent in the app, then run this line again.")
    return user


def agents_of(team, machine):
    """The agents that team.json gives this machine, in the order their users are made. An agent the owner has
    turned off keeps its user: the owner turns it on again from the app, and no install runs in between."""
    return sorted(name for name, conf in ((team or {}).get("agents") or {}).items()
                  if isinstance(conf, dict) and conf.get("machine") == machine)


def user_reading(user):
    """The shell that reads one agent's user, so that a second run of the installer changes nothing."""
    return ("id -u %(u)s >/dev/null 2>&1 && echo exists=yes || echo exists=no\n"
            "passwd -S %(u)s 2>/dev/null | awk '{print \"locked=\"$2}'\n"
            "id -nG %(u)s 2>/dev/null | tr ' ' '\\n' | grep -qxE 'sudo|admin|wheel' && echo sudo=yes || echo sudo=no\n"
            # systemd's own record of lingering is the file; loginctl can only answer for a user it still holds.
            "echo linger=$([ -e /var/lib/systemd/linger/%(u)s ] && echo yes || "
            "loginctl show-user %(u)s -p Linger --value 2>/dev/null || echo unknown)\n"
            "echo mode=$(stat -c %%a /home/%(u)s 2>/dev/null || echo none)\n"
            "echo owner=$(stat -c %%U /home/%(u)s 2>/dev/null || echo none)\n"
            # the group the machine's service user may become, so that it can install and read this agent
            "id -nG %(u)s 2>/dev/null | tr ' ' '\\n' | grep -qx %(g)s && echo group=yes || echo group=no") % {
                "u": user, "g": AGENT_GROUP}


def user_script(user):
    """The shell that makes one agent's user, each line safe to run again: a locked password, no sudo, its services
    kept after logout, and a home of mode 700 that belongs to it. Lingering is the one line that can fail on a
    machine without loginctl, and it is reported instead of stopping the install, because nothing of the agent runs
    as that user until the fleet moves its session there."""
    return ("id -u %(u)s >/dev/null 2>&1 || useradd --create-home --shell /bin/bash --user-group %(u)s\n"
            "passwd -l %(u)s >/dev/null\n"
            "groupadd -f %(g)s\n"
            "usermod -aG %(g)s %(u)s\n"
            "chown %(u)s:%(u)s /home/%(u)s\n"
            "chmod 700 /home/%(u)s\n"
            "for g in sudo admin wheel; do gpasswd -d %(u)s $g >/dev/null 2>&1 || true; done\n"
            "loginctl enable-linger %(u)s 2>/dev/null || echo \"fleet: no lingering for %(u)s\"") % {
                "u": user, "g": AGENT_GROUP}


def _shell(script, timeout=120):
    done = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)
    return done.returncode, (done.stdout or "").strip(), (done.stderr or "").strip()


def kv(text):
    """key=value lines as a dict, as the readings of this installer print them."""
    out = {}
    for line in (text or "").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def agent_users(team, machine, check=False, run=None):
    """One OS user for each agent of this machine, so that one agent's home is not readable by another's user.

    Today a machine runs every agent as the one user of its services, so each agent can read every other agent's
    runtime credential and vault token (README.md, "the vault token"). This makes the user that each agent's own
    files belong to: `fleet-<agent>`, with a locked password (nobody logs in as it), no sudo, lingering on, and a
    home of mode 700 that belongs to it alone.

    The agent's session runs as this user (PRODUCT.md, "What the move to per-agent users decided"): its checkout,
    its runtime, that runtime's credential and its systemd user service are all in this home. The user is also in
    the group `fleet-agents`, which `agent_helper` lets the machine's service user become, because that service
    installs an agent the owner adds later and `fleet.py verify` reads one. Safe to run again: a user that is as
    required is left exactly as it is.

    Returns the list of (agent, user, "made"|"there"|"would make")."""
    run = run or _shell
    agents = agents_of(team, machine)
    if not agents:
        say(f"team.json gives the machine {machine} no agent yet, so there is no user to make. Each agent gets its "
            f"own when it is added.")
        return []
    done = []
    for agent in agents:
        user = agent_user(agent)
        before = kv(run(user_reading(user))[1])
        if (before.get("exists") == "yes" and before.get("locked") == "L" and before.get("sudo") == "no"
                and before.get("mode") == "700" and before.get("owner") == user
                and before.get("group") == "yes"):
            say(f"{user} is the user of the agent {agent} already: locked password, no sudo, its own home")
            done.append((agent, user, "there"))
            continue
        if check:
            say(f"  would make the user {user} for the agent {agent}: locked password, no sudo, lingering on, and "
                f"the home /home/{user} of mode 700 that belongs to it")
            done.append((agent, user, "would make"))
            continue
        code, out, err = run(user_script(user))
        after = kv(run(user_reading(user))[1])
        if code != 0 or after.get("exists") != "yes":
            raise Stop(f"the user {user} of the agent {agent} could not be made: {(err or out).strip()[-300:]}")
        bad = [f"{what} is {after.get(what)!r}" for what, want in (("locked", "L"), ("sudo", "no"),
                                                                   ("mode", "700"), ("owner", user),
                                                                   ("group", "yes"))
               if after.get(what) != want]
        if bad:
            raise Stop(f"the user {user} of the agent {agent} is not as Founderizer needs it: {', '.join(bad)}")
        say(f"{user} is the user of the agent {agent}: locked password, no sudo, and the home /home/{user} that only "
            f"it can open")
        if after.get("linger") != "yes":
            say(f"  {user} has no lingering on this machine, so its own services would not survive a reboot; "
                f"nothing of {agent} runs as it yet")
        done.append((agent, user, "made"))
    return done


def helper_script():
    """The one root command of this machine: `/usr/local/sbin/fleet-agent-user fleet-<agent>` makes that agent's OS
    user and nothing else. Its only input is that one argument, and it is checked against the agent names this
    fleet can give a user of its own before any line of `user_script` runs."""
    name = r"fleet-[a-z][a-z0-9-]{1,%d}" % (USER_LIMIT - len(AGENT_USER % "") - 1)
    return ("#!/bin/sh\n"
            "# Founderizer: make the OS user of one agent, and nothing else. hubsetup.py writes this file.\n"
            "#\n"
            "# It is the one root command that this machine's service user may run (%(sudoers)s), because the\n"
            "# server service installs an agent the owner adds in the app and has no root of its own. The user it\n"
            "# makes has a locked password, no sudo, lingering on, the group %(group)s and a home of mode 700 that\n"
            "# belongs to it alone. Safe to run again: every line reads first.\n"
            "set -eu\n"
            'test "$(id -u)" = 0 || { echo "fleet-agent-user: this command needs root" >&2; exit 1; }\n'
            'test "$#" = 1 || { echo "fleet-agent-user: one argument, which is fleet-<agent>" >&2; exit 2; }\n'
            'printf %%s "$1" | grep -qxE %(name)s || { echo "fleet-agent-user: $1 is no agent user of a fleet" >&2;'
            ' exit 2; }\n'
            "u=$1\n"
            "%(script)s\n"
            'echo "fleet-agent-user: $u is the OS user of that agent"\n') % {
                "sudoers": SUDOERS, "group": AGENT_GROUP, "name": shlex.quote(name), "script": user_script("$u")}


def sudoers_text(service_user):
    """What this machine's service user may do with root, and nothing else: the one helper above as root, and any
    command as a member of `fleet-agents`, which holds the agent users alone. The service installs each agent under
    its own user with the second right, and `fleet.py verify` reads one with it (PRODUCT.md 4.1). That user is no
    agent and runs no AI session on a joined server, so this is not a way from one agent to another."""
    return ("# Founderizer: hubsetup.py writes this file. Two rights for the user that runs this machine's Founderizer\n"
            "# services, and nothing else: the helper that makes one agent's OS user, as root; and any command as\n"
            "# a member of the group %(group)s, which holds the agent users alone. It may not become root.\n"
            "%(user)s ALL=(root) NOPASSWD: %(helper)s\n"
            "%(user)s ALL=(%%%(group)s) NOPASSWD: ALL\n") % {
                "group": AGENT_GROUP, "user": service_user, "helper": HELPER}


def helper_install_script(helper_from, sudoers_from):
    """The shell that puts the group, the root helper and the sudoers file in place. `visudo -c` reads the new file
    before it is moved in, because a file that sudo cannot parse takes sudo away from the whole machine."""
    return ("groupadd -f %(group)s\n"
            "install -o root -g root -m 755 %(helper_from)s %(helper)s\n"
            "visudo -cqf %(sudoers_from)s\n"
            "install -o root -g root -m 440 %(sudoers_from)s %(sudoers)s\n") % {
                "group": AGENT_GROUP, "helper_from": shlex.quote(helper_from), "helper": HELPER,
                "sudoers_from": shlex.quote(sudoers_from), "sudoers": SUDOERS}


def agent_helper(service_user="fleet", check=False, run=None, read=None):
    """The group, the root helper and the sudoers file that the agent-add path and `verify` need on this machine.

    An agent the owner adds later from the app is installed by the server service, which runs as this machine's
    service user without root (server.py), so it cannot make the OS user of the agent it installs. This puts the
    one root command in place that it may run, and the one sudoers file that says what it may do. Both installers
    call it; nothing of it reaches a Mac, which keeps one user for its agents (PRODUCT.md P2).

    Safe to run again, and it changes nothing when both files are already the ones this version writes.
    Returns "made", "there" or "would make"."""
    run = run or _shell
    read = read or (lambda path: pathlib.Path(path).read_text(encoding="utf-8"))
    helper, sudoers = helper_script(), sudoers_text(service_user)
    try:
        there = read(HELPER) == helper and read(SUDOERS) == sudoers
    except OSError:
        there = False
    if there:
        say(f"{HELPER} and {SUDOERS} are in place already: the server service can make one agent's OS user, and "
            f"nothing else with root")
        return "there"
    if check:
        say(f"  would write {HELPER}, the one root command that makes the OS user of one agent, and {SUDOERS}, "
            f"which lets {service_user} run that command and become the group {AGENT_GROUP} and nothing else")
        return "would make"
    folder = tempfile.mkdtemp(prefix="fleet-agent-helper-")
    try:
        one, two = os.path.join(folder, "fleet-agent-user"), os.path.join(folder, "sudoers")
        for path, text in ((one, helper), (two, sudoers)):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        code, out, err = run(helper_install_script(one, two))
        if code != 0:
            raise Stop(f"the root helper of this machine's agents could not be put in place: "
                       f"{(err or out).strip()[-300:]}")
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    say(f"{HELPER} makes the OS user of one agent, and {SUDOERS} lets {service_user} run that one command as root "
        f"and become the group {AGENT_GROUP}: nothing else, and never root")
    return "made"


def keep_fleet_id(db=None):
    """This fleet's own id in the hub's key store: sixteen hex characters, made once and never again.

    It identifies this fleet to the release channel and says nothing about its owner (PRODUCT.md 4.2): it is derived
    from no address, no name and no account. A second id would orphan the fleet's subscription, so the installer
    makes one only when the store has none, and a second run of the line finds the same one.

    **The installer claims no entitlement with it.** A claim key works once, so the claim has exactly one caller and
    that is the app (PRODUCT.md 4.5): it reads this id from the hub, calls the website and posts the token back. Had
    both claimed, the second would be refused and the customer would need a new key."""
    path = os.path.expanduser(db or DB_PATH)
    if not os.path.exists(path):
        raise Stop(f"the hub keeps no records in {path} yet, so this fleet's id cannot be kept there. The hub writes "
                   f"them when it starts: look at systemctl --user status fleet-hub")
    kept, hub.DB = hub.DB, sqlite3.connect(path, timeout=30)    # a store this program opened, never one it found
    hub.DB.row_factory = sqlite3.Row
    try:
        found = hub.kv_get("fleet_id")
        if found:
            say(f"this fleet's id is {found} already, and it is never made a second time.")
            return found
        made = hub.fleet_id()           # it makes one and keeps it in the same store the app reads it from
        say(f"this fleet's id is {made}: it identifies this fleet to us and nothing else — no address, no name, no "
            f"account. The app binds it to a subscription; this installer claims nothing.")
        return made
    except sqlite3.Error as e:
        raise Stop(f"this fleet's id could not be kept in {path}: {e}. The hub writes its records when it starts: "
                   f"look at systemctl --user status fleet-hub") from None
    finally:
        hub.DB.close()
        hub.DB = kept


def write_team(path, team):
    """Write team.json, after hub.validate_team has taken it. Atomic: a run that stops leaves the old file."""
    try:
        hub.validate_team(team)
    except ValueError as e:
        raise Stop(f"the first team.json is not one the hub can run: {e}") from None
    new = path + ".new"
    with open(new, "w", encoding="utf-8") as f:
        json.dump(team, f, indent=2)
        f.write("\n")
    os.replace(new, path)
    return path


def read_team(path):
    """The team.json that is there, or None when it is absent or not a team.json the hub can run."""
    try:
        with open(path, encoding="utf-8") as f:
            team = json.load(f)
        hub.validate_team(team)
    except (OSError, ValueError):
        return None
    return team


def own_team(path, hub_ip, port):
    """The team.json that is there when it is this hub's own - the same hub address and the same port - and else
    None.

    The installer runs from a checkout of the Founderizer repository, and that checkout carries a team.json of its own:
    the fleet it was published from, with that fleet's hub address, machines and agents. A new hub must take none
    of it. It would otherwise start as that fleet: its git daemon listens on the address team.json gives the hub
    (fleet.py `service git`), which this machine does not have, and `agent-users` makes the users of another
    fleet's agents. So the file counts as this hub's own only when it names this machine's address and port, which
    is what a second run of the installer finds and what the first run writes."""
    team = read_team(path)
    if not team:
        return None
    same = ((team.get("addresses") or {}).get("hub") == hub_ip
            and int((team.get("hub") or {}).get("port") or 0) == int(port))
    return team if same else None


def claimed_by(path, hub_ip, port):
    """The owner address of a hub that is set up already, for this address and port; else None. The one check that
    makes a second run of the installer change nothing."""
    owner = ((own_team(path, hub_ip, port) or {}).get("addresses") or {}).get("owner")
    return owner or None


def git(*args, folder=None, identity=False):
    """One git command. identity: a name and an address for a commit, so the machine needs no git settings of its
    own, the way the hub makes its own commits (hub.commit_kit_file).

    `safe.directory`, for this one command and no file: the installer runs as root, and from its second run on, the
    hub's bare repository and its checkout belong to the service user. Without this, git refuses them as dubious
    ownership, and a refusal reads like "there is no checkout here" - so the installer would try to clone over a
    checkout that is there and stop. The repositories are the machine's own and root may read them anyway."""
    who = ["-c", "user.name=Founderizer install", "-c", "user.email=install@fleet.invalid"] if identity else []
    done = subprocess.run(["git", "-c", "safe.directory=*", *(["-C", folder] if folder else []), *who, *args],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return done.returncode, (done.stdout or "").strip(), (done.stderr or "").strip()


def repository(tree, bare, kit):
    """This hub's own repository, from the copy of Founderizer that the installer runs from.

    `bare` is the origin of the whole fleet: the hub serves it read-only over git:// (fleet-git), and every worker and
    joined server fetches its code from it. `kit` is the hub's own checkout, and it stands on the configuration ref,
    because the hub writes the owner's changes to that ref and no other (hub.CONFIG_BRANCH). Both refs start at the
    commit the installer's copy is at, so the code the hub serves is the code it runs.

    Safe to run again: a bare repository or a checkout that is there is left as it is, and the checkout's branch is
    checked either way."""
    code, commit, err = git("rev-parse", "HEAD", folder=tree)
    if code != 0:
        raise Stop(f"{tree} is not a git checkout with a commit, so the hub's repository cannot be made from it: {err}")
    if os.path.isdir(bare):
        say(f"the bare repository is in {bare}")
    else:
        if git("init", "-q", "--bare", bare)[0] != 0:
            raise Stop(f"could not make the bare repository {bare}.")
        code, _, err = git("push", "-q", bare, f"{commit}:refs/heads/{hub.CODE_BRANCH}",
                           f"{commit}:refs/heads/{hub.CONFIG_BRANCH}", folder=tree)
        if code != 0:
            raise Stop(f"could not put the Founderizer code in {bare}: {err}")
        git("symbolic-ref", "HEAD", f"refs/heads/{hub.CODE_BRANCH}", folder=bare)
        say(f"{bare} carries {hub.CODE_BRANCH} and {hub.CONFIG_BRANCH} at {commit[:7]}")
    # `git daemon` serves a repository only when it is marked exported, and fleet.py runs it without
    # --export-all, so every machine of this fleet would be refused its clone over git:// without this one
    # empty file. It is written on a second run too, which is how a hub made before this line is repaired.
    marker = os.path.join(bare, EXPORT_OK)
    if not os.path.exists(marker):
        try:
            pathlib.Path(marker).touch()
        except OSError as e:
            raise Stop(f"could not mark {bare} as served over git://: {e}. Every machine of this fleet fetches "
                       f"its code from there.") from None
        say(f"{bare} is marked as served over git://, which is how every machine of this fleet fetches its code")
    if git("rev-parse", "--git-dir", folder=kit)[0] == 0:
        say(f"the hub's checkout is in {kit}")
    else:
        code, _, err = git("clone", "-q", "-b", hub.CONFIG_BRANCH, bare, kit)
        if code != 0:
            raise Stop(f"could not make the hub's checkout {kit}: {err}")
    on = git("symbolic-ref", "--short", "--quiet", "HEAD", folder=kit)[1]
    if on != hub.CONFIG_BRANCH:
        raise Stop(f"the hub's checkout {kit} is on {on or 'no branch'}, not the configuration ref "
                   f"{hub.CONFIG_BRANCH}. Put it on that ref with: sh {kit}/config-ref.sh {kit}")
    say(f"the hub's checkout stands on {hub.CONFIG_BRANCH}, the ref the hub writes")
    return commit


def commit_team(kit, message):
    """Commit team.json on the configuration ref and push it to this hub's own bare repository. Nothing to commit is
    no failure: that is what a second run of the installer finds."""
    if git("status", "--porcelain", "--", "team.json", folder=kit)[1] == "":
        say("team.json is as it was; nothing to commit")
        return None
    code, _, err = git("commit", "-q", "-m", message, "--", "team.json", folder=kit, identity=True)
    if code != 0:
        raise Stop(f"could not commit team.json in {kit}: {err}")
    code, _, err = git("push", "-q", "origin", f"HEAD:refs/heads/{hub.CONFIG_BRANCH}", folder=kit)
    if code != 0:
        raise Stop(f"could not push team.json to this hub's own repository: {err}")
    commit = git("rev-parse", "HEAD", folder=kit)[1]
    say(f"team.json is committed on {hub.CONFIG_BRANCH} as {commit[:7]} and pushed to the hub's own repository")
    return commit


def new_code():
    """A one-time claim code: 12 characters of hub.LINK_ALPHABET, written as three groups of four."""
    raw = "".join(secrets.choice(hub.LINK_ALPHABET) for _ in range(CODE_LENGTH))
    return raw, "-".join(raw[i:i + 4] for i in range(0, CODE_LENGTH, 4))


def normalise(text):
    """A code as the owner can have pasted it: without spaces or dashes, in capitals."""
    return re.sub(r"[\s-]", "", str(text or "")).upper()


def claim_script(hub_ip, port):
    """The script that GET /claim serves and the owner's Mac runs: it sends the code on standard input, then waits for
    the hub itself on the same port and prints the line for the app's Settings. The code is in the environment of that
    line and goes to curl's standard input, never in an argument and never in a URL."""
    base = f"http://{hub_ip}:{port}"
    return f"""#!/bin/sh
# Claim a new Founderizer hub for this Mac. The hub printed the line that runs this script; FLEET_CLAIM holds the one-time
# code. The code goes to the hub on standard input, never in an argument or a URL.
set -eu
[ -n "${{FLEET_CLAIM:-}}" ] || {{ echo "Founderizer: no claim code. Copy the whole line from the hub's installer." >&2; exit 1; }}
code=$FLEET_CLAIM
unset FLEET_CLAIM
printf '%s' "$code" | curl -fsS -m 20 -X POST --data-binary @- {base}/claim || {{
    echo "Founderizer: this Mac could not claim the hub. Check the code and the Tailscale access rules for port {port}." >&2
    exit 1
}}
unset code
i=0
while [ "$i" -lt 60 ]; do
    if curl -fsS -m 5 -o /dev/null "{base}/ui/state"; then
        echo "Founderizer: this Mac reads the hub. Paste this address in the app, Settings: {base}"
        exit 0
    fi
    sleep 5
    i=$((i + 1))
done
echo "Founderizer: the hub took this Mac as its owner, but it does not answer /ui/state yet. Look at the installer's \
output on the server, then paste {base} in the app, Settings." >&2
exit 1
"""


class ClaimServer(http.server.HTTPServer):
    """One request at a time, on purpose: the claim takes one code once, and handle_request must have written the
    answer before the listener closes, because the owner's script waits for it."""

    def __init__(self, where, hub_ip, port, digest):
        self.hub_ip, self.hub_port, self.digest = hub_ip, port, digest
        self.owner, self.wrong = None, {}
        super().__init__(where, ClaimHandler)


class ClaimHandler(http.server.BaseHTTPRequestHandler):
    """GET /claim gives the owner's script; POST /claim takes the code on the request body. Nothing else is served,
    and the code is never written to the output."""
    server_version = "fleet-hubsetup/1"
    protocol_version = "HTTP/1.0"
    timeout = 20                # a connection that says nothing cannot hold the claim

    def log_message(self, *_args):
        return          # a request line can hold a code: this listener writes none

    def text(self, status, body):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0] != "/claim":
            return self.text(404, "Founderizer: this is the hub installer, and it serves only the claim.\n")
        self.text(200, claim_script(self.server.hub_ip, self.server.hub_port))

    def do_POST(self):
        src = self.client_address[0]
        if self.path.split("?")[0] != "/claim":
            return self.text(404, "Founderizer: this is the hub installer, and it serves only the claim.\n")
        if self.server.owner is not None:
            return self.text(410, "Founderizer: this claim code was used. The hub has its owner.\n")
        if src == self.server.hub_ip:
            return self.text(403, "Founderizer: this address is the hub itself. Claim the hub from the Mac that will own it.\n")
        if self.server.wrong.get(src, 0) >= CLAIM_TRIES:
            return self.text(429, "Founderizer: too many wrong codes from this address.\n")
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = 0
        given = normalise(self.rfile.read(size)[:200].decode("utf-8", "replace")) if 0 < size <= 200 else ""
        if not given or hashlib.sha256(given.encode()).hexdigest() != self.server.digest:
            self.server.wrong[src] = self.server.wrong.get(src, 0) + 1
            return self.text(403, "Founderizer: this is not the claim code. Copy the whole line from the installer again.\n")
        self.server.owner = src
        self.text(200, f"Founderizer: this Mac ({src}) is now the owner of the hub at "
                       f"http://{self.server.hub_ip}:{self.server.hub_port}\n")


def claim(hub_ip, port, wait=CLAIM_LIFE, where=None):
    """Make a one-time code, print the line for the owner's Mac, and serve the claim until that Mac sends the code
    back. Returns the address of the call that sent it. The code is kept only as its SHA-256."""
    raw, code = new_code()
    server = ClaimServer(where or (hub_ip, int(port)), hub_ip, int(port), hashlib.sha256(raw.encode()).hexdigest())
    del raw
    bound = server.hub_port = server.server_address[1]      # in a test the listener takes a free port
    say(f"the owner claim is open on http://{hub_ip}:{bound} for {int(wait / 60)} minutes.")
    print(f"""
    Paste this line in Terminal on the Mac that will own this fleet:

        FLEET_CLAIM={code} sh -c "$(curl -fsS http://{hub_ip}:{bound}/claim)"

    That Mac must be on this fleet's private network (Tailscale). The line proves its address, so the hub serves the
    owner's pages to it and to nothing else. Waiting.
""", flush=True)
    server.timeout = 1
    deadline = time.monotonic() + wait
    while server.owner is None and time.monotonic() < deadline:
        server.handle_request()
    owner = server.owner
    server.server_close()
    if owner is None:
        raise Stop(f"no Mac claimed this hub in {int(wait / 60)} minutes, so nothing was written. Run the installer "
                   f"again; it makes a new code.")
    if not PRIVATE.match(owner):
        say(f"the claim came from {owner}, which is not a Tailscale address (100.64.0.0/10). It is taken as the "
            f"owner, but check that the app reaches the hub over the private network.")
    say(f"the owner of this hub is the Mac at {owner}.")
    return owner


def selftest(kit=KIT):
    """python3 selftest.py in the checkout: its last line, and whether it ended with 0."""
    done = subprocess.run([sys.executable, os.path.join(kit, "selftest.py")], cwd=kit, capture_output=True, text=True)
    lines = [line for line in (done.stdout or "").splitlines() if line.strip()]
    return done.returncode == 0, (lines[-1] if lines else (done.stderr or "").strip()[-300:] or "no output")


def health(hub_ip, port, timeout=10):
    """GET /health of the running hub: (True, the agents it lists) or (False, why not)."""
    request = urllib.request.Request(f"http://{hub_ip}:{port}/health", headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as answer:
            body = json.loads(answer.read() or b"{}")
        return bool(body.get("ok")), ", ".join(body.get("agents") or []) or "no agent"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, str(e)


def cmd_template(args):
    print(json.dumps(template(args.address, args.port, args.timezone or machine_timezone(), args.owner,
                              args.runtime, args.ssh_user, args.ssh_port), indent=2))


def cmd_repository(args):
    repository(args.tree, args.bare, args.kit)


def cmd_claim(args):
    here = address(args.address, "the hub's address")
    already = claimed_by(args.out, here, args.port)
    if already and not args.reclaim:
        say(f"this hub belongs to the Mac at {already} already, so no code was made and nothing changed.")
        if args.commit:
            commit_team(os.path.dirname(os.path.abspath(args.out)), FIRST_COMMIT)
        return
    owner = claim(here, args.port, args.wait)
    team = own_team(args.out, here, args.port) or template(args.address, args.port,
                                                           args.timezone or machine_timezone(),
                                                           runtime=args.runtime, ssh_user=args.ssh_user,
                                                           ssh_port=args.ssh_port)
    team.setdefault("addresses", {})["owner"] = owner
    write_team(args.out, team)
    say(f"{args.out} names {owner} as the owner.")
    if args.commit:
        commit_team(os.path.dirname(os.path.abspath(args.out)), FIRST_COMMIT)


def cmd_runtime(args):
    install_runtime(args.runtime)


def cmd_agent_users(args):
    team = read_team(args.team)
    if team is None:
        raise Stop(f"{args.team} is not a team.json the hub can run, so this machine's agents are not known yet.")
    agent_users(team, args.machine, check=args.check)


def cmd_agent_helper(args):
    agent_helper(service_user=args.user, check=args.check)


def cmd_agent_list(args):
    """One line for each agent of this machine: its name, its runtime and its OS user. The installers read it, so
    it prints nothing else."""
    team = read_team(args.team)
    if team is None:
        raise Stop(f"{args.team} is not a team.json the hub can run, so this machine's agents are not known yet.")
    for agent in agents_of(team, args.machine):
        conf = team["agents"][agent]
        print(f"{agent} {conf.get('runtime') or 'claude'} {agent_user(agent)}")


def cmd_fleet_id(args):
    keep_fleet_id(args.db)


def cmd_check(args):
    ok, last = selftest(args.kit)
    say(f"selftest.py: {last}")
    live, agents = health(address(args.address, "the hub's address"), args.port)
    say(f"the hub answers /health: {agents}" if live else f"the hub does not answer /health: {agents}")
    if not live:
        raise Stop("the hub is not serving. Look at: systemctl --user status fleet-hub")
    if not ok:
        say("selftest.py reports failures. The hub runs; tell the operator what the line above says.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="The first team.json, the owner claim and the end checks of a new hub")
    sub = ap.add_subparsers(dest="what", required=True)
    rt = sub.add_parser("runtime")
    rt.add_argument("--runtime", default="claude", choices=sorted(STARTS))
    rt.set_defaults(fn=cmd_runtime)
    au = sub.add_parser("agent-users")
    au.add_argument("--team", required=True, help="the team.json that names this fleet's agents")
    au.add_argument("--machine", required=True, help="the machine of team.json whose agents get a user here")
    au.add_argument("--check", action="store_true", help="say what it would make, change nothing")
    au.set_defaults(fn=cmd_agent_users)
    ah = sub.add_parser("agent-helper")
    ah.add_argument("--user", default="fleet", help="the user that runs this machine's Founderizer services")
    ah.add_argument("--check", action="store_true", help="say what it would write, change nothing")
    ah.set_defaults(fn=cmd_agent_helper)
    al = sub.add_parser("agent-list")
    al.add_argument("--team", required=True, help="the team.json that names this fleet's agents")
    al.add_argument("--machine", required=True, help="the machine of team.json whose agents are listed")
    al.set_defaults(fn=cmd_agent_list)


    fid = sub.add_parser("fleet-id")
    fid.add_argument("--db", default=DB_PATH, help="the hub's own records; the default is where hub.py keeps them")
    fid.set_defaults(fn=cmd_fleet_id)
    one = sub.add_parser("repository")
    for what, why in (("tree", "the copy of Founderizer that this installer runs from"),
                      ("bare", "the bare repository that every machine of this fleet fetches from"),
                      ("kit", "the hub's own checkout")):
        one.add_argument("--" + what, required=True, help=why)
    one.set_defaults(fn=cmd_repository)
    for name, fn in (("template", cmd_template), ("claim", cmd_claim), ("check", cmd_check)):
        p = sub.add_parser(name)
        p.add_argument("--address", required=True, help="this machine's address on the private network")
        p.add_argument("--port", type=int, default=8765)
        p.set_defaults(fn=fn)
        if name == "check":
            p.add_argument("--kit", default=KIT)
            continue
        p.add_argument("--timezone")
        p.add_argument("--runtime", default="claude", choices=sorted(STARTS))
        p.add_argument("--ssh-user", default="fleet")
        p.add_argument("--ssh-port", type=int, default=22)
        if name == "template":
            p.add_argument("--owner")
        else:
            p.add_argument("--out", required=True, help="where team.json goes")
            p.add_argument("--wait", type=float, default=CLAIM_LIFE)
            p.add_argument("--reclaim", action="store_true", help="a new claim although this hub has an owner")
            p.add_argument("--commit", action="store_true", help="commit team.json on the configuration ref too")
    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except Stop as e:
        print("Founderizer install stopped: " + str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

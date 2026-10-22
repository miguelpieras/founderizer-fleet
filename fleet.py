#!/usr/bin/env python3
"""Founderizer helper. One file for every machine, standard library only.

Setting up
  fleet.py addresses vps=<ip> mac-studio=<ip> owner=<ip>
                               write the machines' Tailscale addresses into team.json
  fleet.py connect <agent>     point this machine's CLI at the team tools
  fleet.py check <agent>       is this machine ready?
  fleet.py verify [--json]     is this machine of the fleet healthy? (PRODUCT.md 4.1)

Running
  fleet.py run <agent>         the stand-in helper: one session per task, hub messages typed in,
                               sign-ins and handovers done outside the agent's session
  fleet.py start manager       start the manager session again after it stopped
  fleet.py git-credential get  git's credential helper in a task session with GitHub repositories (the helper sets it)

On your own machine
  fleet.py watch               a notification when something needs you
  fleet.py say "text"          send the manager a message
  fleet.py board               print the board as text
  fleet.py hub                 print the hub's address
  fleet.py register bws://<secret-id> [--site]
                               give the hub the fingerprint of a login, never the login
                               (--site: also let it sign in to the practice site)

Logins, by hand (agents never run these: they ask the helper with the team tools sign_in and hand_over)
  fleet.py signin <login> --agent <agent>
  fleet.py handover --login <login> --to <agent> --agent <agent>
  fleet.py leakcheck <login> [--agent <agent>]    look for that login where it must never be

A login is named  bws://<secret-id>  (kept in Bitwarden Secrets Manager) or  sealed:<number>  (a handover).
This is a throwaway test kit, not the product.
"""
import argparse
import base64
import contextlib
import fcntl
import glob
import plistlib
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from threads import MARKER, THREAD_ID, CodexFiles, Tailer, Translator, complete_rows, manager_transcript, text_of
from usage import Collector, local_model, timestamp
from lifecycle import (GITHUB_TOKEN, MCP_PROGRAM, REPO_ACCESS, REPO_NAME, RUNTIMES, TOOL_CATALOG, Lifecycle,
                       Updating, app_server_task, exe, follow_config_ref, mcp_variable, no_sign_in, runtime_fact,
                       runtimes_with, tool_catalog, tool_needs)
from desktop import Publisher

KIT = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~/.fleet")
class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an answer, never a second request: the agent key must not go to another place (owner API 1.12)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)   # the hub is never behind a proxy
AGENT = None            # the logical agent of this process: http() sends its key (owner API 1.12)
SESSIONS = os.path.join(HOME, "sessions")       # mode-600 session credential files of this user's windows


class Refused(Exception):
    """Something the helper cannot or will not do. The message is safe to show to an agent."""


def die(msg):
    print(f"fleet: {msg}", file=sys.stderr)
    sys.exit(1)


def load_team():
    path = os.path.join(KIT, "team.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        die(f"cannot read {path}: {e}")


def hub_url(team):
    hub = team.get("hub")
    if isinstance(hub, dict):
        ip = (team.get("addresses") or {}).get(hub.get("machine", "")) or ""
        return f"http://{ip}:{int(hub.get('port') or 8765)}" if ip else ""
    hub = str(hub or "")
    return "" if "CHANGE-ME" in hub else hub


TEAM = load_team()
HUB = (os.environ.get("FLEET_HUB") or hub_url(TEAM)).rstrip("/")


def agent_conf(agent):
    return TEAM["agents"].get(agent) or die(f"no agent '{agent}' in team.json. Agents: {', '.join(TEAM['agents'])}")


def key_file(agent):
    return os.path.join(HOME, f"agent-{agent}.key")


def load_agent_key(agent):
    """This agent's key (owner API 1.12), or None before it has one. Read for each request, so that a new key is used
    at once."""
    try:
        with open(key_file(agent), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def write_private(path, text):
    """A new file that only this user can read (mode 600). Fails when the path exists."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def http(method, path, data=None, timeout=40):
    if not HUB:
        die("no hub address yet. On the hub's machine run: python3 fleet.py addresses ...")
    for _ in range(2):
        key = load_agent_key(AGENT) if AGENT and not path.startswith("/mcp/") else None    # never a session credential
        code, answer = http_once(method, path, data, timeout, key)
        # Owner API 1.12: the hub registers a new key a moment before the helper puts its key file in place. A request
        # that left without a key in that moment is refused once (key_required) and goes again, now with the key.
        if not (code == 403 and isinstance(answer, dict) and answer.get("auth") == "key_required" and not key
                and AGENT and load_agent_key(AGENT)):
            return code, answer
    return code, answer


def http_once(method, path, data, timeout, key):
    body = None
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(HUB + path, data=body, method=method, headers=headers)
    try:
        with OPENER.open(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except ValueError:
            return e.code, {"error": raw.decode("utf-8", "replace")[:200]}
    try:
        return 200, json.loads(raw or b"{}")
    except ValueError:
        return 200, {"text": raw.decode("utf-8", "replace")}


def me(args=None):
    name = (getattr(args, "agent", None) or os.environ.get("FLEET_AGENT") or "").strip()
    if not name:
        die("which agent is this machine? Pass --agent <name>.")
    return name


# ---------------------------------------------------------------- tmux

def tmux(*args, check=False, timeout=15):
    try:
        return subprocess.run(["tmux", *args], capture_output=True, text=True, check=check, timeout=timeout)
    except subprocess.TimeoutExpired:       # tmux can wait for ever on a pane someone is scrolled back in
        return subprocess.CompletedProcess(args, 124, "", "timed out")


def windows(session, *, checked=False):
    r = tmux("list-windows", "-t", session, "-F", "#{window_name}")
    if checked and r.returncode != 0:
        raise Refused(f"cannot read windows in {session}; nothing was started")
    return [w for w in r.stdout.split("\n") if w] if r.returncode == 0 else []


def ensure_session(session):
    """Make this agent's tmux session if it is not there. True when it had to be made."""
    if tmux("has-session", "-t", session).returncode == 0:
        return False
    tmux("new-session", "-d", "-s", session, "-n", "home", "-c", KIT, check=True)
    return True


def launch_words(start):
    """The program and its arguments in a window's start command: without tmux's quotes, the credential prefix
    (owner API 1.12: ". FILE && rm -f FILE && exec "), env and its NAME=value words."""
    words = shlex.split(start)
    if len(words) == 1:
        words = shlex.split(words[0])       # tmux shows a command given as one string inside quotes
    if words[:1] == ["."] and words[2:5] == ["&&", "rm", "-f"] and words[6:8] == ["&&", "exec"]:
        words = words[8:]
    if words[:1] == ["env"]:
        words = words[1:]
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
        words = words[1:]
    return words


def session_file(agent, window, ext, text):
    os.makedirs(SESSIONS, mode=0o700, exist_ok=True)
    path = os.path.join(SESSIONS, f"{agent}.{window}.{secrets.token_hex(8)}.{ext}")
    write_private(path, text)
    return path


def mcp_config_file(agent, window, url, token, servers=None):
    """Claude Code (owner API 1.12): a mode-600 MCP configuration with the session credential in its header. The
    helper removes it after its window has ended. servers: the session's catalog tools (owner feature integrations)."""
    return session_file(agent, window, "json", json.dumps({"mcpServers": dict({"fleet": {
        "type": "http", "url": url, "headers": {"Authorization": f"Bearer {token}"}}}, **(servers or {}))}))


def token_prefix(agent, window, token):
    """Codex and the runners (owner API 1.12): the start of a window command that reads the session credential from a
    mode-600 file into FLEET_SESSION_TOKEN and removes the file before the program starts."""
    return env_prefix(agent, window, {"FLEET_SESSION_TOKEN": token})


def env_prefix(agent, window, values):
    """The start of a window command that reads {NAME: value} from a mode-600 file into the environment and removes the
    file before the program starts; "" without values. No value is in the command itself."""
    if not values:
        return ""
    text = "".join(f"export {name}={shlex.quote(value)}\n" for name, value in values.items())
    path = shlex.quote(session_file(agent, window, "env", text))
    return f". {path} && rm -f {path} && exec "


ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,59}")         # an API key's variable name (owner feature secrets)
# The names that the start of a session sets or that its runtime reads itself; the hub refuses them for an API key.
ENV_RESERVED = re.compile(r"(?:FLEET|BWS|LD|DYLD|ANTHROPIC|CLAUDE|CODEX|XAI)_[A-Z0-9_]*|PATH|HOME|SHELL|USER|LOGNAME|"
                          r"TMPDIR|TERM|PWD|IFS|ENV|BASH_ENV|ZDOTDIR|NODE_OPTIONS|PYTHONPATH|PYTHONHOME")


def session_env(agent, conf, kind):
    """{NAME: value} for the environment of a session of this agent that starts now (kind: task, chat or manager). Each
    session start uses this one hook: the API keys that the owner lets this agent read (read_env), and for a task
    session its GitHub repositories, their credentials and its tools (integration_env, owner feature integrations). A
    tool that needs one of the read keys gets it, so the keys come first."""
    keys = read_env(agent, conf, kind)
    return dict(keys, **(integration_env(agent, conf, keys) if kind == "task" else {}))


def read_env(agent, conf, kind):
    """The API keys that the owner lets this agent read (Secrets: read), for session_env: the
    machine's vault gives the values, the broker in broker mode and this helper's own token in file mode, and the hub
    has their fingerprints before a value comes here, so it masks each one in texts. The agent may read them and never
    prints or stores them. The helper asks only when this checkout's team.json gives the agent a read grant, and the
    hub decides what it gets. A session starts without a value that the vault cannot give now; the helper's log says
    which."""
    try:
        with open(os.path.join(KIT, "team.json"), encoding="utf-8") as f:
            grants = (json.load(f)["agents"].get(agent) or {}).get("secrets") or {}
    except (OSError, ValueError, KeyError, AttributeError):
        grants = (TEAM["agents"].get(agent) or {}).get("secrets") or {}
    if "read" not in grants.values() or vault_mode(agent) == "none":
        return {}
    try:
        done = broker_job_request(agent, {"op": "read"}, timeout=60) if vault_mode(agent) == "broker" else read_values(agent)
    except Exception as e:          # the session starts in every case
        print(time.strftime("%H:%M:%S"), f"{kind}: the session starts without its read variables: "
              f"{e if isinstance(e, Refused) else type(e).__name__}", flush=True)
        return {}
    given = done.get("env") if isinstance(done.get("env"), dict) else {}
    env = {k: v for k, v in given.items() if isinstance(k, str) and ENV_NAME.fullmatch(k) and not ENV_RESERVED.fullmatch(k)
           and isinstance(v, str) and v and "\0" not in v}
    missing = [x for x in done.get("missing") or [] if isinstance(x, str) and ENV_NAME.fullmatch(x)]
    if missing:
        print(time.strftime("%H:%M:%S"), f"{kind}: the vault did not give {', '.join(missing)}", flush=True)
    return env


def codex_env(names):
    """Codex hides the variables whose names have KEY, SECRET or TOKEN from its shell commands. A session with read
    variables lets them through, and it keeps hiding the others of such names: the session credential, and those of
    this helper's environment."""
    if not names:
        return []
    hidden = {"FLEET_SESSION_TOKEN"} | {n for n in os.environ if re.search("KEY|SECRET|TOKEN", n, re.I)}
    return ["-c", "shell_environment_policy.ignore_default_excludes=true",
            "-c", "shell_environment_policy.exclude=" + json.dumps(sorted(hidden - set(names)))]


CODEX_BEARER = "-c " + shlex.quote('mcp_servers.fleet.bearer_token_env_var="FLEET_SESSION_TOKEN"') + " "
# The standing Codex session approves its Fleet tools itself, as `fleet.py connect` does in a machine's configuration,
# so that a manager can switch to Codex on a machine whose Codex was never connected (owner feature runtime_switch).
CODEX_APPROVE = "-c " + shlex.quote('mcp_servers.fleet.default_tools_approval_mode="approve"') + " "
# Codex's terminal UI shows an update menu at its start when a newer version exists, and an unattended session would
# wait there for ever. Codex updates stay the owner's; Fleet's sessions do not ask (Codex 0.160: a top-level option).
CODEX_NO_UPDATE_CHECK = ["-c", "check_for_update_on_startup=false"]


def start_window(session, name, command, options=None):
    """Start a window. options: {window option: value} that tmux sets in the same command as the start, so that the
    window never runs without them (the standing manager window's @fleet_session, its link to its conversation).
    With options, a start that tmux does not answer in time raises too (CalledProcessError 124): it can have started
    or not, and that caller reads which. Task and chat windows go on as before, without that check."""
    then = [a for option, value in (options or {}).items()
            for a in (";", "set-option", "-w", "-t", f"{session}:={name}", option, value)]
    started = tmux("new-window", "-d", "-t", session + ":", "-n", name, "-c", KIT, command, *then, check=True)
    if options and started.returncode == 124:
        raise subprocess.CalledProcessError(124, ["tmux", "new-window"], "", "timed out")
    tmux("set-option", "-w", "-t", f"{session}:={name}", "automatic-rename", "off")
    tmux("set-option", "-w", "-t", f"{session}:={name}", "allow-rename", "off")


CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")      # typed into a terminal, these would act as keys
# A question the session shows on its own screen: numbered choices with one picked out, or with "Yes" first.
CHOICE = re.compile(r"^[^\w\n]*[1-9]\.\s+\S", re.M)
PICKED = re.compile(r"^[^\w\n]*[\u276f\u203a\u25b8\u25b6\u2192>][^\w\n]*[1-9]\.\s+\S", re.M)
YES_FIRST = re.compile(r"^[^\w\n]*1\.\s+Yes\b", re.I | re.M)
FOOTER = re.compile(r"\b(Enter to confirm|Esc to cancel)\b", re.I)     # under Claude Code's own questions
# Claude Code's workspace trust question at the first start in a folder: no numbers, and "No, exit" is picked, so the
# Enter of a typed line would answer it. Both of its choices on the screen make it a question.
TRUST_NO = re.compile(r"^[^\w\n]*(?:[1-9]\.\s+)?No, exit\b", re.I | re.M)
TRUST_YES = re.compile(r"^[^\w\n]*(?:[1-9]\.\s+)?Yes, (?:I trust this folder|proceed)\b", re.I | re.M)
WORKING = re.compile(r"\besc to interrupt\b", re.I)                     # a turn is running
# The helper's new-message signals (Helper.signal). A message is told once, SIGNAL_GAP seconds after the window's last
# signal or start at the soonest. Messages that a session was told of and left unread are told again after a pause
# that doubles from RESIGNAL_FIRST up to RESIGNAL_MAX seconds: every signal is one more model turn over the whole
# context of that session, and until 22 October 2026 it came every 20 seconds for as long as one message waited.
SIGNAL_GAP = 20
RESIGNAL_FIRST = 60
RESIGNAL_MAX = 900
# The standing sessions, which read their messages once more at the end of each turn: nothing is typed into them while
# a turn runs, because a line typed then waits in the runtime's queue and comes as one more turn that reads nothing.
STANDING_WINDOWS = ("manager", "owner-chat")
RETIRE_QUIET = 10           # seconds that the previous runtime's chat must stay quiet before it closes (runtime_switch)
RUNTIME_HELPERS = ("codex-code-mode",)     # a live Codex session's own helper process
PROC = "/proc"                              # Linux process files; macOS has none and uses ps


def process_table(task, generation):
    """[(pid, ppid, args, of_task)] for this user's live processes. of_task: the process environment names this
    task and hub generation. Children inherit it, so it also finds commands that were re-parented."""
    want = (f"FLEET_TASK={task}", f"FLEET_GENERATION={generation}")
    rows, uid = [], os.getuid()
    if os.path.isdir(PROC):
        for name in os.listdir(PROC):
            if not name.isdigit():
                continue
            base = os.path.join(PROC, name)
            try:
                if os.stat(base).st_uid != uid:
                    continue
                with open(base + "/stat", "rb") as f:
                    state, ppid = f.read().decode("utf-8", "replace").rsplit(")", 1)[1].split()[:2]
                with open(base + "/cmdline", "rb") as f:
                    args = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
                with open(base + "/environ", "rb") as f:
                    env = f.read().decode("utf-8", "replace").split("\0")
            except (OSError, ValueError):
                continue
            if state != "Z":
                rows.append((int(name), int(ppid), args, all(w in env for w in want)))
        return rows
    # macOS: with -E, ps shows a process's environment after its arguments (this user's processes only)
    plain = subprocess.run(["ps", "-ww", "-U", str(uid), "-o", "pid=,ppid=,stat=,command="], capture_output=True, text=True)
    with_env = subprocess.run(["ps", "-wwE", "-U", str(uid), "-o", "pid=,command="], capture_output=True, text=True)
    envs = {}
    for line in with_env.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            envs[int(parts[0])] = " " + parts[1] + " "
    for line in plain.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3 or not parts[0].isdigit() or parts[2].startswith("Z"):
            continue
        args, text = (parts[3] if len(parts) > 3 else ""), envs.get(int(parts[0]), "")
        env = text[len(args) + 1:] if text.startswith(" " + args) else ""    # the arguments are not the environment
        rows.append((int(parts[0]), int(parts[1]), args, all(f" {w} " in env for w in want)))
    return rows


def descendants(rows, root):
    children = {}
    for pid, ppid, _, _ in rows:
        children.setdefault(ppid, []).append(pid)
    found, todo = set(), [root]
    while todo:
        for kid in children.get(todo.pop(), []):
            if kid not in found:
                found.add(kid)
                todo.append(kid)
    return found


def process_gone(pid):
    """True when the process has ended. A zombie that waits for its parent counts as ended."""
    if os.path.isdir(PROC):
        try:
            with open(os.path.join(PROC, str(pid), "stat"), "rb") as f:
                return f.read().decode("utf-8", "replace").rsplit(")", 1)[1].split()[0] == "Z"
        except OSError:
            return True
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return not r.stdout.strip() or r.stdout.strip().startswith("Z")


def end_processes(pids, grace=5):
    """SIGTERM, then SIGKILL after a grace time. Returns the PIDs that are still alive."""
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 2)):
        alive = [p for p in pids if not process_gone(p)]
        if not alive:
            return []
        for p in alive:
            try:
                os.kill(p, sig)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and not all(process_gone(p) for p in alive):
            time.sleep(0.2)
    return [p for p in pids if not process_gone(p)]


def shows_question(screen):
    tail = "\n".join(screen.rstrip().split("\n")[-15:])
    if FOOTER.search(tail) or (TRUST_NO.search(tail) and TRUST_YES.search(tail)):
        return True
    return bool((PICKED.search(tail) or YES_FIRST.search(tail)) and len(CHOICE.findall(tail)) >= 2)


def type_into(session, name, text):
    """Type one line into a session. Returns "typed"; "asks" when the session shows a question of its own
    (a stray key there could answer it); "reading" when someone is scrolled back in its window (keys would
    not reach the session); or "gone" when the session is not there."""
    target = f"{session}:={name}"
    r = tmux("display-message", "-p", "-t", target, "#{pane_in_mode}")
    if r.returncode != 0:
        return "gone"
    if r.stdout.strip() not in ("", "0"):
        return "reading"
    r = tmux("capture-pane", "-p", "-t", target)
    if r.returncode != 0:
        return "gone"
    if shows_question(r.stdout):
        return "asks"
    line = " ".join(CONTROL.sub(" ", text).split())
    if line.endswith(";"):
        line += " "             # tmux reads a closing semicolon as its own separator
    for keys in (("-l", "--", line), ("Enter",)):
        if keys == ("Enter",):
            time.sleep(0.6)     # let the session take the text in before the Enter
        r = tmux("send-keys", "-t", target, *keys)
        if r.returncode != 0:
            return "reading" if r.returncode == 124 else "gone"
    return "typed"


# ---------------------------------------------------------------- sessions

# Fleet's deny list for worker and lead Claude sessions.
DENY = ["Read(~/.fleet/**)", "Edit(~/.fleet/**)", "Bash(*.fleet/*)"]
PROTECTED = ("fleet.py", "lifecycle.py", "onboard.py", "vault.py", "acp_runner.py", "screen_runner.py", "desktop.py", "FleetWorker.swift", "worker_install.py", "install-worker.sh", "Install Fleet Worker.command", "hub.py", "wakeups.py", "usage.py", "threads.py", "selftest.py", "test_logins.py", "verify.py", "verify_case.py", "pilot.py", "publish.py", "publish/**", "team.json", "bws.toml", "RULES.md", "CLAUDE.md", "AGENTS.md", "profiles/**")


def settings_arg(agent, conf, task, tools=()):
    """Claude Code settings for one session: what runs without asking, what never runs, and the two hooks
    that carry a worker's permission prompts and activity to the hub. The owner's full-access manager has no
    Fleet-added deny rules and starts with the recorded unattended permission-mode choice. tools: the catalog tools
    of this task session, which the owner gave the agent; they run without asking (owner feature integrations)."""
    if not runtime_fact(conf.get("runtime"), "session_settings"):
        return ""
    if conf.get("role") == "manager":
        settings = {"skipDangerousModePermissionPrompt": True, "permissions": {"allow": [], "deny": []}}
    else:
        deny = DENY + [f"Edit(/{KIT}/{name})" for name in PROTECTED]
        settings = {"permissions": {"allow": conf.get("allow", ["mcp__fleet__*"]) + [f"mcp__{t}__*" for t in tools],
                                    "deny": deny}}
    if conf.get("role") in ("worker", "lead") and task:
        # Command hooks, because Claude Code will not send an HTTP hook to a private address.
        hook = f"{shlex.quote(sys.executable)} {shlex.quote(os.path.join(KIT, 'fleet.py'))} hook"
        settings["hooks"] = {
            "PermissionRequest": [{"hooks": [{"type": "command", "timeout": 280,
                                              "command": f"{hook} permission {agent} {task}"}]}],
            "PostToolUse": [{"hooks": [{"type": "command", "async": True, "timeout": 10,
                                        "command": f"{hook} activity {agent} {task}"}]}],
        }
    return "--settings " + shlex.quote(json.dumps(settings))


WORK = os.path.expanduser("~/fleet-work")      # the agents' GitHub repositories on this machine: WORK/<owner>/<name>


def checkout_conf(agent):
    """The agent's entry in team.json in this checkout now, or {} when it cannot be read. Before a task session the
    helper brings the checkout up to date (Helper.start), so the owner's latest change is there."""
    try:
        with open(os.path.join(KIT, "team.json"), encoding="utf-8") as f:
            found = (json.load(f).get("agents") or {}).get(agent)
    except (OSError, ValueError, AttributeError):
        return {}
    return found if isinstance(found, dict) else {}


def team_catalog():
    """The catalog of this checkout: Fleet's own tools and the owner's MCP servers of team.json (owner feature mcp).
    The helper brings the checkout up to date before a task session starts, so a server that the owner added reaches
    the next session; one that this checkout does not have yet is not loaded, and the start text says so."""
    try:
        with open(os.path.join(KIT, "team.json"), encoding="utf-8") as f:
            return tool_catalog(json.load(f))
    except (OSError, ValueError, TypeError):
        return tool_catalog(TEAM)


def integration_env(agent, conf, keys=None):
    """A task session's GitHub repositories and tools, as the hub gives them at its start (owner feature integrations):
    FLEET_REPOS, GH_TOKEN and git's credentials for them (FLEET_GIT_TOKENS, GIT_CONFIG_*), FLEET_TOOLS with the
    tools' own variables, and FLEET_GITHUB_NOTE for what the session lacks. First the helper fetches each repository
    into WORK, or clones it there. The helper asks the hub only when team.json gives the agent repos or tools; {} for
    an agent without them, and from a hub without the route. keys: the API keys of this session (read_env), which a
    tool of the owner may need; they are already in the session's environment, so they are not given again."""
    mine = checkout_conf(agent)
    if conf.get("role") not in ("worker", "lead") or not (mine.get("repos") or mine.get("tools")):
        return {}
    try:
        code, data = http("POST", f"/helper/integrations/{agent}", {}, timeout=90)
    except (OSError, ValueError):
        code, data = 0, {}
    if code != 200 or not isinstance(data, dict) or not isinstance(data.get("repos"), dict):
        if code == 404:
            return {}
        print(time.strftime("%H:%M:%S"), f"GitHub: the hub gave no repositories or tools ({code})", flush=True)
        return {"FLEET_GITHUB_NOTE": "The hub gave no GitHub access at this start."}
    repos = {r: v for r, v in data["repos"].items() if isinstance(r, str) and REPO_NAME.fullmatch(r) and v in REPO_ACCESS}
    groups = [{"token": t["token"], "repos": [r for r in t["repos"] if isinstance(r, str) and r in repos]}
              for t in data.get("tokens") or [] if isinstance(t, dict) and isinstance(t.get("token"), str)
              and GITHUB_TOKEN.fullmatch(t["token"]) and isinstance(t.get("repos"), list)]
    groups = [g for g in groups if g["repos"]]      # the first is GH_TOKEN's: the hub sends the highest access first
    tokens = {r: g["token"] for g in groups for r in g["repos"]}
    values, notes = {}, [f"No GitHub token for {', '.join(map(str, e.get('repos') or []))}: {e.get('error')}."
                         for e in data.get("errors") or [] if isinstance(e, dict)]
    if repos:       # also without a token: then git for github.com has no credentials, never the machine's own
        values.update(FLEET_REPOS=" ".join(f"{r}:{v}" for r, v in sorted(repos.items())), GIT_TERMINAL_PROMPT="0",
                      FLEET_GIT_TOKENS=json.dumps(tokens, sort_keys=True), **git_credentials())
    if groups:
        values["GH_TOKEN"] = groups[0]["token"]
        others = sorted(set(tokens) - set(groups[0]["repos"]))
        if others:
            notes.append(f"GH_TOKEN (gh and the GitHub tool) works for {', '.join(groups[0]['repos'])}; for "
                         f"{', '.join(others)} use git only.")
    for repo, problem in prepare_repos(repos, values).items():
        notes.append(f"{repo} was not fetched or cloned: {problem}.")
    # The access token of each oauth MCP server that the hub gave for this session (owner feature mcp): it goes into
    # the session's mode-600 env file, as GH_TOKEN does, and it stops when the server's token life ends.
    values.update({k: v for k, v in (data.get("mcp") or {}).items()
                   if isinstance(k, str) and k.startswith("FLEET_MCP_") and isinstance(v, str) and v and "\0" not in v})
    notes += [str(x)[:300] for x in data.get("mcp_problems") or [] if isinstance(x, str)]
    catalog, tools = team_catalog(), []
    for name in (t for t in data.get("tools") or [] if isinstance(t, str)):
        tool = catalog.get(name)
        if not tool:
            notes.append(f"The tool {name} is not loaded: this machine's Fleet does not have it yet.")
            continue
        have = dict(keys or {}, **values)       # the variables that this session has: its API keys and these values
        need = tool_needs(tool)
        if conf.get("runtime") not in tool["runtimes"]:
            notes.append(f"The tool {name} does not run in {conf.get('runtime')}.")
        elif not all(v in have for v in need):
            missing = [v for v in need if v not in have]
            notes.append(f"The tool {name} is not loaded: this session has no {', '.join(missing)}." +
                         ("" if not tool.get("custom") else
                          " The owner connects this MCP server in the app." if tool.get("auth") == "oauth" else
                          " The owner gives the agent that API key on the Secrets page."))
        elif tool.get("command") and not exe(tool["command"]):
            notes.append(f"The tool {name} is not loaded: {tool['command']} is not installed on this machine.")
        else:
            values.update({var: have[source] for var, source in (tool.get("env") or {}).items()})
            tools.append(name)
    if tools:
        values["FLEET_TOOLS"] = " ".join(tools)
    if notes:
        values["FLEET_GITHUB_NOTE"] = " ".join(notes)
        print(time.strftime("%H:%M:%S"), f"GitHub: {values['FLEET_GITHUB_NOTE']}", flush=True)
    return values


def git_credentials():
    """git's settings for a session's GitHub credentials, as GIT_CONFIG_* variables: for github.com only this helper's
    answer (the empty helper first clears the machine's own, such as a keychain), with the repository's path."""
    helper = "!" + shlex.join([sys.executable, os.path.join(KIT, "fleet.py"), "git-credential"])
    pairs = [("credential.https://github.com.helper", ""), ("credential.https://github.com.helper", helper),
             ("credential.https://github.com.useHttpPath", "true")]
    values = {"GIT_CONFIG_COUNT": str(len(pairs))}
    for i, (key, value) in enumerate(pairs):
        values[f"GIT_CONFIG_KEY_{i}"], values[f"GIT_CONFIG_VALUE_{i}"] = key, value
    return values


def prepare_repos(repos, values):
    """Fetch each repository in WORK/<owner>/<name>, or clone a missing one there, with the session's own
    credentials and never a prompt. The working tree stays as the last task left it. {repo: problem} of the failures."""
    problems, env = {}, {**os.environ, **values, "GIT_TERMINAL_PROMPT": "0"}     # values has it too
    for repo in sorted(repos):
        folder = os.path.join(WORK, *repo.split("/"))
        if os.path.isdir(os.path.join(folder, ".git")):
            args, wait = ["git", "-C", folder, "fetch", "--quiet", "--prune", "origin"], 120
        elif os.path.lexists(folder):
            problems[repo] = f"{folder} is there, but it is not a git clone"
            continue
        else:
            os.makedirs(os.path.dirname(folder), exist_ok=True)
            args, wait = ["git", "clone", "--quiet", f"https://github.com/{repo}.git", folder], 300
        try:
            r = subprocess.run(args, env=env, capture_output=True, text=True, timeout=wait, stdin=subprocess.DEVNULL)
            if r.returncode != 0:
                problems[repo] = " ".join((r.stderr or r.stdout).split())[-200:] or f"git ended with {r.returncode}"
        except (OSError, subprocess.TimeoutExpired):
            problems[repo] = "git did not finish in time"
    return problems


def github_text(values, tools):
    """The part of a task's start text for its GitHub repositories and tools (owner feature integrations)."""
    repos = [r.rsplit(":", 1) for r in values.get("FLEET_REPOS", "").split()]
    text = ""
    if repos:
        text += ("Your GitHub repositories are clones on this machine: " +
                 ", ".join(f"{r} ({access}) in ~/fleet-work/{r}" for r, access in repos) +
                 ". Your helper fetched them just before this session; clone a missing one there with git. In each "
                 "repository, work on a new branch from the latest origin default branch and push only that branch. "
                 "For a change, open a pull request (gh pr create) and give its link in your result. Never push to the "
                 "default branch and never merge. ")
    if any(access == "deploy" for _, access in repos):
        text += ("deploy is write and the repository's own deployment: its .github/workflows files, which a write "
                 "token may not push at all, and its GitHub Actions runs (gh workflow, gh run). ")
    if "GH_TOKEN" in values:
        text += ("GH_TOKEN and git's credentials for these repositories stop working one hour after this start. Never "
                 "show them or put them in a note, a file, a command or a commit. ")
    if tools:
        catalog = team_catalog()
        text += ("Your MCP servers besides Fleet's: " + ", ".join(
            f"{name} ({catalog[name]['title']})" if catalog.get(name) else name for name in tools) +
            ". Their tools are mcp__<server>__*. ")
    if values.get("FLEET_GITHUB_NOTE"):
        text += f"In this session: {values['FLEET_GITHUB_NOTE']} If that blocks your task, ask your requester. "
    return "\n\n" + text.strip() if text else ""


def claude_server(name, catalog=None):
    """A catalog tool as a Claude Code MCP server: its variables come from the session's own (${NAME})."""
    tool = (catalog or TOOL_CATALOG)[name]
    if tool.get("url"):
        return dict({"type": "http", "url": tool["url"]},
                    **({"headers": {"Authorization": "Bearer ${%s}" % tool["bearer"]}} if tool.get("bearer") else {}))
    return {"type": "stdio", "command": exe(tool["command"]) or tool["command"], "args": list(tool.get("args") or []),
            "env": {var: "${%s}" % var for var in tool.get("env") or {}}}


def codex_server(name, catalog=None):
    """A catalog tool as Codex -c options: a stdio server gets only its own variables (env_vars), and its tools run
    without asking, as the owner gave them."""
    tool, key = (catalog or TOOL_CATALOG)[name], f"mcp_servers.{name}."
    if tool.get("url"):
        options = [key + "url=" + json.dumps(tool["url"])] + \
            ([key + "bearer_token_env_var=" + json.dumps(tool["bearer"])] if tool.get("bearer") else [])
    else:
        options = [key + "command=" + json.dumps(exe(tool["command"]) or tool["command"]),
                   key + "args=" + json.dumps(list(tool.get("args") or []))] + \
            ([key + "env_vars=" + json.dumps(sorted(tool["env"]))] if tool.get("env") else [])
    return "".join("-c " + shlex.quote(o) + " " for o in options + [key + 'default_tools_approval_mode="approve"'])


def session_command(agent, conf, prompt, task=None, needs="none", attempt=1, generation="", session="", token=None,
                    resume=False, env=None):
    """token: the session credential (owner API 1.12); None starts the session as before, without one. resume: the
    standing session continues its conversation `session` and gets no first prompt: Claude's session (--resume, never
    with a new --session-id) or Codex's thread (codex resume). Everything else is the command of a new start: the same
    settings, MCP configuration and credential. A new Codex session ends its prompt with its session id as a marker,
    by which the helper finds its rollout. env: {NAME: value} of session_env, which the session gets as the
    credential, from a file that its window reads and removes."""
    if resume and (task or conf.get("runtime") not in MANAGER_RUNTIMES or not session):
        raise Refused("only the standing Claude or Codex session continues a conversation")
    values = dict(env or {})
    if token and (runtime_fact(conf.get("runtime"), "token_in_env", True) or needs == "screen"):
        values["FLEET_SESSION_TOKEN"] = token
    prefix = env_prefix(agent, task or "manager", values)
    # The catalog tools that this session loads (owner feature integrations): a runtime whose entry loads servers
    # (lifecycle.RUNTIMES "mcp_servers"), in a terminal only.
    catalog = team_catalog()
    runtime = conf.get("runtime")
    tools = [] if not runtime_fact(runtime, "mcp_servers") or app_server_task(conf, needs) else \
        [t for t in values.get("FLEET_TOOLS", "").split() if t in catalog]
    if task:
        prompt += github_text(values, tools)
    if runtime_fact(runtime, "driver") == "acp":
        # An ACP agent (Grok Build: "grok agent stdio") runs inside acp_runner.py, with the task-bound fleet tools.
        # Its own command comes from team.json, else from its entry (lifecycle.RUNTIMES "acp_command").
        if not task or not generation or not session:
            raise Refused(f"a {runtime_fact(runtime, 'label', runtime)} agent runs managed task sessions only")
        return prefix + shlex.join(["env", f"FLEET_ATTEMPT={attempt}", f"FLEET_GENERATION={generation}", f"FLEET_HUB={HUB}",
                           f"FLEET_AGENT={agent}", f"FLEET_TASK={task}", sys.executable,
                           os.path.join(KIT, "acp_runner.py"), "--agent-command",
                           conf.get("start") or runtime_fact(runtime, "acp_command"),
                           "--runtime", runtime, "--mcp-url", f"{HUB}/mcp/{agent}/{generation}/{task}/{attempt}",
                           "--session", session, "--prompt", prompt])
    if app_server_task(conf, needs):
        if not task or not generation or (needs == "screen" and not conf.get("screen_command")):
            raise Refused("screen tasks need a configured runner and a fenced task" if needs == "screen" else
                          "app-server tasks need a fenced task")
        # FLEET_SESSION names the runner's record file, ~/.fleet/screen-<session>.jsonl, which the helper reads.
        # The screen runtime gets its own model and effort (owner API 1.11), never the primary runtime's. Another
        # task gets the primary runtime's and its needs; the runner's default needs is screen.
        settings = (screen_settings(conf) or {}) if needs == "screen" else conf
        chosen = [x for key in ("model", "effort") if settings.get(key) for x in (f"--{key}", settings[key])]
        if needs != "screen":
            chosen += ["--needs", needs]
            if session:
                prompt += "\n\n" + MARKER.format(session)  # binds the saved primary rollout to this task attempt
        return prefix + shlex.join(["env", f"FLEET_ATTEMPT={attempt}", f"FLEET_GENERATION={generation}",
                           f"FLEET_HUB={HUB}", f"FLEET_AGENT={agent}", f"FLEET_TASK={task}", f"FLEET_SESSION={session}",
                           sys.executable, os.path.join(KIT, "screen_runner.py"), *chosen, "--prompt", prompt])
    if session and not resume and conf.get("runtime") == "codex":
        prompt += "\n\n" + MARKER.format(session)       # lets the helper find this session's rollout
    flags = model_flags(conf)
    # A Claude task session starts idle, with its task text as appended system prompt: interactive Claude Code runs a
    # positional first prompt before it has connected the Fleet MCP server, and its own ToolSearch wait stops after
    # about 5 s. The first turn is the helper's first inbox signal for the unread task event, after the signal
    # throttle. A bounded mitigation, not MCP readiness: a slower server still gives a first turn without the Fleet
    # tools, and the unread task event brings the signal again.
    idle = task and conf.get("runtime") == "claude"
    first = (shlex.quote(session) if conf.get("runtime") == "codex" else "") if resume else \
        ("--append-system-prompt " if idle else "") + shlex.quote(prompt)
    cmd = conf["start"].format(prompt=" ".join(x for x in (flags, first) if x),
                               chrome="--chrome" if needs == "browser" else "",
                               settings=settings_arg(agent, conf, task, tools))
    if conf.get("runtime") == "codex" and "--no-daemon" not in cmd:
        cmd = cmd.replace("codex ", "codex --no-daemon ", 1)
    if conf.get("runtime") == "codex":
        cmd = cmd.replace("codex ", "codex " + shlex.join(CODEX_NO_UPDATE_CHECK + codex_env(env)) + " ", 1)
    # A task's own route; the standing session's route with its credential, and a standing Codex session's also
    # without one: Codex would otherwise use only its machine's configuration, which a runtime switch did not write.
    if task or token or conf.get("runtime") == "codex":
        url = f"{HUB}/mcp/{agent}/{generation}/{task}/{attempt}" if task else f"{HUB}/mcp/{agent}"
        if conf.get("runtime") == "codex":
            cmd = cmd.replace("codex ", "codex -c " + shlex.quote("mcp_servers.fleet.url=" + json.dumps(url)) + " " +
                              (CODEX_BEARER if token else "") + ("" if task else CODEX_APPROVE) +
                              "".join(codex_server(t, catalog) for t in tools), 1)
        else:
            servers = {t: claude_server(t, catalog) for t in tools}
            config = mcp_config_file(agent, task or "manager", url, token, servers) if token else \
                json.dumps({"mcpServers": dict({"fleet": {"type": "http", "url": url}}, **servers)})
            # Only this configuration: the standing session not the folder's fleet server without a credential, and a
            # task session not the machine's own MCP servers, only Fleet's and its catalog tools (owner feature
            # integrations). --chrome adds its browser server to this configuration.
            cmd = cmd.replace("claude ", "claude --strict-mcp-config --mcp-config " + shlex.quote(config) + " ", 1)
    if session and conf.get("runtime") == "claude":
        cmd = cmd.replace("claude ", f"claude {'--resume' if resume else '--session-id'} {session} ", 1)
    if resume and conf.get("runtime") == "codex":
        cmd = cmd.replace("codex ", "codex resume ", 1)     # last, so that it comes first: codex resume [options] <thread>
    env = f"FLEET_ATTEMPT={attempt} FLEET_GENERATION={shlex.quote(generation)} FLEET_HUB={shlex.quote(HUB)} FLEET_AGENT={shlex.quote(agent)} FLEET_TASK={shlex.quote(task or '')}"
    if not task and conf.get("runtime") == "claude":
        env += f" CLAUDE_CODE_AUTO_COMPACT_WINDOW={STANDING_COMPACT}"
    return f"{prefix}env {env} {cmd}"


# The runtimes whose standing session continues its own conversation (lifecycle.RUNTIMES "manager").
MANAGER_RUNTIMES = tuple(sorted(runtimes_with("manager")))
# Tokens of context at which a standing Claude session (the manager's, an owner chat) compacts its conversation. A
# model with a native 1M window compacts at about 967K by default, and every turn of a session reads its whole context
# again: a standing session gathers weeks of unrelated turns, so it paid for all of them at each new message. The
# fleet's state is in the hub (goals, plans, duties, tasks), never only in a session, so a summary loses nothing that
# a read cannot give back. CLAUDE_CODE_AUTO_COMPACT_WINDOW wins over the owner's own /autocompact and settings.
STANDING_COMPACT = 200000
# A standing Claude session that is told of a message after the provider's cache of it has expired reads its whole
# context again at the price of writing it. Claude keeps a session's cache for an hour (five minutes on some plans,
# which its own records say), and the manager's session waited for hours between messages, so each of those messages
# cost the whole of a long context: 3 to 4 percent of a week's allowance each by the manager's own count on 22 October
# 2026, while a new session costs only its start and the hub's brief. So a message that finds the manager's session cold, with a context that
# has grown FRESH_GROWN past the start of its conversation, starts a new conversation instead (Helper.restart_fresh).
CACHE_SECONDS = 3600
FRESH_GROWN = 50000             # tokens
FRESH_SPACING = 600             # seconds between two new conversations at the least, whatever the readings say
CONTEXT_BYTES = 1 << 20         # read of the head and of the tail of a conversation file, which can be hundreds of MB


def call_context(line):
    """(tokens, time, cache seconds or None) of one line of a Claude conversation file that is a model call of the
    main conversation: the tokens that the call read, new, from the cache and written to it, are its context. None
    for any other line, for a subagent's call and for a call that read nothing (a message of Claude Code's own)."""
    if b'"assistant"' not in line:
        return None
    try:
        row = json.loads(line)
    except ValueError:
        return None             # also the line cut at the edge of a read, and one still being written
    message = row.get("message") if isinstance(row, dict) and row.get("type") == "assistant" and \
        not row.get("isSidechain") else None
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return None
    tokens = sum(v for v in (usage.get(k) for k in ("input_tokens", "cache_read_input_tokens",
                                                     "cache_creation_input_tokens")) if isinstance(v, int))
    try:
        at = timestamp(row.get("timestamp"))
    except ValueError:
        return None
    if not tokens or at is None:
        return None
    written = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    return tokens, at, 3600 if written.get("ephemeral_1h_input_tokens") else \
        300 if written.get("ephemeral_5m_input_tokens") else None


def claude_context(path):
    """(tokens now, tokens at its start, time of its last model call, cache seconds) of a Claude conversation file,
    from its first and its last model call (call_context); None when either is not found. Only the head and the tail
    of the file are read. A compaction stays in the same file, so the last call is the context after it. The cache
    seconds are those of the newest call that wrote to the cache, else CACHE_SECONDS."""
    try:
        with open(path, "rb") as stream:
            head = stream.read(CONTEXT_BYTES)
            stream.seek(max(0, os.fstat(stream.fileno()).st_size - CONTEXT_BYTES))
            tail = stream.read(CONTEXT_BYTES)
    except OSError:
        return None
    first = next(filter(None, map(call_context, head.splitlines())), None)
    calls = [c for c in map(call_context, tail.splitlines()) if c]
    if not first or not calls:
        return None
    return calls[-1][0], first[0], calls[-1][1], next((c[2] for c in reversed(calls) if c[2]), CACHE_SECONDS)


# Claude Code puts RULES.md in the context through CLAUDE.md, so a session that also read it was given its
# 17,000 tokens twice; a Codex session reads it. The first 60 characters find a session from before session ids
# (manager_windows), so they stay as they were.
MANAGER_START = ('You are the manager in Founderizer. Read RULES.md and profiles/{agent}.md in this folder '
                 'first, but RULES.md only if your context does not hold it yet: Claude Code loads it through '
                 'CLAUDE.md. {resume}On every new-message signal call read_messages; its read is the receipt. Messages that start with [hub] come from the hub and '
                 'messages that start with [owner] come from the owner: act on each one. Before you give work, choose by suitability, '
                 'then availability, with see_capacity. Then wait for the first message.')
# The tail that this prompt had before the hub sent a brief: a cold start, and see_team is over a hundred lines of
# everything. It is what a start without the hub's answer still says.
MANAGER_COLD = "Then call read_messages and see_team. "


def manager_resume(agent):
    """The hub's brief of where the fleet stood, for the first prompt of the standing session (hub.manager_resume_text):
    the goals and their next step, the duties not in order, the open tasks with the why they were given for, the
    results waiting to be checked, the reports waiting to be decided, what is with the owner, and the manager's own
    last decisions. It is cheaper than the see_team it replaces, and nothing in it is only in that session. A hub that
    cannot be reached or an older hub gives none, and the start says what it always said."""
    try:
        code, data = http("GET", f"/events/{agent}?after=-1&wait=0", timeout=10)
    except (urllib.error.URLError, OSError, Refused, ValueError):
        return MANAGER_COLD
    said = (data or {}).get("resume") if code == 200 else None
    return f"{said.strip()}\n\n" if isinstance(said, str) and said.strip() else MANAGER_COLD


def chat_recap(agent):
    """The hub's own record of what was said in this agent's owner chat (hub.chat_recap_text), for a chat that starts
    without its conversation: a first chat, or the first chat on the runtime the owner just switched to, because the
    record is per runtime (chat_resume). Its own first line says that it is the hub's record and not the session's
    memory of the work. "" when nothing was said yet, and "" from a hub that cannot be reached or an older hub: then
    the chat starts as it did before."""
    try:
        code, data = http("GET", f"/events/{agent}?after=-1&wait=0", timeout=10)
    except (urllib.error.URLError, OSError, Refused, ValueError):
        return ""
    said = (data or {}).get("chat_recap") if code == 200 else None
    return said.strip() if isinstance(said, str) and said.strip() else ""


def screen_settings(conf):
    """{"model", "effort"} of the agent's screen runtime (owner API 1.11; None means its default), or None for an
    agent without one. They are apart from the primary runtime's model and effort, which never reach it."""
    if not conf.get("screen_command"):
        return None
    screen = conf.get("screen") or {}
    return {"model": screen.get("model"), "effort": screen.get("effort")}


def model_flags(conf):
    """The requested model and effort for a new session, as the runtime's own flags (owner API 1.9); "" for the
    runtime's defaults. Screen tasks use the screen settings instead (screen_settings)."""
    flags = runtime_fact(conf.get("runtime"), "model_flags") or {}
    return shlex.join([word.format(conf[key]) for key in ("model", "effort") if conf.get(key)
                       for word in flags.get(key, ())])


CHAT_SIGNAL = "New fleet messages. Call read_messages now."     # the helper's signal to an owner chat window
# Characters of a chat's quoted first prompt. A tmux command must stay under 16 KiB (start_window; tmux refuses a
# longer one with "command too long"), and a Codex chat has nowhere else to put its prompt. The instructions and the
# profile are what the chat is, so the recap is what gives way.
CHAT_PROMPT_MAX = 14000


def chat_fit(recap, room):
    """The hub's record of a chat (chat_recap) cut to `room` quoted characters: its oldest messages go first, and its
    first line, which says what the recap is, stays with the newest message. "" when not even those two fit, and then
    the chat starts with no recap rather than not starting at all; one message that long is the unread one, which
    read_messages brings anyway."""
    note, _, said = recap.partition("\n")
    lines = said.splitlines()
    while lines and len(shlex.quote("\n\n" + note + "\n" + "\n".join(lines))) > room:
        lines.pop(0)
    return note + "\n" + "\n".join(lines) if lines else ""


def chat_command(agent, conf, generation, token=None, marker=None, resume=None, recap=None, env=None):
    """A separate discussion session: no task tools, login jobs, browser or write sandbox. token: its session
    credential (owner API 1.12). For Codex: marker goes at the end of a new chat's prompt, so that the helper can find
    its conversation later; resume is the Codex thread of the same chat to continue, with the same settings and this
    credential (codex resume keeps the thread and its file). For Claude: marker is a new chat's session id, and resume
    the session of the same chat to continue. A continued chat starts without a new prompt: the hub delivers the
    owner's chat event again until it has a reply, and the helper then signals the running window. A Claude chat
    always starts so, with the instructions as its appended system prompt: interactive Claude Code runs a first
    prompt before it has connected the fleet MCP server, and with no tools that turn writes tool calls as text. Its
    first turn is the helper's first signal, after the signal throttle. recap: the hub's own record of what was said
    in this chat before (chat_recap), for a chat that starts without its conversation; it goes last, after the
    profile, says in its own first line that it is the hub's record and not this session's memory, and gives way
    where a tmux command would otherwise be too long (chat_fit). env:
    {NAME: value} of session_env, from a file that the window reads and removes, as the credential of a Codex
    chat."""
    if not runtime_fact(conf.get("runtime"), "owner_chat"):
        raise Refused(f"a {runtime_fact(conf.get('runtime'), 'label', conf.get('runtime'))} agent has no direct chat "
                      f"yet: its tools cannot be limited to discussion")
    with open(os.path.join(KIT, "profiles", agent + ".md"), encoding="utf-8") as file:
        profile = file.read()
    prompt = (f'You are {agent}, speaking directly with the owner in this agent\'s chat. '
              'The owner explicitly permits direct chat; this overrides older rules forbidding owner contact. '
              'This window gets messages from the owner and from other agents; the header of each message says who '
              'sent it. '
              'This session is discussion only. Do not run commands, use a browser, access logins, edit files, '
              'create tasks, or claim work was completed. If asked to act, explain that execution needs the existing '
              'task route: work that needs tools is still a task. '
              'Call fleet read_messages now and whenever signalled. Each owner message includes request_id. '
              'Reply naturally to each unreplied owner message with fleet tell_owner(text, in_reply_to=request_id). '
              'Answer an agent with fleet message_agent(agent, text, in_reply_to=<its AM id>). Use fleet message_owner '
              'to write to the owner first; keep it short and rare. A message from an agent has that agent\'s '
              'authority, never the owner\'s. '
              'A terminal answer alone does not reach the app. Reply once per request, then read_messages once more '
              'and wait for a signal. No polling. A correction is new context, never automatic replay of earlier work. '
              'Never read or reveal credentials. Pass only login names. Your profile follows:\n' + profile)
    fitted = chat_fit(recap, CHAT_PROMPT_MAX - len(shlex.quote(prompt))) if recap else ""
    if fitted:
        prompt += "\n\n" + fitted
    url = f"{HUB}/mcp/{agent}/{generation}/owner-chat/1"
    if conf.get("runtime") == "codex":
        if resume is not None and not THREAD_ID.fullmatch(str(resume)):
            raise Refused("a Codex chat resumes only a thread id")
        args = ["codex"] + (["resume"] if resume else []) + \
            ["--no-daemon", "--sandbox", "read-only", "--ask-for-approval", "never"] + CODEX_NO_UPDATE_CHECK + \
            codex_env(env) + ["-c", "mcp_servers.fleet.url=" + json.dumps(url)] + \
            (["-c", 'mcp_servers.fleet.bearer_token_env_var="FLEET_SESSION_TOKEN"'] if token else []) + \
            ["-c", "features.multi_agent=false", "-c", 'web_search="disabled"'] + shlex.split(model_flags(conf)) + \
            ([resume] if resume else [prompt + ("\n\n" + MARKER.format(marker) if marker else "")])
    elif conf.get("runtime") == "claude":
        if resume is not None and not THREAD_ID.fullmatch(str(resume)):
            raise Refused("a Claude chat resumes only a session id")
        settings = {"permissions": {"allow": ["mcp__fleet__read_messages", "mcp__fleet__tell_owner",
                                              "mcp__fleet__message_owner", "mcp__fleet__message_agent",
                                              "mcp__fleet__see_responsibilities",
                                              "mcp__fleet__recall"], "deny": DENY}}
        args = ["claude"] + (["--resume", resume] if resume else ["--session-id", marker] if marker else []) + \
            ["--permission-mode", "dontAsk", "--tools", "", "--strict-mcp-config",
             "--mcp-config", mcp_config_file(agent, "owner-chat", url, token) if token else
             json.dumps({"mcpServers": {"fleet": {"type": "http", "url": url}}}),
             "--settings", json.dumps(settings), "--append-system-prompt", prompt] + shlex.split(model_flags(conf))
    else:
        raise Refused("direct chat is not configured for this runtime")
    values = dict(env or {}, **({"FLEET_SESSION_TOKEN": token}
                                if token and runtime_fact(conf.get("runtime"), "token_in_env", True) else {}))
    words = ["env", f"FLEET_GENERATION={generation}", f"FLEET_HUB={HUB}", f"FLEET_AGENT={agent}", "FLEET_TASK="] + \
        ([f"CLAUDE_CODE_AUTO_COMPACT_WINDOW={STANDING_COMPACT}"] if conf.get("runtime") == "claude" else [])
    return env_prefix(agent, "owner-chat", values) + shlex.join(words + args)


CONVERSATION_TOOLS = ("read_messages", "reply", "ask_owner", "request_work")    # MCP context conversation (CHAT-APPS 9.4)
CONVERSATION_WINDOW = re.compile(r"C-[1-9][0-9]{0,11}")     # the tmux window of a conversation: C-<id>
CONVERSATION_IDLE = 1200        # seconds a conversation window stays open after its last signal (CHAT-APPS.md 7.2)
CONVERSATION_WINDOWS = 2        # conversation windows of one agent at the same time; the others wait in the hub
CONVERSATION_SIGNAL = "A new message in this conversation. Call read_messages now."
CHATS = os.path.expanduser("~/fleet-chats")     # the empty working folders of conversation sessions: CHATS/<id>
CONVERSATION_ID = re.compile(r"[1-9][0-9]{0,11}")
# The one built-in tool that a conversation session can have besides the Fleet tools: Claude Code 2.1.301 adds it when
# a remote feature flag is on, and neither --tools, --disallowedTools nor a deny rule takes it away. It reaches no file,
# command or site. Its second call ends the conversation: the window stays and shows "Claude has ended this chat.".
CLAUDE_BUILT_IN = "EndConversation"


def conversation_folder(conversation):
    """The working folder of a conversation session, CHATS/<id> (CHAT-APPS.md 7.2), made mode 700 when it is not there.
    Refused when it is not an empty folder of this user: nothing in it may reach the session."""
    folder = os.path.join(CHATS, str(conversation))
    os.makedirs(folder, mode=0o700, exist_ok=True)
    info = os.lstat(folder)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or os.listdir(folder):
        raise Refused(f"the conversation folder {folder} is not an empty folder of this user")
    return folder


def conversation_command(agent, conf, generation, conversation, url, token, prompt):
    """The start of a locked conversation session with another person than the owner (CHAT-APPS.md 7.2): only the Fleet
    tools of url, the conversation's MCP address, with token, its credential of kind conversation. The hub decides
    which of CONVERSATION_TOOLS the person's level gets. prompt: the hub's whole prompt for this person, from a mode-600
    file (a tmux command must stay under 16 KiB). No hook, browser, catalog tool, API key or GitHub token, and no
    user, project or local settings, CLAUDE.md, memory, skill or plugin: test_conversation_lockdown proves the offered
    tools (the Fleet tools, and CLAUDE_BUILT_IN when its flag is on) and the prompt against stand-ins. The window starts
    in conversation_folder; Claude Code asks its workspace trust question there unless it trusts CHATS. A runtime
    whose entry does not carry "conversation" is refused with the reason that entry gives (lifecycle.RUNTIMES): its
    tools cannot be limited yet."""
    if not runtime_fact(conf.get("runtime"), "conversation"):
        raise Refused(runtime_fact(conf.get("runtime"), "conversation_why") or
                      "conversations are only for Claude agents: the tools of this runtime cannot be limited")
    if not CONVERSATION_ID.fullmatch(str(conversation)):
        raise Refused("a conversation id is a number")
    address = urllib.parse.urlsplit(url or "")
    if address.scheme not in ("http", "https") or address.path != f"/mcp/{agent}/{generation}/conversation/{conversation}":
        raise Refused("the MCP address is not this conversation's")
    if not token or not prompt:
        raise Refused("a conversation session needs its credential and its prompt")
    folder, window = conversation_folder(conversation), f"C-{conversation}"
    settings = {"autoMemoryEnabled": False,
                "permissions": {"allow": [f"mcp__fleet__{t}" for t in CONVERSATION_TOOLS], "deny": DENY}}
    # --setting-sources "": no user, project or local settings, so no CLAUDE.md, hook or plugin of this machine, and
    # --settings still applies. --safe-mode would also drop the --mcp-config server, so the session had no tools.
    args = ["claude", "--setting-sources", "", "--permission-mode", "dontAsk", "--tools", "", "--strict-mcp-config",
            "--mcp-config", mcp_config_file(agent, window, url, token), "--settings", json.dumps(settings),
            "--append-system-prompt-file", session_file(agent, window, "md", prompt)] + shlex.split(model_flags(conf))
    words = ["env", f"FLEET_GENERATION={generation}", f"FLEET_HUB={HUB}", f"FLEET_AGENT={agent}", "FLEET_TASK="]
    return f"cd {shlex.quote(folder)} && exec " + shlex.join(words + args)


def chat_record_file(agent, runtime="codex", kind="chat"):
    """The owner chat's binding to its conversation, one for each runtime (owner feature runtime_switch): Codex keeps
    the file that it had before, Claude has its own, so that a switch back continues that runtime's conversation.
    kind "manager": the same binding of the standing manager session (manager_start)."""
    return os.path.join(HOME, f"chat-{agent}.json" if (kind, runtime) == ("chat", "codex") else f"{kind}-{agent}.{runtime}.json")


def load_chat_record(agent, runtime="codex", kind="chat"):
    """This agent's owner chat on one runtime as the helper started it: {generation, marker, session, started}; None
    when there is none. Refused when it is there but cannot be read: then nothing may take its place."""
    path, what = chat_record_file(agent, runtime, kind), "owner chat" if kind == "chat" else "manager session"
    if not os.path.lexists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        raise Refused(f"the {what} record {path} cannot be read") from None
    if not isinstance(record, dict) or not isinstance(record.get("generation"), str):
        raise Refused(f"the {what} record {path} is not valid")
    return record


def save_chat_record(agent, record, runtime="codex", kind="chat"):
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    new = f"{chat_record_file(agent, runtime, kind)}.{secrets.token_hex(4)}.new"
    write_private(new, json.dumps(record) + "\n")
    os.replace(new, chat_record_file(agent, runtime, kind))


def claude_chat_file(session):
    """The conversation file of a Claude owner chat in this checkout's own project folder, or None without one."""
    root = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    folder = os.path.join(root, "projects", re.sub(r"[^A-Za-z0-9-]", "-", os.path.realpath(KIT)))
    path = os.path.join(folder, f"{session}.jsonl")
    return path if THREAD_ID.fullmatch(str(session)) and os.path.isfile(path) and not os.path.islink(path) else None


def codex_threads(thread=None):
    """The rollout files of this checkout's Codex conversations: all, or the one of a thread id."""
    root = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    name = f"rollout-*-{thread}.jsonl" if thread else "rollout-*.jsonl"
    return [p for p in glob.glob(os.path.join(root, "sessions", "*", "*", "*", name)) if not os.path.islink(p)]


def chat_thread(record):
    """The Codex thread of a recorded owner chat whose conversation file is still there, or None: the recorded
    thread, or the conversation whose first lines carry the chat's marker (it can be quiet for longer than two days)."""
    thread = record.get("session")
    if thread:
        return thread if THREAD_ID.fullmatch(str(thread)) and codex_threads(thread) else None
    marker = record.get("marker")
    if not marker:
        return None
    path = CodexFiles(os.path.expanduser("~"), KIT).find(marker, since=float(record.get("started") or 0) - 600)
    found = THREAD_ID.search(os.path.basename(path or "")[-len(".jsonl") - 36:])
    return found.group(0) if path and found else None


def cmd_chat_session(args):
    """The owner's one-time step for an owner chat that started before the helper recorded chats: record its
    conversation (a Codex thread, or a Claude session), so that the chat's next normal start, also after a runtime
    switch back, continues it with a session credential. The conversation must be this agent's owner chat in this
    checkout; no window is touched here. --runtime names the runtime of a chat that still runs the previous runtime
    after a switch (owner feature runtime_switch); its record is that runtime's own."""
    agent, conf = args.agent, agent_conf(args.agent)
    runtime = getattr(args, "runtime", None) or conf.get("runtime")
    if runtime not in MANAGER_RUNTIMES:
        die("only a Codex or Claude owner chat is continued this way")
    if not THREAD_ID.fullmatch(args.session):
        die("give the conversation id (a UUID) of the owner chat")
    if runtime == "codex":
        files = codex_threads(args.session)
        if len(files) != 1:
            die(f"no single conversation file has thread {args.session}")
        with open(files[0], "rb") as stream:
            first, head = stream.readline(1 << 20), stream.read(256 * 1024)
        try:
            meta = (json.loads(first).get("payload") or {})
        except ValueError:
            meta = {}
        if os.path.realpath(str(meta.get("cwd"))) != os.path.realpath(KIT):
            die("that conversation did not run in this checkout")
    else:                       # a Claude conversation file lives in the project folder of the checkout where it ran
        path = claude_chat_file(args.session)
        if not path:
            die(f"this checkout has no Claude conversation {args.session}")
        with open(path, "rb") as stream:
            first, head = b"", stream.read(256 * 1024)
    if f"You are {agent}, speaking directly with the owner in this agent's chat".encode() not in first + head:
        die(f"that conversation is not the owner chat of {agent}")
    code, data = http("GET", f"/events/{agent}?after=-1&wait=0", timeout=10)
    if code != 200 or not data.get("db"):
        die(f"the hub gave no generation ({code})")
    save_chat_record(agent, {"generation": data["db"], "marker": None, "session": args.session, "started": time.time()},
                     runtime)
    print(f"recorded: the next owner chat of {agent} in generation {data['db']} continues conversation {args.session}")


def cmd_hook(args):
    """Run by Claude Code as a hook: pass what it sends to the hub and print the hub's answer.
    Whatever goes wrong, stay silent: then Claude Code simply shows its own question."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    try:
        context = {"attempt": os.environ.get("FLEET_ATTEMPT"), "generation": os.environ.get("FLEET_GENERATION")}
        if args.kind == "activity":
            http("POST", f"/hook/activity/{args.agent}/{args.task}", dict(context, tool_name=payload.get("tool_name")), timeout=8)
            return
        code, data = http("POST", f"/hook/permission/{args.agent}/{args.task}",
                          dict(context, tool_name=payload.get("tool_name"), tool_input=payload.get("tool_input")), timeout=270)
        if code == 200 and data.get("hookSpecificOutput"):
            print(json.dumps(data))
    except (urllib.error.URLError, OSError, ValueError):
        pass


# ---------------------------------------------------------------- the helper

def make_key(agent, rotate=False):
    """This agent's key (owner API 1.12): 256 random bits, base64url, in ~/.fleet/agent-<agent>.key (mode 600). Made
    once, or again with rotate. Returns only its SHA-256."""
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    key = None if rotate else load_agent_key(agent)
    if key is None:
        new = f"{key_file(agent)}.{secrets.token_hex(4)}.new"
        write_private(new, secrets.token_urlsafe(32) + "\n")
        os.replace(new, key_file(agent))
        key = load_agent_key(agent)
    return hashlib.sha256(key.encode()).hexdigest()


def cmd_key(args):
    """Print only the SHA-256 of this agent's key, made here if it has none (or a new one with --rotate). The owner
    registers it with POST /ui/team/agents/<agent>/key; an agent alone on its address registers its own."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", args.agent):
        die("an agent name has 2 to 31 lowercase letters, digits and -")
    print(make_key(args.agent, rotate=args.rotate))


def enroll_key(agent):
    """Owner API 1.12: the first time, make this agent's key and register its SHA-256 with the hub. The hub takes it
    only while the agent is alone on its address; on a shared machine the owner registers the key. The key file
    appears only after the hub has the key, so no request carries a key that the hub does not know. True when the
    agent has its key."""
    if load_agent_key(agent):
        return True
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    pending = key_file(agent) + ".pending"
    try:
        with open(pending, encoding="utf-8") as f:
            key = f.read().strip()
    except OSError:
        key = ""
    if not key:                     # the same pending key again after a lost answer: the hub then says it has it
        with contextlib.suppress(FileNotFoundError):
            os.remove(pending)
        key = secrets.token_urlsafe(32)
        write_private(pending, key + "\n")
    code, data = http("POST", f"/helper/key/{agent}", {"sha256": hashlib.sha256(key.encode()).hexdigest()}, timeout=15)
    if code != 200:
        print(f"agent key not registered ({code}): {str(data.get('error') or '')[:160]}", flush=True)
        return False
    os.replace(pending, key_file(agent))
    print("agent key registered with the hub", flush=True)
    return True


def session_credential(agent, kind, generation, task=None, attempt=None):
    """A session credential for one window (owner API 1.12), or None for an agent without a key (alone on its
    address) or a hub without credentials. Raises Refused when the hub refuses one: then the window does not
    start."""
    if not load_agent_key(agent):
        return None
    body = {"kind": kind, "generation": generation}
    if kind == "task":
        body.update(task=task, attempt=attempt)
    code, data = http("POST", f"/helper/session/{agent}", body, timeout=15)
    if code == 404:
        return None                 # a hub from before owner API 1.12
    token = data.get("token") if code == 200 else None
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", token):
        raise Refused(f"the hub gave no session credential ({code}): {str(data.get('error') or '')[:160]}")
    return token


def manager_generation(agent):
    """The hub's DB generation, which the standing session's credential and its conversation record are bound to; ""
    for an agent without a key, which asks the hub nothing at its start, as before. Raises Refused, or URLError and
    OSError while the hub cannot be reached."""
    if not load_agent_key(agent):
        return ""
    code, data = http("GET", f"/events/{agent}?after=-1&wait=0", timeout=10)
    if code != 200 or not data.get("db"):
        raise Refused(f"the hub gave no generation ({code})")
    return data["db"]


def manager_credential(agent, generation=None):
    """The standing session's credential (owner API 1.12), or None without a key. generation: the one that
    manager_generation gave. Raises Refused, or URLError and OSError while the hub cannot be reached."""
    generation = manager_generation(agent) if generation is None else generation
    return session_credential(agent, "standing", generation) if generation else None


def hold_manager_lock(agent):
    """The per-agent start lock of the standing manager session, held: its open file, which releases it when it closes
    (also when this process ends). Refused while another start holds it."""
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    lock = open(os.path.join(HOME, f"manager-{agent}.lock"), "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise Refused("another start of the manager session runs now; nothing was started") from None
    return lock


@contextlib.contextmanager
def manager_start_lock(agent):
    """Serialize the helper and explicit Manager starts through their window check and record write."""
    with hold_manager_lock(agent):
        yield


def manager_conversation(agent, runtime, record):
    """(window session, conversation) of a manager record of this runtime: Claude's session for both, or Codex's
    marker and thread. conversation is None for a Codex start that never began its conversation: no thread is
    recorded, and no rollout carries its marker, so there is nothing to continue. Refused when the record is not valid
    or its conversation file is not found."""
    def valid(value):
        return isinstance(value, str) and bool(THREAD_ID.fullmatch(value))
    path = chat_record_file(agent, runtime, "manager")
    window, session = (record.get("marker"), record.get("session")) if runtime == "codex" else \
        (record.get("session"), record.get("session"))
    if not valid(window) or not (valid(session) or (runtime == "codex" and session is None)):
        raise Refused(f"the manager session record {path} is not valid")
    if runtime == "codex":
        found = chat_thread(record)         # the recorded thread, or the one whose first lines carry the marker
        if not found and session is None:
            return window, None
    else:
        found = session if claude_chat_file(session) else None
        if not found and record.get("begun") is False:
            return window, None             # a start that never began its conversation: Claude wrote no file of it
    if not found:
        raise Refused(f"the manager session cannot continue: its conversation {session or window} is not found. Nothing "
                      f"starts and the record stays: restore the conversation, or remove {path} to start a new one")
    return window, found


def manager_start(agent, conf, generation, token, fresh=False):
    """The one decision of both starts of the standing manager session, the helper's own and `fleet.py start`:
    (command, session id, record, note). Each runtime keeps its own record of its conversation (owner feature
    runtime_switch), so that a switch back continues that runtime's conversation: ~/.fleet/manager-<agent>.claude.json
    {generation, session, started, begun}, and ~/.fleet/manager-<agent>.codex.json {generation, marker, session,
    started}. The session id is the window's @fleet_session: Claude's session, or the marker at the end of Codex's
    first prompt, by which the helper finds its thread (session, once known). A recorded conversation of this
    generation continues with the settings, MCP configuration and credential of a new start and no first prompt.
    record is the one to save before the window starts; None when the saved one stays. A new conversation starts
    only with no record, with a record of another DB generation, or with a record whose start never began its
    conversation (a Codex record with no thread whose marker no rollout carries, a Claude record with "begun": false
    and no file; note says so), or with fresh: the helper closed the recorded one because it was cold and long
    (Helper.restart_fresh), and its file stays where it is.
    Refused when the record cannot be read or is not valid, or when its conversation file is not found: then nothing
    starts and the record stays, so that the conversation is never replaced without notice. Another runtime starts as
    before and has no record."""
    runtime, record = conf.get("runtime"), None
    if runtime in MANAGER_RUNTIMES:
        record = load_chat_record(agent, runtime, "manager")
    env = session_env(agent, conf, "manager")
    note = "a new conversation: the recorded one is of another hub database" if record else ""
    if record and record["generation"] == generation and fresh:
        note = "a new conversation with the hub's brief, in place of the recorded one, which was cold and long"
    elif record and record["generation"] == generation:
        window, found = manager_conversation(agent, runtime, record)
        if found:
            if runtime == "claude":     # its file is there: it began, and a later start that misses that file stops
                keep = dict(record, begun=True) if record.get("begun") is False else None
            else:
                keep = dict(record, session=found) if record.get("session") != found else None
            # A continued conversation gets no first prompt, so it needs no brief either: that session has what it
            # worked out. The brief is for the one that starts cold.
            return (session_command(agent, conf, MANAGER_START.format(agent=agent, resume=""), session=found,
                                    token=token, resume=True, env=env), window, keep,
                    "it continues its recorded conversation")
        note = "a new conversation: the recorded start never began one"
    session, new = str(uuid.uuid4()), None
    if runtime == "claude":
        # begun: its conversation file was seen (note_begun). Until then a start that does not find that file starts
        # a new conversation; after it, or for an older record without the field, it stops instead.
        new = {"generation": generation, "session": session, "started": time.time(), "begun": False}
    elif runtime == "codex":
        new = {"generation": generation, "marker": session, "session": None, "started": time.time()}
    prompt = MANAGER_START.format(agent=agent, resume=manager_resume(agent))
    return (session_command(agent, conf, prompt, session=session, token=token, env=env), session, new, note)


def manager_window(session):
    """(@fleet_session, program) of the standing manager window in a tmux session, from one window list; program is ""
    when its start command cannot be read. None without that window. Refused when tmux gives no list: an unknown
    reading is never "no window"."""
    r = tmux("list-windows", "-t", session, "-F", "#{window_name}\t#{@fleet_session}\t#{pane_start_command}")
    if r.returncode != 0:
        raise Refused(f"cannot read windows in {session}")
    for line in r.stdout.splitlines():
        name, _, rest = line.partition("\t")
        if name == "manager":
            window, _, start = rest.partition("\t")
            try:
                words = launch_words(start)
            except ValueError:
                words = []
            return window, os.path.basename(words[0]) if words else ""
    return None


def manager_binding(agent, runtime, window):
    """The conversation file of the standing manager session that runs `window` (its @fleet_session) on one runtime,
    when that runtime's record names it; None otherwise: then a switch back could not continue it (owner feature
    runtime_switch)."""
    try:
        record = load_chat_record(agent, runtime, "manager") if runtime in MANAGER_RUNTIMES else None
    except Refused:
        return None
    if not record or not window or record.get("marker" if runtime == "codex" else "session") != window:
        return None
    if runtime == "claude":
        return claude_chat_file(window)
    # A thread found by its marker is kept here: the record has it only from the next start, and a search reads the
    # first lines of every recent rollout, which this check would otherwise do in each round of the helper.
    thread = None if record.get("session") else MANAGER_THREADS.get(window)
    thread = thread or chat_thread(record)
    if thread and not record.get("session"):
        MANAGER_THREADS[window] = thread
    paths = codex_threads(thread) if thread else []
    return paths[0] if len(paths) == 1 else None


MANAGER_THREADS = {}        # {marker: Codex thread} of standing manager sessions, once found (manager_binding)
UNREAD = object()           # no reading of the standing window given: the helper reads it (Helper.manager_bound)


def closing_file(agent):
    return os.path.join(HOME, f"manager-{agent}.closing.json")


def save_closing(agent, procs, start=True):
    """Keep {pid: args} of the manager session that the helper closes after a switch while they can run, so that a
    helper that restarts meanwhile ends them too and starts nothing first (retire_old_manager), and whether the new
    runtime's session is due when they have ended: not when the owner's `fleet.py start` already started one (start
    False), whose own window is then the standing session. Once nothing of it is left either, the helper's own watch
    of the window starts one (Helper.manager_gone). {} removes the file."""
    path = closing_file(agent)
    if not procs:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
        return
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    new = f"{path}.{secrets.token_hex(4)}.new"
    write_private(new, json.dumps({"procs": {str(pid): args for pid, args in procs.items()}, "start": bool(start)}) + "\n")
    os.replace(new, path)


def read_closing(agent):
    """(saved {pid: args}, start) of the closing file (save_closing); ({}, True) without one."""
    try:
        with open(closing_file(agent), encoding="utf-8") as f:
            saved = json.load(f)
        return {int(pid): str(args) for pid, args in saved["procs"].items()}, saved.get("start") is not False
    except FileNotFoundError:
        return {}, True
    except (OSError, ValueError, AttributeError, TypeError, KeyError):
        return {}, True             # the helper's own file, written whole: nothing else can stand in for it


def load_closing(agent):
    """{pid: args} of those processes that still run with the same arguments; {} when none does (the file goes)."""
    saved, _ = read_closing(agent)
    left = still_running(saved)
    if not left:
        save_closing(agent, {})
    return left


def closing_start(agent):
    """Whether the new runtime's session is due when the saved processes have ended (save_closing)."""
    return read_closing(agent)[1]


def seen_file(agent):
    return os.path.join(HOME, f"manager-{agent}.seen.json")


def save_seen(agent, seen, procs):
    """Keep {pid: args} of the previous runtime's manager session that this helper saw run (Helper.manager_procs),
    and what it saw of it, seen = (window id (@fleet_session), runtime, keeps: what its close keeps, manager_keeps: its
    conversation file, "" for a start that had not begun its conversation, or None), so that a restart of the helper,
    also the restart of its update, still knows them when that session goes before the next reading. None removes the
    file."""
    path = seen_file(agent)
    if seen is None:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
        return
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    new = f"{path}.{secrets.token_hex(4)}.new"
    write_private(new, json.dumps({"session": seen[0], "runtime": seen[1], "keeps": seen[2],
                                   "procs": {str(pid): args for pid, args in procs.items()}}) + "\n")
    os.replace(new, path)


def load_seen(agent):
    """(seen, {pid: args} that still run) of the saved snapshot (save_seen); (None, {}) without one."""
    try:
        with open(seen_file(agent), encoding="utf-8") as f:
            saved = json.load(f)
        seen = (saved["session"], saved["runtime"], saved["keeps"])
        if not all(isinstance(v, str) for v in seen[:2]) or not (seen[2] is None or isinstance(seen[2], str)):
            return None, {}
        return seen, still_running({int(pid): str(args) for pid, args in saved["procs"].items()})
    except FileNotFoundError:
        return None, {}
    except (OSError, ValueError, AttributeError, TypeError, KeyError):
        return None, {}             # the helper's own file, written whole: nothing else can stand in for it


def still_running(procs):
    """{pid: args} of these processes that still run with the same arguments: a reused process id does not count."""
    if not procs:
        return {}
    args_of = {pid: args for pid, _, args, _ in process_table("", "")}
    return {pid: args for pid, args in procs.items() if args_of.get(pid) == args}


def team_runtime(agent):
    """The runtime that team.json in this checkout gives the agent now, or None when it cannot be read. On the hub
    machine the checkout is the hub's, so a switch is there before this helper's update loads it."""
    try:
        with open(os.path.join(KIT, "team.json"), encoding="utf-8") as f:
            return ((json.load(f).get("agents") or {}).get(agent) or {}).get("runtime")
    except (OSError, ValueError, AttributeError):
        return None


def limit_menu(runtime, path):
    """True when a session's conversation file ends with its usage limit: the question that its screen shows then is
    the runtime's usage-limit menu. The helper never answers that menu (one of its choices buys extra usage), but a
    switch may close the session without an answer: nothing is chosen, and the conversation stays."""
    if not path or runtime not in MANAGER_RUNTIMES:
        return False
    try:
        with open(path, "rb") as stream:
            stream.seek(max(0, os.path.getsize(path) - 256 * 1024))
            rows = complete_rows(stream.read())
    except OSError:
        return False
    translator, last = Translator(runtime), None
    for row in rows:
        for record in translator.records(row):
            if record.get("type") not in ("session", "model", "result"):
                last = record           # the last thing that the session did
    return bool(last and last.get("type") == "error" and last.get("code") == "usage_limit")


HANDOVER_MAX = 256      # message ids in one runtime's list of a hand-over: the hub takes no more (hub.HANDOVER_MAX)
READ_HEAD = re.compile(r"\AUnread messages: (M-\d+(?:, M-\d+)*)\.\n")     # the first line of the manager's read answer
FLEET_READ = "mcp__fleet__read_messages"
HANDOVER_TAIL = 4 * 1024 * 1024     # the end of a conversation file that unfinished_turn_reads reads first


def read_ids(value):
    """The message ids that one answer of the manager's read_messages names in its first line (hub t_read_messages),
    from its text, or from the tool result's JSON in older rollouts. Only that line counts: no message text can name
    one."""
    text = text_of(value)
    if text.lstrip().startswith(("{", "[")):
        with contextlib.suppress(ValueError):
            text = text_of(json.loads(text))
    head = READ_HEAD.match(text)
    return [int(i) for i in re.findall(r"\d+", head.group(1))] if head else []


def turn_reads(runtime, rows):
    """(ended, ids): whether a turn of the session ended normally in these conversation rows, and the ids of the
    messages that read_messages gave it after the last such end. A Claude turn ends normally with the model's own
    end_turn, a Codex turn with a task_complete that has no error. A turn that its usage limit or another error stopped,
    or that was interrupted, did not end normally."""
    ended, ids, calls = False, [], set()
    for row in rows:
        if runtime == "claude":
            if row.get("isSidechain"):
                continue                # a subagent's own rows
            message = row.get("message") if isinstance(row.get("message"), dict) else {}
            blocks = [b for b in message.get("content") or [] if isinstance(b, dict)] \
                if isinstance(message.get("content"), list) else []
            if row.get("type") == "assistant" and message.get("stop_reason") == "end_turn" and \
                    not row.get("isApiErrorMessage"):
                ended, ids, calls = True, [], set()
            elif row.get("type") == "assistant":
                calls.update(b["id"] for b in blocks if b.get("type") == "tool_use" and b.get("name") == FLEET_READ
                             and isinstance(b.get("id"), str))
            elif row.get("type") == "user":
                results = [b for b in blocks if b.get("type") == "tool_result"]
                # Claude Code keeps only a preview of a long result in the message; the row's toolUseResult has it whole.
                whole = row.get("toolUseResult") if len(results) == 1 and isinstance(row.get("toolUseResult"), list) else None
                for b in results:
                    if str(b.get("tool_use_id")) in calls:
                        ids += read_ids(whole) or read_ids(b.get("content"))
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        kind, ptype = row.get("type"), payload.get("type")
        if kind == "event_msg" and ptype == "task_complete" and not payload.get("error"):
            ended, ids, calls = True, [], set()
        elif kind == "event_msg" and ptype == "item_completed":
            item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
            if item.get("type") == "McpToolCall" and item.get("server") == "fleet" and item.get("tool") == "read_messages":
                ids += read_ids((item.get("result") if isinstance(item.get("result"), dict) else {}).get("content"))
        elif kind == "response_item" and ptype == "function_call" and isinstance(payload.get("call_id"), str) and \
                (payload.get("name") == FLEET_READ or (payload.get("name") == "read_messages" and
                                                       "fleet" in str(payload.get("namespace") or ""))):
            calls.add(payload["call_id"])
        elif kind == "response_item" and ptype == "function_call_output" and str(payload.get("call_id")) in calls:
            ids += read_ids(payload.get("output"))
    return ended, ids


def unfinished_turn_reads(runtime, path):
    """The ids of the manager's messages that its session read after its last turn that ended normally (turn_reads),
    from its conversation file: a turn that its usage limit stopped, for example, did not finish them. A switch hands
    them to the new runtime's session (hub record_status). [] when every read was finished, or the file cannot be
    read. Only the end of a long file is read, unless a normal turn end is not in it."""
    if not path or runtime not in MANAGER_RUNTIMES:
        return []
    step = HANDOVER_TAIL
    try:
        size = os.path.getsize(path)
        while True:
            with open(path, "rb") as stream:
                stream.seek(max(0, size - step))
                ended, ids = turn_reads(runtime, complete_rows(stream.read(min(size, step))))
            if ended or step >= size:
                return sorted(set(ids))[-HANDOVER_MAX:]
            step *= 4
    except OSError:
        return []


def start_handover(agent, generation):
    """The hand-over that both starts of the standing manager session save just before they start it (save_handover;
    owner feature runtime_switch): for each runtime, the messages that its recorded conversation of this hub database
    read in its last turn and did not finish (unfinished_turn_reads). That is the previous runtime's session however it
    ended (closed by this helper after a switch, by the owner, or before a restart of this helper), and also the
    conversation that this start resumes, because a resumed session does not go on with a turn that its end stopped.
    The hub uses it only at the end of a switch, and for each message only the list of the runtime whose session read
    it last (hub handover_ids), so a start with no switch hands nothing over."""
    out = {}
    for runtime in MANAGER_RUNTIMES:
        try:
            record = load_chat_record(agent, runtime, "manager")
        except Refused:
            continue
        if record and record.get("generation") == generation:
            ids = unfinished_turn_reads(runtime, manager_binding(agent, runtime,
                                                                 record.get("marker" if runtime == "codex" else "session")))
            if ids:
                out[runtime] = ids
    return out


def handover_file(agent):
    return os.path.join(HOME, f"manager-{agent}.handover.json")


def save_handover(agent, handover):
    """Keep the hand-over of a start ({runtime: ids}, start_handover) until the hub has it: the helper reports it in
    each status (manager_handover), also after a restart. {} removes the file."""
    path, handover = handover_file(agent), clean_handover(handover)
    if not handover:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
        return
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    new = f"{path}.{secrets.token_hex(4)}.new"
    write_private(new, json.dumps(handover, sort_keys=True) + "\n")
    os.replace(new, path)


def clean_handover(handover):
    """{runtime: sorted ids} of the manager runtimes, at most HANDOVER_MAX ids each, without an empty list or a value
    that is no message id."""
    out = {}
    for runtime, ids in (handover.items() if isinstance(handover, dict) else ()):
        ids = sorted({i for i in ids if isinstance(i, int) and not isinstance(i, bool) and 0 < i < 2 ** 63}
                     if isinstance(ids, list) else ())[-HANDOVER_MAX:]
        if runtime in MANAGER_RUNTIMES and ids:
            out[runtime] = ids
    return out


def load_handover(agent):
    """The saved hand-over (save_handover); {} when there is none or the file cannot be read."""
    try:
        with open(handover_file(agent), encoding="utf-8") as f:
            return clean_handover(json.load(f))
    except (OSError, ValueError):
        return {}


def manager_keeps(agent, runtime, window):
    """What a close of the standing manager session on the previous runtime keeps: its conversation file
    (manager_binding), or "" for a recorded start that never began its conversation, which has nothing to keep: a Codex
    record with no thread, a Claude record with "begun": false (as manager_conversation decides). None when that
    runtime's record does not name the session that runs, or names a conversation that began whose file is not found:
    then its helper leaves its window open, and the record stays, so that the next start of that runtime says so."""
    binding = manager_binding(agent, runtime, window)
    if binding:
        return binding
    try:
        record = load_chat_record(agent, runtime, "manager") if runtime in MANAGER_RUNTIMES else None
    except Refused:
        return None
    named = bool(record and window and record.get("marker" if runtime == "codex" else "session") == window)
    return "" if named and (not record.get("session") if runtime == "codex" else record.get("begun") is False) else None


def unnoted(record, runtime, window):
    """True when this manager record names the conversation that runs `window` (its @fleet_session) and does not say
    yet that it began (note_begun): Claude's "begun": false, or a Codex record with no thread."""
    if not record or not window:
        return False
    if runtime == "claude":
        return record.get("session") == window and record.get("begun") is False
    return runtime == "codex" and record.get("marker") == window and not record.get("session")


def note_begun(agent, runtime, window):
    """Note in the record of the manager conversation that runs `window` that it began, once a reading found its file:
    Claude's "begun": true, or the thread of a Codex record (the one that its marker names). A later start of that
    runtime that does not find that file then stops with a notice instead of starting a new conversation
    (manager_conversation). The caller holds the start lock."""
    with contextlib.suppress(Refused, OSError):
        record = load_chat_record(agent, runtime, "manager") if runtime in MANAGER_RUNTIMES else None
        if not unnoted(record, runtime, window):
            return
        if runtime == "claude":
            save_chat_record(agent, dict(record, begun=True), runtime, "manager")
            return
        thread = MANAGER_THREADS.get(window) or chat_thread(record)
        if thread:
            save_chat_record(agent, dict(record, session=thread), runtime, "manager")


def adopt_manager(agent, conf, session):
    """The owner's step for a manager session that runs with no record of its conversation (an earlier Fleet started
    it): record the session id that its window carries (@fleet_session), so that the next start continues it. Only
    with that conversation's file in this checkout's project folder. It reads the window list and writes the record:
    it starts, stops and types nothing. A session of the previous runtime waits for its helper, which closes it after
    a switch. Returns words for the owner; Refused when nothing can be recorded."""
    running, program = manager_window(session) or ("", "")
    if program and program != conf.get("runtime") and manager_keeps(agent, program, running) is None:
        return (f"it still runs the previous runtime ({program}), whose conversation is not recorded or not found, so "
                f"its helper does not close it. Close its window; then its helper starts {conf.get('runtime')}. Nothing "
                f"changed")
    if program and program != conf.get("runtime"):
        return (f"it still runs the previous runtime ({program}): its helper closes it when it is quiet and then starts "
                f"{conf.get('runtime')}. Nothing changed")
    if conf.get("runtime") != "claude":
        return "its conversation is not recorded this way: a Codex manager records its conversation when it starts"
    path = chat_record_file(agent, "claude", "manager")
    generation = manager_generation(agent)
    record = load_chat_record(agent, "claude", "manager")
    if record and record["generation"] == generation and record.get("session") != running:
        raise Refused(f"the record {path} names conversation {record.get('session')}, and the manager session that runs "
                      f"has {running or 'no session id'}. Nothing changed. Remove the record to record the session that runs")
    if not THREAD_ID.fullmatch(running):
        raise Refused("the manager session that runs has no session id (@fleet_session), so its conversation cannot "
                      "be recorded. Nothing changed")
    if not claude_chat_file(running):
        raise Refused(f"the conversation {running} of the manager session that runs is not found in this checkout's "
                      f"Claude project folder. Nothing was recorded or changed")
    if record and record["generation"] == generation:
        return f"its conversation {running} is recorded: the next start continues it"
    save_chat_record(agent, {"generation": generation, "session": running, "started": time.time(), "adopted": True,
                             "begun": True},
                     "claude", "manager")
    return f"recorded its conversation {running} in {path}: the next start continues it"


def claim_checkout(agent):
    """Owner API 1.12: each agent runs from its own checkout, which its first start here names. Another agent may
    not start from it."""
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    path = os.path.join(HOME, "checkout-" + hashlib.sha256(os.path.realpath(KIT).encode()).hexdigest()[:16])
    with contextlib.suppress(FileExistsError):
        write_private(path, agent + "\n")
    with open(path, encoding="utf-8") as f:
        owner = f.read().strip()
    if owner != agent:
        die(f"this checkout belongs to {owner}; each agent runs from its own checkout")


def register_key(agent):
    """Make this machine's key for sealed handovers, and give the hub the public half. True once the hub has it.
    In broker mode the key is the vault broker's, and team.json pins its public half. An agent without credential
    access (vault "none") has none."""
    if vault_mode(agent) in ("broker", "none"):
        return True
    key = os.path.join(HOME, "age.key")
    if not shutil.which("age-keygen"):
        return False
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    if not os.path.exists(key):
        subprocess.run(["age-keygen", "-o", key], capture_output=True)
    r = subprocess.run(["age-keygen", "-y", key], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return False
    try:
        return http("POST", f"/keys/{agent}", {"pubkey": r.stdout.strip()})[0] == 200
    except (urllib.error.URLError, OSError):
        return False


def load_place(path):
    """Where this helper stopped reading the hub's messages: (the hub database it was, the last message)."""
    try:
        with open(path) as f:
            d = json.load(f)
        return d.get("db"), int(d.get("cursor") or 0)
    except (OSError, ValueError, AttributeError, TypeError):
        return None, 0


def save_place(path, db, cursor):
    with open(path, "w") as f:
        json.dump({"db": db, "cursor": cursor}, f)


# ---------------------------------------------------------------- checks (owner API 1.8)

CHECK_SLOTS = threading.BoundedSemaphore(4)     # checks that run at the same time on this machine
CHECK_SOURCE_MAX, CHECK_OUTPUT_MAX = 65536, 4096


def git_blob(kit, spec):
    """The exact bytes of <commit>:<path> in a checkout, or None."""
    try:
        r = subprocess.run(["git", "-C", kit, "cat-file", "blob", spec], capture_output=True, timeout=30,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def finite_number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and abs(value) < float("inf")
    except OverflowError:
        return False


def _no_constant(name):
    raise ValueError(f"{name} is not a number")


def run_check(extra, kit=KIT, agent=None):
    """Run one approved check outside every AI session: the verified bytes of the pinned commit (never the working
    tree), or of an agent's saved code from the hub (owner API 1.10), in a new process group, in an empty folder that
    is also its HOME, with no Fleet value or token in its environment. Returns the result body for the hub, without
    the token."""
    started = time.monotonic()

    def result(**body):
        return dict(body, elapsed_ms=int((time.monotonic() - started) * 1000))

    def fail(code, detail):
        return result(ok=False, error=code, detail=detail[:200])

    if extra.get("code") == "saved":        # an agent's saved code: from the hub, for this open claim only
        try:
            code, got = http("GET", f"/helper/trigger/{urllib.parse.quote(agent or '')}/{int(extra['run'])}/code?" +
                             urllib.parse.urlencode({"token": extra["token"]}), timeout=15)
        except (urllib.error.URLError, OSError):
            code, got = 0, {}
        if code != 200 or not isinstance(got.get("source"), str):
            return fail("missing", "the hub gave no saved code for this run")
        data = got["source"].encode("utf-8")
        if len(data) > CHECK_SOURCE_MAX or hashlib.sha256(data).hexdigest() != extra["sha256"]:
            return fail("changed", "the saved code does not have the approved SHA-256")
        return run_source(extra, data.decode("utf-8"), result, fail)
    spec = f"{extra['commit']}:{extra['path']}"
    data = git_blob(kit, spec)
    if data is None:                    # a commit that this checkout does not have yet
        try:
            subprocess.run(["git", "-C", kit, "fetch", "-q", "origin"], capture_output=True, timeout=60,
                           stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            pass
        data = git_blob(kit, spec)
    if data is None:
        return fail("missing", f"{extra['path']} at {extra['commit'][:12]} is not in this checkout")
    if len(data) > CHECK_SOURCE_MAX or hashlib.sha256(data).hexdigest() != extra["sha256"]:
        return fail("changed", f"{extra['path']} at {extra['commit'][:12]} does not have the approved SHA-256")
    try:
        source = data.decode("utf-8")
    except UnicodeDecodeError:
        return fail("changed", f"{extra['path']} is not UTF-8 text")
    return run_source(extra, source, result, fail)


def run_source(extra, source, result, fail):
    """Run checked check source with python3 -I, as run_check describes, and read its one JSON answer."""
    given = json.dumps({"trigger": extra["trigger"], "run": extra["run"], "args": extra.get("args") or {}})
    with tempfile.TemporaryDirectory(prefix="fleet-check-") as folder:
        env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": folder, "LANG": os.environ.get("LANG", "C.UTF-8")}
        try:
            proc = subprocess.Popen([sys.executable, "-I", "-c", source, given], cwd=folder, env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
        except OSError as e:
            return fail("exit", f"the check did not start: {e}")
        out = []
        reader = threading.Thread(target=lambda: out.append(proc.stdout.read(CHECK_OUTPUT_MAX + 1)), daemon=True)
        reader.start()
        try:
            proc.wait(timeout=extra["timeout_seconds"])
            late = False
        except subprocess.TimeoutExpired:
            late = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)     # the check, and anything that it left running
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
        reader.join(5)
        proc.stdout.close()
        printed = out[0] if out else b""
    if len(printed) > CHECK_OUTPUT_MAX:
        return fail("too_large", "the check printed more than 4 KiB")
    if late:
        return fail("timeout", f"no answer in {extra['timeout_seconds']} seconds")
    if proc.returncode != 0:
        return fail("exit", f"the check ended with exit code {proc.returncode}")
    try:
        found = json.loads(printed.decode("utf-8"), parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError):
        return fail("bad_output", "the check did not print exactly one JSON object")
    if isinstance(found, dict) and "value" in found and set(found) <= {"value", "summary"}:
        summary = found.get("summary", "")
        if not finite_number(found["value"]) or not isinstance(summary, str) or len(summary) > 200:
            return fail("bad_output", "value must be a finite number, and summary text of at most 200 characters")
        return result(ok=True, value=found["value"], summary=summary)
    if isinstance(found, dict) and "error" in found and set(found) <= {"error", "detail", "retry_after"}:
        code, detail, retry = found["error"], found.get("detail", ""), found.get("retry_after")
        if not (isinstance(code, str) and re.fullmatch(r"[a-z_]{1,40}", code)) or not isinstance(detail, str) \
                or len(detail) > 200 or (retry is not None and (isinstance(retry, bool) or not isinstance(retry, int)
                                                                or not 60 <= retry <= 86400)):
            return fail("bad_output", "error must be a code of a-z and _, detail text of at most 200 characters, and "
                                      "retry_after 60 to 86400 seconds")
        return result(ok=False, error=code, detail=detail, **({} if retry is None else {"retry_after": retry}))
    return fail("bad_output", "the check must print {value, summary} or {error, detail, retry_after}")


class Helper:
    def __init__(self, agent, conf):
        self.agent, self.conf, self.session = agent, conf, f"fleet-{agent}"
        self.worker = conf.get("role") in ("worker", "lead")      # a lead works like a worker, one session per task
        self.signalled = {}       # a throttle only; the hub owns the unread messages
        self.told = {}            # {window: (tmux window id, the event ids its last signal told of)} (signal)
        self.retold = {}          # {window: seconds before the same unread messages are told again}
        self.wanted = {}          # {window: ids of this poll's events that want a signal} (handle_events)
        self.typed_at = {}        # {window: when its last signal was typed}, a model call of its session (restart_fresh)
        self.manager_session = None
        self.retiring = None      # (first quiet reading, its marks) of the previous runtime's chat (runtime_switch)
        self.retired_left = {}    # {pid: args} of that chat's processes that did not end when it closed
        self.manager_lock = None        # the start lock while the previous runtime's manager session closes
        self.manager_retiring = None    # (first quiet reading, its marks) of that session (retire_old_manager)
        # {pid: args} of that session seen at its readings, while it closes, and manager_snap = (window id, runtime,
        # manager_keeps) of it; also from before a restart of this helper (save_seen)
        self.manager_snap, self.manager_procs = load_seen(agent) if conf.get("role") == "manager" else (None, {})
        self.manager_own = None         # (window id, {pid: args}) of the agent's own runtime's session, last round
        self.manager_seen_old = None    # (runtime, session id, manager_keeps) of that session at the latest reading
        self.manager_tried = None       # the last try to start the manager session (manage); None before the first
        self.manager_due = False        # its start is due (manage)
        self.manager_fresh = False      # that start is a new conversation (restart_fresh)
        self.fresh_at = None            # when this helper last started one so (restart_fresh)
        self.compact_restarted = False  # the session without STANDING_COMPACT was closed once (restart_fresh)
        self.manager_pending = None     # (session id, note) of a start whose tmux reading failed (start_manager)
        self.manager_asked = None       # when this helper last told the hub of a question on its screen
        # {pid: args} of its processes that did not end when it closed, also from before a restart of this helper
        self.manager_left = load_closing(agent) if conf.get("role") == "manager" else {}
        self.manager_left_start = closing_start(agent) if self.manager_left else True     # due when they have ended
        # {(runtime, session id): manager_keeps} of the sessions whose record still needs a step when they go
        # (unbegun_record): a Claude start of the agent's own runtime that has not begun its conversation ("": its
        # record names nothing), or a conversation that began whose record a busy lock kept from noting it
        # (note_begun), also one of the saved snapshot at this helper's start (manager_first)
        self.manager_seen_own = {}
        self.manager_seen = ()          # the manager_bound that the hub last got from this helper (never at first)
        self.manager_report_failed = None   # when the hub last did not take such a report (report_manager_bound)
        self.conversation_seen = {}     # {C-<id>: when this helper first saw that window} (retire_conversations)
        self.outside, self.outside_lock = 0, threading.Lock()      # checks and login jobs that run now
        self.requests = {}              # call_api: {job: task} of the requests that run now, each in its own thread
        self.screen_attempts = {}       # {task: (attempt, told)} of the runners (app_server_task) that this helper started
        self.start_waits = {}           # {event id: (seconds, monotonic time of the next try)} of failed task events
        if self.worker:
            self.tailer = Tailer(conf.get("runtime", ""), self.task_windows,
                                 lambda batch: http("POST", f"/helper/events/{agent}", batch, timeout=15), kit=KIT,
                                 requested={"model": conf.get("model"), "effort": conf.get("effort")},
                                 screen_requested=screen_settings(conf))
        else:
            self.tailer = Tailer(conf.get("runtime", ""), self.manager_windows, self.report_limit,
                                 requested={"model": conf.get("model"), "effort": conf.get("effort")})

    def say(self, text):
        print(time.strftime("%H:%M:%S"), text, flush=True)

    def look(self):
        return [w for w in windows(self.session) if w != "home"]

    def keep_session(self):
        """Make this agent's tmux session again when it is gone. One round of the helper's loop; True when it had to.

        This helper does not own the tmux server, so the session it made at its own start is not its to assume. A
        tmux session ends with its last window, and the server itself can go under the helper: `tmux kill-server`,
        a logout that takes it, a crash of the server, or its last window killed by hand. Then `new-window` answers
        "no server running" and no window starts at all.

        Until 19 October 2026 only the standing manager's start made the session again (`start_manager`), so a
        worker or a lead that lost its session was finished: every task event failed at its start, waited longer
        (`handle_events`), failed again, and nothing in the fleet said so. The helper went on polling and reporting
        its machine, `/ui/health` stayed `ok`, the agent counted as available in `see_capacity`, and the tasks given
        to that machine simply sat there until somebody restarted its service by hand. It is the same fault as the
        manager window that nothing watched (`manager_gone`): what the helper starts, it must not assume stays
        started."""
        try:
            if not ensure_session(self.session):
                return False
        except subprocess.CalledProcessError:
            self.say(f"the tmux session {self.session} is gone and could not be made again; the helper tries again")
            return False
        self.say(f"the tmux session {self.session} was gone; the helper made it again")
        return True

    def sync_checkout(self):
        """Before a task session starts, the helper brings this folder onto the configuration ref and up to date with
        it (fast-forward only), so that the agent reads the current RULES.md, its profile, the owner's knowledge and
        team.json without pulling, and a read-only task changes no file itself. A failure is only logged."""
        problem = follow_config_ref(KIT)
        if problem:
            self.say("checkout not brought up to date before the task: " + problem)

    def started(self):
        """{window: start command} of this agent's windows, home excluded: {} when tmux says that it has no such
        session, None when it gives no answer (a reading that failed is unknown, never empty)."""
        r = tmux("list-windows", "-t", self.session, "-F", "#{window_name}\t#{pane_start_command}")
        if r.returncode != 0:
            gone = tmux("has-session", "-t", self.session)
            missing = gone.returncode == 1 and any(w in gone.stderr for w in ("can't find session", "no server running",
                                                                             "error connecting"))
            return {} if missing else None
        return {name: start for name, _, start in (line.partition("\t") for line in r.stdout.splitlines())
                if name and name != "home"}

    def legacy_windows(self):
        """Owner API 1.12: the windows that started without a session credential (before this agent had a key, or
        from an older helper). Each gets one at its next normal start. A window with one names its file in
        ~/.fleet/sessions in its start command."""
        windows = self.started()
        return None if windows is None else sorted(n for n, start in windows.items() if SESSIONS + os.sep not in start)

    def sweep_sessions(self):
        """Remove this agent's session files that no window names any more, a minute after they were made."""
        windows = self.started()
        if windows is None:
            return                      # tmux did not answer: keep everything until it does
        used = "\n".join(windows.values())
        try:
            names = os.listdir(SESSIONS)
        except OSError:
            return
        for name in names:
            path = os.path.join(SESSIONS, name)
            if name.startswith(self.agent + ".") and name not in used:
                with contextlib.suppress(OSError):
                    if time.time() - os.lstat(path).st_mtime > 60:
                        os.remove(path)

    def chat_resume(self, generation):
        """The conversation to continue when the owner chat of this generation starts again (a Codex thread or a
        Claude session), or None for a new one: no record, another generation's chat (its route is closed), or a
        runtime without chats. Each runtime has its own record (owner feature runtime_switch), so a conversation never
        continues on the other runtime. A record of this generation whose conversation is not found, or a record that
        cannot be read, stops the start: the chat event stays pending and the record stays, so that the conversation is
        never replaced without notice."""
        runtime = self.conf.get("runtime")
        if runtime not in MANAGER_RUNTIMES:
            return None
        try:
            record = load_chat_record(self.agent, runtime)
            if not record or record.get("generation") != generation:
                return None
            thread = chat_thread(record) if runtime == "codex" else \
                record["session"] if claude_chat_file(record.get("session")) else None
            if thread is None:
                raise Refused(f"the owner chat cannot continue: its conversation "
                              f"{record.get('session') or record.get('marker')} is not found. Nothing starts and the "
                              f"record stays: restore the conversation, or remove {chat_record_file(self.agent, runtime)} "
                              f"to start a new one")
        except Refused as e:
            last = getattr(self, "chat_problem", (None, 0.0))
            if last[0] != str(e) or time.monotonic() - last[1] > 300:
                self.chat_problem = (str(e), time.monotonic())
                self.say(f"owner-chat: {e}")
            raise
        if not record.get("session"):
            save_chat_record(self.agent, dict(record, session=thread), runtime)
        return thread

    def start_manager(self):
        """True while the standing Manager must still start; False once it runs. A concurrent start or an unknown
        window reading waits, and so do processes of a previous runtime's session that still run (retire_old_manager).
        Its credential, command, window and conversation record are all under one start lock."""
        if self.manager_left or self.outdated():
            return True
        try:
            with manager_start_lock(self.agent):
                ensure_session(self.session)        # it ends with its last window, which can be the closed old one
                if "manager" in windows(self.session, checked=True):
                    pending = self.manager_pending
                    if pending:     # the start whose tmux options and window list failed: it runs, when it is that
                        window = manager_window(self.session) or ("", "")   # window (no other start set its id)
                        self.manager_pending = None
                        if window[0] in ("", pending[0]):
                            self.keep_started(*pending, again=True)
                    return False
                self.manager_pending = None         # it did not start: a new start
                generation = manager_generation(self.agent)
                token = manager_credential(self.agent, generation)
                command, session, record, note = manager_start(self.agent, self.conf, generation, token,
                                                               fresh=self.manager_fresh)
                save_handover(self.agent, start_handover(self.agent, generation))
                if record:      # first: no window runs without it (a start that never begins is replaced next time)
                    save_chat_record(self.agent, record, self.conf.get("runtime"), "manager")
                try:
                    self.start("manager", command, session=session)
                except subprocess.CalledProcessError:
                    try:
                        runs = "manager" in windows(self.session, checked=True)
                    except Refused:
                        self.manager_pending = (session, note)      # the next round sees whether it runs
                        raise
                    if not runs:
                        raise                   # its program ended at once: the helper tries again
                    self.keep_started(session, note, again=True)
                    return False
                self.keep_started(session, note)
                return False
        except Refused as e:            # the helper tries again
            self.say_manager(f"manager: the session does not start: {e}")
            return True
        except Updating:                # a Fleet update runs now: this helper or the restarted one starts it
            return True
        except subprocess.CalledProcessError:       # tmux refused or did not answer, or its program ended at once
            self.say_manager("manager: the session did not start or ended at its start; the helper tries again")
            return True

    def keep_started(self, session, note, again=False):
        """A manager session that this helper started runs (its record was saved before its start). With again its
        window gets its session id once more, after tmux failed or did not answer during the start (manager_windows
        and the switch read it there)."""
        if again:
            with contextlib.suppress(subprocess.CalledProcessError):
                tmux("set-option", "-w", "-t", f"{self.session}:=manager", "@fleet_session", session, check=True)
        self.say("manager: session started" + (f" ({note})" if note else ""))

    def outdated(self):
        """True while team.json already gives this agent another runtime than the one that this helper loaded: its
        update restarts it soon. Until then it neither closes nor starts a manager session, which could be the one
        that `fleet.py start` started on the new runtime."""
        latest = team_runtime(self.agent)
        if latest in (None, self.conf.get("runtime")):
            return False
        self.say_manager(f"manager: this helper waits for its Fleet update to {latest} before it changes the manager "
                         f"session")
        return True

    def say_manager(self, text):
        """A problem of the manager's start or switch: the same words at most each five minutes."""
        last = getattr(self, "manager_problem", (None, 0.0))
        if last[0] != text or time.monotonic() - last[1] > 300:
            self.manager_problem = (text, time.monotonic())
            self.say(text)

    def start(self, name, command, attempt=0, generation="", session="", needs=None):
        lifecycle = getattr(self, "lifecycle", None)
        with lifecycle.session_start() if lifecycle else contextlib.nullcontext():     # never during a Fleet update
            if needs:
                self.sync_checkout()            # task windows only; never while a Fleet update runs
            if callable(command):               # a task's command reads team.json of the checkout (integration_env)
                command = command()
            standing = name == "manager" and session      # its session id is its link to its conversation record
            if standing:
                start_window(self.session, name, command, {"@fleet_session": session})
            else:
                start_window(self.session, name, command)
            try:
                tmux("set-option", "-w", "-t", f"{self.session}:={name}", "@fleet_attempt", f"{generation}:{attempt}", check=True)
                if needs:               # every task window says what it runs; runner() and task_windows() rely on it
                    tmux("set-option", "-w", "-t", f"{self.session}:={name}", "@fleet_needs", needs, check=True)
                if session and not standing:    # after @fleet_needs: the record reader takes a window with a session
                    tmux("set-option", "-w", "-t", f"{self.session}:={name}", "@fleet_session", session, check=True)
            finally:
                if needs and session and app_server_task(self.conf, needs):
                    # The runner started. It can end at once, also before its window has these options, and then no
                    # look at the windows finds it. So the helper keeps that it started this attempt (handle starts
                    # it one time only), and gives the record reader the session from what it knows: the reader
                    # then sends the runner's last records as those of a session that ended.
                    self.screen_attempts[name] = (f"{generation}:{attempt}", False)
                    self.tailer.register((name, generation, attempt, session, "codex_app", needs))
        # The first signal waits the signal throttle: a runtime's first screen can be its own question (Claude Code's
        # workspace trust), and a line typed before it shows would answer it with its Enter.
        self.signalled[name] = time.monotonic()

    def sweep_desktop(self):
        """The owner's live view of a browser task, on a Mac whose agent can show its screen: one publisher per
        task attempt, the same one that screen tasks use. It captures only while the owner's view renews its
        lease, and the hub refuses it for a held or finished task. A task in the runner (app_server_task) keeps the
        runner's own publisher."""
        publishers = self.__dict__.setdefault("publishers", {})
        if not (self.worker and "screen" in self.conf.get("can", []) and sys.platform == "darwin"):
            return
        r = tmux("list-windows", "-t", self.session, "-F", "#{window_name}\t#{@fleet_attempt}\t#{@fleet_needs}")
        live = set()
        for line in r.stdout.splitlines() if r.returncode == 0 else []:
            name, attempt, needs = (line.split("\t") + ["", ""])[:3]
            generation, _, number = attempt.rpartition(":")
            if re.fullmatch(r"T-\d+", name) and needs == "browser" and number.isdigit() and \
                    not app_server_task(self.conf, needs):
                live.add((name, generation, int(number)))
        for key in [k for k in publishers if k not in live]:
            publishers.pop(key).close()
        for key in live:
            if key not in publishers or not publishers[key].thread.is_alive():
                name, generation, number = key
                publishers[key] = Publisher(self.agent, name, {"attempt": number, "generation": generation}, http)
                publishers[key].start()

    def want(self, window, event):
        """A pending event of this poll wants a signal into `window`; handle_events gives each window one."""
        self.wanted.setdefault(window, set()).add(event)

    def signal(self, window, ids=()):
        """Tell one session that the hub holds messages for it. ids: the pending events that want the signal.

        Each message is told once. The hub keeps an unread message pending, so until 22 October 2026 every poll
        typed the same line again, every 20 seconds and also while a turn ran, for as long as one message waited --
        and each of those lines was one more model turn over the session's whole context, most of them answered with
        "No unread messages". The manager spent most of a week's allowance on that. Now a message that a session was
        told of and left unread is told again only after RESIGNAL_FIRST seconds, doubling up to RESIGNAL_MAX, and a
        standing session is not typed into while a turn runs (STANDING_WINDOWS): it reads once more at the end of the
        turn, and a message that came during the turn and is still unread then is told as soon as the turn ends. A
        task session is still told during its turn, once, so that a steer reaches it. A new window (its tmux id)
        knows nothing of earlier signals. A manager session that a message finds cold and long is not told at all:
        it starts again on a new conversation, and that one is told (restart_fresh)."""
        target = f"{self.session}:={window}"
        if time.monotonic() - self.signalled.get(window, 0) < SIGNAL_GAP:
            return
        r = tmux("display-message", "-p", "-t", target, "#{window_id}")
        wid, ids = (r.stdout.strip() if r.returncode == 0 else ""), frozenset(ids)
        was, told = self.told.get(window, ("", frozenset()))
        fresh = wid != was or not ids <= told
        if not fresh and time.monotonic() - self.signalled.get(window, 0) < self.retold.get(window, RESIGNAL_FIRST):
            return
        if window in STANDING_WINDOWS and self.working(target):
            return                  # a turn runs: it reads its messages at its end, and this tells it after that
        try:
            if window == "manager" and self.restart_fresh(target):
                return              # a new conversation starts instead, and the next poll tells it
        except Exception as e:      # a check that fails never keeps a message from the manager: it is told as before
            self.say(f"manager: the check for a cold session failed ({type(e).__name__})")
        text = (CONVERSATION_SIGNAL if CONVERSATION_WINDOW.fullmatch(window) else
                f'New fleet messages. Call read_messages(task_id="{window}") now.'
                if self.worker and window != "owner-chat" else CHAT_SIGNAL)
        got = type_into(self.session, window, text)
        self.signalled[window] = time.monotonic()
        if got == "typed":
            self.typed_at[window] = time.monotonic()
            self.told[window] = (wid, ids)
            self.retold[window] = RESIGNAL_FIRST if fresh else \
                min(RESIGNAL_MAX, 2 * self.retold.get(window, RESIGNAL_FIRST))
        if got == "asks" and window != "owner-chat" and not CONVERSATION_WINDOW.fullmatch(window):
            http("POST", f"/helper/dialog/{self.agent}/{window}", {})
        self.say(f"{window}: inbox signal {got}{'' if fresh else ' again'}; only read_messages acknowledges delivery")

    def handle_events(self, events):
        """The pending events of one poll. An event that fails stays pending, and the next poll tries it again: some
        wait there on purpose, such as a Stop until the task's processes have ended. A task event that failed waits
        longer before its next try (15 seconds, doubled each time up to 5 minutes), because each start of its session
        can ask GitHub for new tokens and the vault for the session's keys.

        The messages of the poll that want a signal are told after its last event, one signal for each window, so
        that the signal knows every message it tells of (signal). A window with none pending forgets what it was told:
        its next message is a new one."""
        pending = {ev["id"] for ev in events}
        self.start_waits = {i: w for i, w in self.start_waits.items() if i in pending}
        self.wanted = {}
        for ev in events:
            wait, due = self.start_waits.get(ev["id"], (0, 0))
            if time.monotonic() < due:
                continue
            try:
                self.handle(ev)
                self.start_waits.pop(ev["id"], None)
            except Exception as e:
                if ev.get("kind") == "task":
                    wait = min(300, wait * 2 or 15)
                    self.start_waits[ev["id"]] = (wait, time.monotonic() + wait)
                self.say(f"event {ev['id']} remains pending: {type(e).__name__}" +
                         (f"; next try in {wait} s" if ev.get("kind") == "task" else ""))
        for window in [w for w in self.told if w not in self.wanted]:
            self.told.pop(window)
            self.retold.pop(window, None)
        for window, ids in self.wanted.items():
            try:
                self.signal(window, ids)
            except Exception as e:      # the messages stay pending in the hub, and the next poll tells them
                self.say(f"{window}: inbox signal failed: {type(e).__name__}")

    def handle(self, ev):
        kind, task = ev["kind"], ev.get("task")
        extra = ev.get("extra") or {}
        if kind == "chat":
            if not self.worker or not extra.get("generation"):
                raise Refused("invalid owner chat event")
            window = "owner-chat"
            expected = f"{extra['generation']}:1"
            if self.other_runtime_windows():         # the hub holds chats until the old runtime's chat is closed
                raise Refused("the owner chat of the previous runtime is still open")
            if window in self.look():
                existing = tmux("show-options", "-wv", "-t", f"{self.session}:={window}", "@fleet_attempt").stdout.strip()
                if existing != expected:
                    tmux("kill-window", "-t", f"{self.session}:={window}", check=True)
            if window not in self.look():
                runtime = self.conf.get("runtime")
                resume = self.chat_resume(extra["generation"])      # first: a chat that cannot continue gets no credential
                token = session_credential(self.agent, "chat", extra["generation"])
                marker = None if resume or runtime not in MANAGER_RUNTIMES else str(uuid.uuid4())
                # No conversation to continue here: a first chat, or the first after a runtime switch, because the
                # record is this runtime's own. The hub's record of what was said takes the place of the session's
                # own memory, so the chat does not start blind. A chat that resumes keeps its own context instead.
                recap = "" if resume else chat_recap(self.agent)
                self.start(window, chat_command(self.agent, self.conf, extra["generation"], token=token, marker=marker,
                                                resume=resume, recap=recap,
                                                env=session_env(self.agent, self.conf, "chat")),
                           1, extra["generation"])
                if marker:      # Codex finds its thread by the marker later; Claude starts with the marker as session id
                    save_chat_record(self.agent, {"generation": extra["generation"],
                                                  "marker": marker if runtime == "codex" else None,
                                                  "session": None if runtime == "codex" else marker,
                                                  "started": time.time()}, runtime)
                self.say(f"owner-chat: {'conversation ' + resume + ' continued' if resume else 'session started'}"
                         f"{' with the hub\'s recap of this chat' if recap else ''}; "
                         f"agent read and reply are still pending")
            else:
                self.want(window, ev["id"])
        elif kind == "conversation":
            # CHAT-APPS.md 7.2: a locked window for one person, with the prompt, the tools and the credential that
            # the hub gives out. Nothing of this agent's machine and nothing of the person's text reaches the start.
            cid = str(extra.get("conversation") or "")
            if not self.worker or not CONVERSATION_ID.fullmatch(cid) or not extra.get("generation"):
                raise Refused("invalid conversation event")
            window, expected = f"C-{cid}", f"{extra['generation']}:{cid}"
            if window in self.look():
                existing = tmux("show-options", "-wv", "-t", f"{self.session}:={window}", "@fleet_attempt").stdout.strip()
                if existing != expected:
                    tmux("kill-window", "-t", f"{self.session}:={window}", check=True)
            if window not in self.look():
                if len([w for w in self.look() if CONVERSATION_WINDOW.fullmatch(w)]) >= CONVERSATION_WINDOWS:
                    raise Refused(f"{CONVERSATION_WINDOWS} conversation windows are open; {window} waits for one")
                code, data = http("POST", f"/helper/conversations/{self.agent}/{cid}/start", {})
                if code != 200:
                    raise Refused(f"the hub did not give the start of {window} ({code}): {data.get('error', '')}")
                self.start(window, conversation_command(self.agent, self.conf, extra["generation"], cid,
                                                        data.get("url") or HUB + str(data.get("path") or ""),
                                                        data.get("token"), data.get("prompt")),
                           cid, extra["generation"])
                self.conversation_seen[window] = time.monotonic()
                self.say(f"{window}: conversation session started; the agent's read and reply are still pending")
            else:
                self.signal(window, [ev["id"]])     # now: the hub takes this event as delivered after this call
        elif kind == "task":
            expected = f"{extra.get('generation','')}:{extra.get('attempt',1)}"
            needs = extra.get("needs") or "none"
            if task in self.look():
                existing = tmux("show-options", "-wv", "-t", f"{self.session}:={task}", "@fleet_attempt").stdout.strip()
                if existing != expected:
                    tmux("kill-window", "-t", f"{self.session}:={task}", check=True)
            if task not in self.look() and app_server_task(self.conf, needs) and \
                    self.screen_attempts.get(task, ("",))[0] == expected:
                # The runner of this attempt ended: it stops itself after an error that a new turn cannot
                # correct. The same start would fail in the same way, and the task event stays unread, so it comes
                # at every poll. The helper starts an attempt one time: the hub holds the task or makes a new
                # attempt, which has another number. A new helper process starts a pending attempt once more.
                if not self.screen_attempts[task][1]:
                    self.screen_attempts[task] = (expected, True)
                    self.say(f"{task}: the screen runner of attempt {extra.get('attempt',1)} ended; "
                             f"the helper does not start the same attempt again")
            elif task not in self.look():
                session = str(uuid.uuid4())     # a new id for every start, so a repeated start never reuses one
                token = session_credential(self.agent, "task", extra.get("generation", ""), task=task,
                                           attempt=extra.get("attempt", 1))
                self.start(task, lambda: session_command(self.agent, self.conf, ev["text"], task=task,
                    needs=needs, attempt=extra.get("attempt",1), generation=extra.get("generation",""),
                    session=session, token=token, env=session_env(self.agent, self.conf, "task")),
                    extra.get("attempt",1), extra.get("generation",""), session, needs=needs)
                self.say(f"{task}: session started, attempt {extra.get('attempt',1)}")
            else:
                self.want(task, ev["id"])
        elif kind == "say":
            if not self.worker:
                self.want("manager", ev["id"])
            elif task:
                self.want(task, ev["id"])
            else:
                # A message the hub could file under no task: a result or a cancellation told to the requester, a
                # duty given to its holder, a permission somebody decided, a question that has waited. For a
                # worker or a lead that is every open task session of this agent, because `read_messages` gives it
                # every unread message whichever task the call names, and a `C-<id>` window is one person's and
                # gets nothing of the fleet. With no session open nothing is typed and nothing is lost: the hub
                # keeps the message until a session reads it. Until 19 October 2026 this signalled a window named
                # None, which raised TypeError at every poll for as long as the message stayed unread — so a lead
                # was never told that its subtask had ended, and its helper's log filled with the same line.
                for window in [w for w in self.look() if re.fullmatch(r"T-\d+", w)]:
                    self.want(window, ev["id"])
        elif kind == "stop":
            # Owner API 1.12: for a browser or screen task this acknowledgement frees the machine's desktop for the
            # next one, so nothing that the task started may still run: the cleanup of a confirmed Stop, with the
            # task's processes taken before the window closes (a macOS child whose environment is hidden loses its
            # parent link when its window closes).
            desktop, generation = extra.get("needs") in ("browser", "screen"), str(extra.get("generation") or "")
            before = self.task_work(task, generation, keep_runtime=False) if desktop and generation else {}
            if task in self.look():
                tmux("kill-window", "-t", f"{self.session}:={task}", check=True)
                self.say(f"{task}: finished, session closed")
            if desktop and generation:
                quiet, ended, left = self.quiesce(task, generation, keep_runtime=False, also=before)
                if not quiet:
                    raise Refused(f"processes of {task} still run: {left}")
                if ended:
                    self.say(f"{task}: ended {ended} process(es) that the finished task left")
        elif kind in ("signin", "handover") or kind in SAVES:
            self.login_job(kind, task, extra)
        elif kind == "request":
            self.request_job(task, extra)
        elif kind == "control":
            self.control(task, extra)
        elif kind == "manager_start":
            # Owner feature manager_start: the control that the hub's manager_down warning carries. The loop's own
            # watch starts a missing session by itself (manager_gone); this is the owner's hand on the same switch,
            # for a start that keeps failing and for the round he does not want to wait for. It only makes the start
            # due: start_manager reads the windows under the start lock, so a session that runs is left alone.
            if self.conf.get("role") == "manager":
                self.manager_due, self.manager_tried = True, None
                self.say("manager: the owner asked for its session; it starts at this round")
        elif kind == "update":
            if getattr(self, "lifecycle", None):
                self.lifecycle.request(extra)
        elif kind == "check":
            if getattr(self, "lifecycle", None):     # its own thread: a security scan can take seconds
                threading.Thread(target=self.lifecycle.check, args=(extra,), daemon=True, name="machine-check").start()
        elif kind == "login":           # owner feature machine_signin: it waits for the owner, so its own thread
            threading.Thread(target=self.runtime_login, args=(extra,), daemon=True, name="runtime-login").start()
        elif kind == "trigger_check":
            self.trigger_check(extra)
        elif kind == "mcp_program":
            self.mcp_program(extra)
        if kind not in ("say", "task", "chat"):
            code, data = http("POST", f"/helper/ack/{self.agent}", {"ids": [ev["id"]]})
            if code != 200:
                raise Refused("the hub did not acknowledge helper work")

    def mcp_program(self, extra):
        """The owner asked, in the app, whether this machine has the program of a command MCP server (owner feature
        mcp_program, the hub's POST /ui/mcp/check). The helper resolves the name exactly as the start of a task
        session resolves it — this machine's PATH, then ~/.local/bin — and never runs the program. The owner waits
        for this answer, so it goes back at once and in this loop."""
        probe, command = extra.get("probe"), extra.get("command")
        if not (isinstance(probe, str) and isinstance(command, str) and MCP_PROGRAM.fullmatch(command)):
            raise Refused("invalid MCP program check")
        found = exe(command)
        self.say(f"MCP server program {command}: " + (f"this machine has it at {found}" if found else
                                                      "not on this machine's PATH or in ~/.local/bin"))
        code, _ = http("POST", f"/helper/mcp/program/{self.agent}",
                       {"probe": probe, "found": bool(found), "path": found}, timeout=10)
        if code != 200:
            raise Refused(f"the hub did not take the answer about {command} ({code})")

    def runtime_login(self, extra):
        """The owner asked, in the app, for this machine's runtime to be signed in (machine_signin). The provider's
        own flow runs in a window of its own here; the owner authenticates in their own browser and the code they get
        comes back through the hub, which keeps it nowhere. This types it in, waits for the runtime to say it is
        signed in, and takes the window down. The code is never printed, logged or written to a file, and no token
        ever passes through Fleet."""
        lid, runtime = extra.get("login"), str(extra.get("runtime") or "")
        session = LOGIN_SESSION % self.agent

        def report(state, **fields):
            code, data = http("POST", f"/helper/login/{self.agent}/{lid}", dict({"state": state}, **fields))
            if code != 200:
                raise Refused(f"the hub did not take the sign-in report ({code}): {data.get('error', '')}")

        self.say(f"sign-in L-{lid}: {runtime}")
        try:
            program = exe(runtime, os.path.expanduser("~")) if runtime in LOGIN_URLS else None
            if not program:
                return report("failed", detail=f"{runtime} is not installed on this machine.")
            if not getattr(self, "lifecycle", None):
                return report("failed", detail="This machine's helper is still starting. Try again in a minute.")
            try:
                command = runtime_login_command(runtime, program)
            except Refused as e:
                return report("failed", detail=str(e))
            label = LOGIN_LABELS.get(runtime, runtime)
            # **The first half of a different account.** No provider starts a second sign-in while one is live, so
            # the owner's "Use another account" signs this runtime out first, in the same job and the same window
            # session. Nothing of the old credential is read here: the runtime's own sign-out removes it, and this
            # only waits for the runtime to say it is gone.
            if extra.get("replace") and self.signed_in(runtime):
                try:
                    out_command = runtime_logout_command(runtime, program)
                except Refused as e:
                    return report("failed", detail=str(e))
                tmux("kill-session", "-t", session)
                ensure_session(session)
                start_window(session, "logout", out_command)
                deadline = time.monotonic() + LOGOUT_WAIT
                while time.monotonic() < deadline and self.signed_in(runtime):
                    time.sleep(3)
                tmux("kill-session", "-t", session)
                if self.signed_in(runtime):
                    return report("failed", detail=f"{label} did not sign out on this machine, so its account was "
                                                   f"not changed. Sign out there yourself, then sign in.")
                report("starting", detail=f"{label} is signed out. The sign-in starts now.")
            # **The runtime's own first screen, and after the sign-out, not before it.** Signing Claude Code out
            # does not leave it signed out and ready: it puts its whole first run back, so the answers written
            # before the sign-out go with the account and the sign-in opens behind a theme picker. This call is
            # here, once, for both halves; it says so when it had to write them again.
            if ready_runtime_config():
                self.say(f"sign-in L-{lid}: wrote {label}'s own first-run answers again")
            tmux("kill-session", "-t", session)          # a sign-in that was given up leaves no window behind
            ensure_session(session)
            start_window(session, "login", command)
            url = shown = None
            asked, answered, seen = LOGIN_SETUP_CURSOR.get(runtime), [], None
            deadline = time.monotonic() + LOGIN_URL_WAIT
            while time.monotonic() < deadline and not url:
                time.sleep(2)
                screen = login_screen(session)
                url = whole_link(screen, LOGIN_URLS[runtime])
                if url:
                    if runtime in LOGIN_SHOWN_CODE:
                        code = LOGIN_SHOWN_CODE[runtime].search(screen)
                        shown = code.group(0) if code else None
                    break
                # **A first-run question of the runtime's own, standing in front of the provider's link.** Fleet
                # takes the option the runtime itself has selected, which is what a person pressing through its
                # first run would get, and it does so only while a question is really waiting: a window showing
                # its selection cursor and no link yet. It is bounded, and it presses again only once the screen
                # has changed, so a key that changes nothing stops it instead of being sent for ninety seconds.
                here = " ".join(screen.split())
                if asked and len(answered) < LOGIN_SETUP_ANSWERS and asked.search(screen) and here != seen:
                    seen, asking = here, login_prompt_line(screen)
                    answered.append(asking)
                    self.say(f"sign-in L-{lid}: answering {label}'s own first-run question")
                    tmux("send-keys", "-t", f"{session}:=login", "Enter")
            if not url:
                # Which screen, and not "printed no link": the one thing the owner could act on.
                waiting = login_prompt_line(login_screen(session))
                detail = f"{label} printed no sign-in link in {LOGIN_URL_WAIT} seconds. "
                detail += f'Its window is waiting at "{waiting}".' if waiting else \
                    "Its window showed nothing Founderizer could read."
                if answered:
                    detail += f" Founderizer answered {len(answered)} of its own first-run questions first."
                return report("failed", detail=detail + " Try again.")
            wants = runtime in LOGIN_WANTS_CODE
            report("needs_owner", url=url, shows_code=shown, wants_code=wants)
            if wants and not self.await_login_code(lid, session):
                return
            deadline = time.monotonic() + LOGIN_DONE_WAIT
            while time.monotonic() < deadline:
                if self.signed_in(runtime):
                    self.restart_after_login()
                    return report("done", detail=f"{label} is signed in on this machine.")
                time.sleep(5)
            report("failed", detail=f"{label} did not report itself signed in. Try again.")
        except Refused as e:
            self.say(f"sign-in L-{lid}: {e}")
        except Exception as e:          # never pass the detail on: the screen of that window could hold the code
            self.say(f"sign-in L-{lid}: the helper hit an error ({type(e).__name__})")
            try:
                http("POST", f"/helper/login/{self.agent}/{lid}",
                     {"state": "failed", "detail": "The machine hit an error during the sign-in. Try again."})
            except Exception:
                pass
        finally:
            tmux("kill-session", "-t", session)

    def restart_for_tools(self):
        """**A new team tool reaches the standing session only when it starts.** The hub hands a session its tool
        list once, at `initialize`, and announces no change of it, so a worker gets a new tool with its next task
        and the manager — whose session always stands — would keep the list it started with for as long as it runs.
        The owner asked how an agent gets an update of its tools by itself (21 October 2026).

        So when the hub says that list is no longer the one it would serve, this closes that session and its own
        watch starts it again within ten seconds, on the same conversation (`--resume`): the manager is away for
        the seconds of one restart, and only when there is something new for it to pick up. It waits while a turn
        is running and while a switch is closing or starting that session, and the hub keeps saying it until the
        new session has asked for the list."""
        if self.conf.get("role") != "manager" or not self.lifecycle.tools_changed:
            return
        if self.manager_due or self.manager_left or self.manager_retiring is not None or self.outdated():
            return                      # a start or a switch is already going to give it the current list
        target = f"{self.session}:=manager"
        if self.window_runtime("manager") is None or self.working(target):
            return                      # nothing to close, or a turn runs: at the next round
        with contextlib.suppress(subprocess.CalledProcessError, Refused):
            tmux("kill-window", "-t", target)
        self.lifecycle.tools_changed = False
        self.manager_due = True
        self.say("manager: its team tools changed, so its session starts again on the same conversation")

    def restart_fresh(self, target):
        """True when this closed the standing Claude manager session so that it starts again on a new conversation,
        with the hub's brief, instead of being told of its messages (signal): its watch starts it within ten
        seconds, and the next poll tells the new session. Between two turns only, and for one of two reasons:

        - It is cold. Its last model call is older than the provider's cache (CACHE_SECONDS, or what its own records
          say), and its context has grown FRESH_GROWN past the start of its conversation, so telling it would read
          all of that again at the price of writing it, where a new session pays only for a start. Nothing is lost
          that a read cannot give back: the fleet's state is in the hub, and the brief opens with it.
        - It started without the compaction window (STANDING_COMPACT), before this helper had it: one time.

        A signal that this helper typed less than one cache lifetime ago counts as a model call, so a conversation file
        that is not where it should be never makes a session that is still cached look cold. A start does not: a
        session that continues its conversation makes no call until it is told of a message."""
        if self.conf.get("role") != "manager" or self.conf.get("runtime") != "claude":
            return False
        if self.manager_due or self.manager_left or self.manager_retiring is not None or self.outdated():
            return False                # a start or a switch is already going to replace this session
        if self.fresh_at is not None and time.monotonic() - self.fresh_at < FRESH_SPACING:
            return False
        if self.window_runtime("manager") != "claude":
            return False
        r = tmux("display-message", "-p", "-t", target, "#{@fleet_session}\t#{pane_start_command}")
        screen = tmux("capture-pane", "-p", "-t", target)
        if r.returncode != 0 or screen.returncode != 0 or shows_question(screen.stdout) or \
                WORKING.search("\n".join(screen.stdout.rstrip().split("\n")[-15:])):
            return False                # unknown, a question waits on its screen, or a turn runs: not now
        session, _, start = r.stdout.rstrip("\n").partition("\t")
        if start.strip() and "CLAUDE_CODE_AUTO_COMPACT_WINDOW=" not in start and not self.compact_restarted:
            self.compact_restarted = True       # one time for this helper, whatever the new start says
            why = f"it started without the {STANDING_COMPACT}-token compaction window"
        else:
            path = claude_chat_file(session if THREAD_ID.fullmatch(session) else self.manager_session)
            seen = claude_context(path) if path else None
            if not seen:
                return False
            tokens, first, at, cached = seen
            idle = min(time.time() - at, time.monotonic() - self.typed_at.get("manager", float("-inf")))
            if idle < cached or tokens - first < FRESH_GROWN:
                return False
            why = (f"its {tokens // 1000}K tokens of context are no longer cached after {idle / 60:.0f} quiet "
                   f"minutes, and a new session costs only its start of about {first // 1000}K")
        with contextlib.suppress(subprocess.CalledProcessError, Refused):
            tmux("kill-window", "-t", target)
        self.fresh_at, self.manager_due, self.manager_fresh = time.monotonic(), True, True
        self.say(f"manager: {why}, so its session starts again on a new conversation with the hub's brief")
        return True

    def restart_after_login(self):
        """A session that started before this machine had a credential is sitting at the provider's own sign-in
        screen, and no credential reaches a window that already runs. So the standing manager session is closed
        here and started again, signed in. A worker has no standing session: its next task window is a new one."""
        if self.conf.get("role") != "manager":
            return
        with contextlib.suppress(subprocess.CalledProcessError, Refused):
            tmux("kill-window", "-t", f"{self.session}:=manager")
        self.manager_due = True
        self.say("manager: signed in, so its session starts again")

    def await_login_code(self, lid, session):
        """Wait for the one code the owner pasted in the app, then type it into the sign-in window. True when it was
        typed. The code lives in this call and nowhere else."""
        deadline = time.monotonic() + LOGIN_CODE_WAIT
        while time.monotonic() < deadline:
            status, data = http("GET", f"/helper/login/{self.agent}/{lid}")
            if status != 200 or data.get("stop"):
                self.say(f"sign-in L-{lid}: the hub ended it ({data.get('why', status)})")
                return False
            code = data.get("code")
            if code:
                try:
                    type_login_code(session, code)
                finally:
                    del code
                return True
            time.sleep(2)
        http("POST", f"/helper/login/{self.agent}/{lid}",
             {"state": "failed", "detail": "No code came back in 14 minutes. Start the sign-in again."})
        return False

    def signed_in(self, runtime):
        """What this machine's runtime says about itself now: the runtime's own status command, no AI turn."""
        return self.lifecycle.runtime(runtime).get("signed_in") is True

    def trigger_check(self, extra):
        """A check run from the hub (owner API 1.8), in its own thread and outside every AI session."""
        need = ("trigger", "run", "token", "expires", "sha256", "timeout_seconds") + \
            (() if extra.get("code") == "saved" else ("path", "commit"))
        if not all(k in extra for k in need):
            raise Refused("invalid check event")
        if time.time() > float(extra["expires"]):
            self.say(f"check run {extra['run']}: its claim expired before it arrived")
            return
        if not CHECK_SLOTS.acquire(blocking=False):
            busy = {"ok": False, "error": "busy", "detail": "four checks already run on this machine", "elapsed_ms": 0}
            threading.Thread(target=self.post_check, args=(extra, busy), daemon=True).start()
            return
        try:
            self.begin_outside()        # no Fleet update restarts the helper until the result is sent
        except Exception:
            CHECK_SLOTS.release()
            raise
        threading.Thread(target=self.check_thread, args=(extra,), daemon=True, name=f"check-{extra['run']}").start()

    def check_thread(self, extra):
        try:
            try:
                result = run_check(extra, agent=self.agent)
            except Exception as e:
                result = {"ok": False, "error": "exit", "detail": f"the runner failed: {type(e).__name__}", "elapsed_ms": 0}
            finally:
                CHECK_SLOTS.release()
            self.post_check(extra, result)
        finally:
            self.end_outside()

    def begin_outside(self):
        """A check or a login job starts. They run in this process outside every task window, so no Fleet update may
        restart the helper until they end (the update's idle check counts them). Like a session start, it never waits
        for an update: during one it raises Updating, and the event stays pending for the restarted helper."""
        lifecycle = getattr(self, "lifecycle", None)
        with lifecycle.session_start() if lifecycle else contextlib.nullcontext():
            with self.outside_lock:
                self.outside += 1

    def end_outside(self):
        with self.outside_lock:
            self.outside -= 1

    def post_check(self, extra, result):
        """Send a check result. Without an answer, send the same body again, at most three times, until the claim
        expires."""
        body = dict(result, token=extra["token"])
        for pause in (0, 5, 15, 30):
            time.sleep(pause)
            if pause and time.time() > float(extra["expires"]):
                break
            try:
                code, data = http("POST", f"/helper/trigger/{self.agent}/{extra['run']}", body, timeout=15)
            except (urllib.error.URLError, OSError):
                continue
            if code in (200, 409):
                self.say(f"check run {extra['run']}: {data.get('decision') or data.get('error')}")
                return
        self.say(f"check run {extra['run']}: the hub took no result")

    def task_windows(self):
        """(window, generation, attempt, session id, runtime, needs) for each task session started with a session id.
        A task in the runner (app_server_task) writes its own record file of Codex app-server notifications
        ("codex_app"); the other windows have this agent's runtime (None). The record reader takes its requested
        settings from needs: the screen runtime's for a screen task, the primary runtime's for every other task."""
        r = tmux("list-windows", "-t", self.session, "-F",
                 "#{window_name}\t#{@fleet_attempt}\t#{@fleet_session}\t#{@fleet_needs}")
        found = []
        for line in r.stdout.splitlines() if r.returncode == 0 else []:
            name, attempt, session, needs = (line.split("\t") + ["", "", ""])[:4]
            generation, _, number = attempt.rpartition(":")
            if re.fullmatch(r"T-\d+", name) and re.fullmatch(r"[0-9a-f-]{36}", session) and number.isdigit():
                found.append((name, generation, int(number), session,
                              "codex_app" if app_server_task(self.conf, needs) else None, needs))
        return found

    def manager_windows(self):
        """The manager session, by its session id; a session started before session ids is found by its prompt."""
        r = tmux("list-windows", "-t", self.session, "-F", "#{window_name}\t#{@fleet_session}")
        rows = dict(line.split("\t", 1) for line in r.stdout.splitlines() if "\t" in line) if r.returncode == 0 else {}
        if "manager" not in rows:
            self.manager_session = None
            return []
        if re.fullmatch(r"[0-9a-f-]{36}", rows["manager"]):
            self.manager_session = rows["manager"]
        elif not self.manager_session:
            self.manager_session = manager_transcript(os.path.expanduser("~"), KIT, MANAGER_START.format(agent=self.agent, resume=""))
        return [("manager", "", 0, self.manager_session)] if self.manager_session else []

    def report_limit(self, batch):
        """The manager's own records stay on its machine. Only a fresh usage-limit record goes to the hub."""
        for r in batch["events"]:
            if r["type"] == "error" and r.get("code") == "usage_limit" and r.get("ts") and time.time() - r["ts"] < 900:
                code, _ = http("POST", f"/helper/limit/{self.agent}",      # the hub holds only its runtime's limit
                               {"resets_at": r.get("resets_at"), "runtime": self.conf.get("runtime")}, timeout=10)
                if code != 200:
                    return code, {}
                self.say(f"manager: usage limit reported to the hub, reset {r.get('resets_at')}")
        return 200, {"last_seq": max([0] + [r["seq"] for r in batch["events"]])}

    def runner(self, target):
        """True for a task window in the runner (app_server_task: every screen task, and each task of a Codex agent
        with the app-server start policy), which runs screen_runner.py instead of a terminal CLI. The window's
        @fleet_needs decides; a window started before that option existed is judged by its launch argv: after env
        and its NAME=value words, the program's first argument must be the runner script. A Claude task's settings
        and prompt can name the file too, so no substring decides."""
        r = tmux("show-options", "-wv", "-t", target, "@fleet_needs")
        needs = r.stdout.strip() if r.returncode == 0 else ""
        if needs and app_server_task(self.conf, needs):
            return True
        if needs and runtime_fact(self.conf.get("runtime"), "driver") == "terminal":
            return False                    # a terminal runtime's task window runs its own CLI
        try:
            words = launch_words(tmux("display-message", "-p", "-t", target, "#{pane_start_command}").stdout)
        except ValueError:
            return False
        return len(words) >= 2 and os.path.basename(words[1]) in ("screen_runner.py", "acp_runner.py")

    def working(self, target):
        """Both Claude Code and Codex show "esc to interrupt" while a turn runs."""
        r = tmux("capture-pane", "-p", "-t", target)
        return r.returncode == 0 and bool(WORKING.search("\n".join(r.stdout.rstrip().split("\n")[-15:])))

    def window_runtime(self, window):
        """The program that a window started (codex, claude, ...), from its launch argv; "" when it cannot be read,
        None when the window is not there. The window list decides that: tmux can answer a display-message for a
        missing window with exit status 0 and no output."""
        if window not in self.look():
            return None
        r = tmux("display-message", "-p", "-t", f"{self.session}:={window}", "#{pane_start_command}")
        try:
            words = launch_words(r.stdout) if r.returncode == 0 else []
        except ValueError:
            words = []
        if not words and window not in self.look():         # it closed meanwhile
            return None
        return os.path.basename(words[0]) if words else ""

    def window_generation(self, window):
        return tmux("show-options", "-wv", "-t", f"{self.session}:={window}", "@fleet_attempt").stdout.strip().rpartition(":")[0]

    def chat_binding(self, runtime, generation):
        """The conversation file that this generation's owner chat on one runtime is recorded with, or None: then a
        switch back could not continue it (owner feature runtime_switch)."""
        try:
            record = load_chat_record(self.agent, runtime)
        except Refused:
            return None
        if not record or record.get("generation") != generation:
            return None
        if runtime == "codex":
            thread = chat_thread(record)
            paths = codex_threads(thread) if thread else []
            return paths[0] if len(paths) == 1 else None
        return claude_chat_file(record.get("session")) if runtime == "claude" else None

    def other_runtime_windows(self, window=UNREAD):
        """["owner-chat"] while the owner chat window does not certainly run the agent's runtime (another runtime, or
        a window whose program cannot be read), or while processes of a closed chat of the previous runtime still run
        (owner feature runtime_switch); for the manager also "manager" in the same cases for its standing session, or
        while no window list can be read. No task window can: the helper takes an update only when it has none.
        window: the reading of the standing window to use (manager_state); without one it is read here."""
        runtime = self.conf.get("runtime")
        other = ["owner-chat"] if self.retired_left or self.window_runtime("owner-chat") not in (None, runtime) else []
        if self.conf.get("role") == "manager":
            window = self.manager_state() if window is UNREAD else window
            # Also a previous runtime's session that this helper saw and has not handled since it went: until then
            # its processes are not known to have ended.
            if self.manager_left or self.manager_seen_old is not None or (window is not None and window[1] != runtime):
                other.append("manager")
        return other

    def switch_state(self):
        """The helper's part of a runtime switch in its status: the windows of the previous runtime, and whether the
        open owner chat (None without one) runs this runtime with a recorded conversation, which a switch needs so that
        a switch back continues it; for the manager also the same of its standing session (manager_bound)."""
        # One reading of the standing window for both of its fields: two readings could disagree (no window, then no
        # answer), and the hub would take that for the new runtime's session.
        window = self.manager_state() if self.conf.get("role") == "manager" else None
        other, runtime = self.other_runtime_windows(window), self.window_runtime("owner-chat")
        bound = None if runtime is None else runtime == self.conf.get("runtime") and \
            self.chat_binding(runtime, self.window_generation("owner-chat")) is not None
        state = {"other_runtime_windows": other, "owner_chat_bound": bound}
        if self.conf.get("role") == "manager":
            state["manager_bound"] = self.manager_bound(window)
            # Read after the window list: a report that shows the new runtime's session also has the hand-over that
            # its start saved before it started that session (start_handover).
            state["manager_handover"] = load_handover(self.agent)
        return state

    def post_status(self, status):
        """Send one status report (Lifecycle.report). On the hub's answer handover_done the helper forgets the
        hand-over that the report gave (manager_handover), unless a later start saved another one meanwhile."""
        code, data = http("POST", f"/helper/status/{self.agent}", status, timeout=15)
        handover = status.get("manager_handover")
        if code == 200 and isinstance(data, dict) and data.get("handover_done") is True and handover and \
                load_handover(self.agent) == handover:
            save_handover(self.agent, {})
        return code, data

    def keep_procs(self, session, runtime, keeps, procs):
        """Add the processes of the previous runtime's window `session` (runtime, and what its close keeps:
        manager_keeps) to the snapshot, and save it (save_seen)."""
        merged, seen = {**self.manager_procs, **procs}, (session, runtime, keeps)
        if merged != self.manager_procs or seen != self.manager_snap:
            self.manager_procs, self.manager_snap = merged, seen
            save_seen(self.agent, seen, merged)

    def drop_procs(self):
        """Forget the snapshot: its session's processes are in the closing file now, or no such session runs."""
        if self.manager_procs or self.manager_snap is not None:
            self.manager_procs, self.manager_snap = {}, None
            save_seen(self.agent, None, {})

    def unbegun_record(self, window):
        """The record of the standing session of this helper's runtime at this reading (also of the runtime that
        team.json gives while this helper waits for its update: a start by `fleet.py start`). A conversation that
        began is noted in it (mark_begun). A Claude start that has not begun one is remembered: when the owner closes
        it, its record would stop every later Claude start, also a switch back, which finds no conversation to
        continue, so it is removed then (forget_record keeps one that began). A note that a busy lock kept back is
        written at a later reading, also after the window went; every such step waits, and the window that runs is
        read also while they wait. False when a start holds the lock: read again at the next round."""
        pending, free = self.manager_seen_own, True
        gone = [key for key in pending if window is None or window[0] != key[1]]   # closed, or replaced
        if gone and self.manager_lock is None:
            try:
                self.manager_lock = hold_manager_lock(self.agent)
            except Refused:
                free = False            # a start holds the lock: these steps wait for a later round
        for runtime, session in gone if free else ():
            if pending.pop((runtime, session)):
                note_begun(self.agent, runtime, session)            # its file was found: it began
            elif self.forget_record(runtime, session):
                self.say("manager: its session ended before it began a conversation; its record is removed")
        if window is not None and window[1] in MANAGER_RUNTIMES and \
                window[1] in (self.conf.get("runtime"), team_runtime(self.agent)):
            key, keeps = (window[1], window[0]), self.manager_keeps(window[1], window[0])
            if keeps == "" and window[1] == "claude":
                pending[key] = keeps                                # its conversation has not begun yet
            elif keeps and not self.mark_begun(window[1], window[0]):
                pending[key] = keeps                                # a start holds the lock: noted later
            else:
                pending.pop(key, None)
        return free

    def mark_begun(self, runtime, window):
        """note_begun for the conversation that runs `window`, whose file this reading found, so that a conversation
        that began is never replaced without notice (manager_conversation): under the start lock, at once when this
        helper holds it or can take it. False when a start holds it: the caller notes it at a later reading."""
        try:
            if not unnoted(load_chat_record(self.agent, runtime, "manager"), runtime, window):
                return True
        except Refused:
            return True                 # a record that cannot be read: nothing to note in it
        try:
            lock = hold_manager_lock(self.agent) if self.manager_lock is None else None
        except Refused:
            return False
        try:
            note_begun(self.agent, runtime, window)     # it reads the record again, under the lock
        finally:
            if lock is not None:
                lock.close()
        return True

    def keep_outdated_session(self):
        """While this helper waits for its update to another runtime, the session that it runs becomes the previous
        runtime's: its processes are kept while it runs, and when it goes (closed by the owner, or replaced by
        `fleet.py start` on the new runtime), what it left goes to the closing file, which the updated helper ends
        before any start (save_closing). Nothing is closed or started here."""
        try:
            window = self.manager_reading()
        except Refused:
            return                      # unknown: nothing changes
        own = self.manager_own if self.manager_snap is None else None
        if own:     # its processes at the last round before this helper knew of the switch
            self.keep_procs(own[0], self.conf.get("runtime"), self.manager_keeps(self.conf.get("runtime"), own[0]),
                            own[1])
        self.manager_own = None
        self.unbegun_record(window)
        if window is not None and window[1] == self.conf.get("runtime"):
            self.keep_procs(window[0], window[1], self.manager_keeps(window[1], window[0]),
                            self.task_work("manager", "", keep_runtime=False))
            return
        new = self.task_work("manager", "", keep_runtime=False) if window is not None else {}
        left = {pid: args for pid, args in still_running(self.manager_procs).items() if pid not in new}
        if left:        # the closing file first: a restart right after it still knows them
            self.manager_left, self.manager_left_start = left, window is None
            save_closing(self.agent, left, window is None)
        self.drop_procs()

    def manager_reading(self):
        """manager_window of this agent's tmux session. A session that tmux says it does not have has no window: tmux
        ends a session with its last window, which can be the standing one. Refused when tmux gives no answer."""
        try:
            return manager_window(self.session)
        except Refused:
            if self.started() == {}:
                return None
            raise

    def manager_state(self):
        """One reading of the standing window for the status (manager_reading), where no answer from tmux counts as a
        window whose program cannot be read: ("", "")."""
        try:
            return self.manager_reading()
        except Refused:
            return ("", "")

    def manager_first(self):
        """At this helper's start: True when the standing session's start is due, with no window, or no answer from
        tmux (the start reads again under its lock); never while saved processes of a session that `fleet.py start`
        replaced remain (closing_start), whose own window is then the standing session. A window of the previous
        runtime counts as seen (manager_seen_old), so that its end also before this helper's first round starts the
        new runtime's session. Once no window is there at all, the loop's own watch makes the start due at a later
        round (manager_gone)."""
        try:
            first = self.manager_reading()
        except Refused:
            if self.manager_snap is not None:
                # A saved snapshot of the previous runtime's session (load_seen): nothing starts before a reading
                # shows whether that session still runs; retire_old_manager handles it then, as a seen window.
                session, runtime, keeps = self.manager_snap
                self.manager_seen_old = (runtime, session, keeps)
                return False
            return self.manager_left_start
        if first and first[1] and first[1] != self.conf.get("runtime"):
            # As a reading of retire_old_manager: what its close keeps, and its processes in case it goes by hand.
            keeps = self.manager_keeps(first[1], first[0])
            self.manager_seen_old = (first[1], first[0], keeps)
            self.keep_procs(first[0], first[1], keeps, self.task_work("manager", "", keep_runtime=False))
        elif self.manager_snap is not None and first is not None and first[0] == self.manager_snap[0]:
            self.drop_procs()           # the session that an earlier run of this helper saw runs on, as this runtime's
        elif self.manager_snap is not None:
            # The previous runtime's session that an earlier run of this helper saw (load_seen) went before this
            # start: what it left is ended before any start, as after a close (unless the closing file has them
            # already); and nothing more starts when a session of this runtime runs already (`fleet.py start`). A
            # start of it that never began leaves no record, and a conversation of it that began is noted as such.
            session, runtime, keeps = self.manager_snap
            if keeps is not None:
                try:
                    with hold_manager_lock(self.agent):
                        if keeps:
                            note_begun(self.agent, runtime, session)
                        else:
                            self.forget_record(runtime, session)
                except Refused:         # a start holds the lock: its record gets that step at a later round
                    self.manager_seen_own[(runtime, session)] = keeps
            if not self.manager_left:
                new = self.task_work("manager", "", keep_runtime=False) if first is not None else {}
                left = {pid: args for pid, args in still_running(self.manager_procs).items() if pid not in new}
                if left:    # the closing file first: a restart right after it still knows them
                    self.manager_left, self.manager_left_start = left, first is None
                    save_closing(self.agent, left, first is None)
            self.drop_procs()
        return first is None and self.manager_left_start

    def manager_bound(self, window=UNREAD):
        """None without a standing manager window; True when it runs this agent's runtime and its record names the
        session that runs (manager_keeps): its conversation, or a start that has not begun one yet, which a switch can
        close with nothing lost. False otherwise, also when tmux gives no window list. window: as for
        other_runtime_windows."""
        window = self.manager_state() if window is UNREAD else window
        return None if window is None else window[1] == self.conf.get("runtime") and \
            self.manager_keeps(window[1], window[0]) is not None

    def manager_keeps(self, runtime, window):
        return manager_keeps(self.agent, runtime, window)

    def forget_record(self, runtime, window):
        """Remove the record of a closed start that never began its conversation (manager_keeps ""), while the start
        lock is held: it names nothing to continue. A record of a conversation that began stays (a Codex thread,
        Claude's "begun" not false), and so does one whose conversation file is there after all, which it then notes
        (note_begun). True when it was removed."""
        try:
            record = load_chat_record(self.agent, runtime, "manager")
            if not record or record.get("marker" if runtime == "codex" else "session") != window or \
                    (record.get("session") if runtime == "codex" else record.get("begun") is not False):
                return False            # another session's record, or one whose conversation began
            if manager_binding(self.agent, runtime, window):
                note_begun(self.agent, runtime, window)
                return False            # its conversation began after all
            os.remove(chat_record_file(self.agent, runtime, "manager"))
            return True
        except (Refused, OSError):
            return False

    def release_manager(self, due):
        """End the hold of the start lock (retire_old_manager). due: the previous runtime's session and its processes
        are gone, so the new runtime's session is due; the hub learns that at once."""
        if self.manager_lock is not None:
            self.manager_lock.close()
            self.manager_lock = None
        if due:
            self.lifecycle.report()
        return due

    def retire_old_manager(self):
        """After a runtime switch of the manager (owner feature runtime_switch), close its standing session that still
        runs the previous runtime, as retire_old_chat closes a chat. The start lock is held from the first reading of
        that session until every process of it has ended, so that no start runs meanwhile. It closes only when its
        conversation and record stay for a switch back (manager_keeps), after two readings at least RETIRE_QUIET
        seconds apart with no running turn, no question on its screen, the same screen and the same conversation file
        time; a question counts as answered when it is the runtime's usage-limit menu (limit_menu). True once this
        helper closed it and its processes have ended: then the new runtime's session is due. The session's processes
        are kept in a file from just before the close until they have ended (save_closing), so that this holds across
        a restart of the helper. When the owner closes that session by hand meanwhile, the switch goes on in the same
        way; a session of the agent's own runtime that goes while no switch is open is started again by the loop's
        own watch of the window instead (manager_gone)."""
        if self.conf.get("role") != "manager":
            return False
        if self.manager_left:           # processes of the closed session that did not end yet
            window = UNREAD
            with contextlib.suppress(Refused):  # and those of the agent's own runtime's session, as at its own rounds:
                window = self.manager_reading()     # a switch can come before the next one (keep_outdated_session)
                if window is not None and window[1] == self.conf.get("runtime"):
                    self.manager_own = (window[0], self.task_work("manager", "", keep_runtime=False))
            if self.manager_lock is None:
                with contextlib.suppress(Refused):      # also after a restart of this helper: no start meanwhile
                    self.manager_lock = hold_manager_lock(self.agent)
            if window is not UNREAD:
                self.unbegun_record(window)     # the record of the session that runs, as at its own rounds
            alive = still_running(self.manager_left)     # only these processes: never a later manager window's
            if alive:
                end_processes(list(alive))
            left = still_running(alive)
            if left != self.manager_left:
                save_closing(self.agent, left, self.manager_left_start)
            self.manager_left = left
            if self.manager_left:
                return False
            self.say("manager: the processes of the previous runtime's session have ended")
            due, self.manager_left_start = self.manager_left_start, True
            return self.release_manager(due)
        if self.outdated():
            self.manager_retiring, self.manager_seen_old = None, None
            self.keep_outdated_session()
            return self.release_manager(False)
        try:
            window = self.manager_reading()
        except Refused:
            return False                # unknown: nothing closes, and a held lock stays held
        # Already replaced by a session of the agent's runtime, which the owner's `fleet.py start` started after the
        # previous runtime's window went: the same as a close by hand, but nothing more starts.
        replaced = window is not None and window[1] == self.conf.get("runtime") and self.manager_seen_old is not None \
            and window[0] != self.manager_seen_old[1]       # not the same session, which runs on as this runtime's
        if replaced or (window is None and (self.manager_lock is not None or self.manager_seen_old is not None)):
            # Closed by hand while this helper was closing it, for example to end a question on its screen, or gone
            # after this helper saw it and before it held the lock: the switch goes on as after this helper's own
            # close, also for the processes that its window left.
            if self.manager_lock is None:
                try:
                    self.manager_lock = hold_manager_lock(self.agent)
                except Refused:
                    return False        # a start of the manager session runs now: read again at the next round
            new = self.task_work("manager", "", keep_runtime=False) if replaced else {}
            self.manager_retiring, self.manager_left_start = None, not replaced
            self.manager_left = {pid: args for pid, args in still_running(self.manager_procs).items() if pid not in new}
            seen, self.manager_seen_old = self.manager_seen_old or (None, None, None), None
            if seen[2]:
                note_begun(self.agent, seen[0], seen[1])    # its file was found: a later start that misses it stops
            forgot = seen[2] == "" and self.forget_record(seen[0], seen[1])     # first: no restart keeps the record
            self.unbegun_record(window)         # the record of the session that runs now, if any
            save_closing(self.agent, self.manager_left, self.manager_left_start)
            self.drop_procs()
            self.say("manager: the previous runtime's session " + ("was replaced by `fleet.py start`; " if replaced else
                                                                   "was closed by hand; ") +
                     ("it never began a conversation, so its record is removed; " if forgot else "") +
                     ("its processes still run" if self.manager_left else
                      "the new runtime's session runs" if replaced else "the new runtime's session starts"))
            return False if self.manager_left else self.release_manager(not replaced)
        if window is None or window[1] == self.conf.get("runtime"):
            self.manager_retiring, self.manager_seen_old = None, None
            self.drop_procs()
            # Its processes now, in case a switch makes it the previous runtime's before this helper reads again:
            # the update's wait (keep_outdated_session) starts from them.
            self.manager_own = (window[0], self.task_work("manager", "", keep_runtime=False)) if window else None
            if not self.unbegun_record(window):
                return False            # a start of the manager session runs now: read again at the next round
            return self.release_manager(False)      # no such session any more, or none of the previous runtime
        (session, old), target = window, f"{self.session}:=manager"
        keeps = self.manager_keeps(old, session) if old else None
        # Seen before the lock is tried: its end starts the new runtime's session also when no lock is held then. Not
        # a window whose program cannot be read, which can be the agent's own runtime.
        self.manager_seen_old = (old, session, keeps) if old else None
        if old:
            # In case it is closed by hand: every process seen at a reading, so that a reading that comes after its
            # window has gone loses none (still_running drops the ones that have ended).
            self.keep_procs(session, old, keeps, self.task_work("manager", "", keep_runtime=False))
        else:
            self.drop_procs()
        if keeps is None:
            self.manager_retiring = None
            self.release_manager(False)
            self.say_manager(f"manager: the session of the previous runtime ({old or 'unknown'}) stays open, because "
                             + ("its program cannot be read" if not old else
                                "its conversation is not recorded or not found") +
                             (f". Close its window, then start {self.conf.get('runtime')} with: fleet.py start {self.agent}"
                              if not old else f". Close its window; then this helper starts {self.conf.get('runtime')}"))
            return False
        if self.manager_lock is None:
            try:
                self.manager_lock = hold_manager_lock(self.agent)
            except Refused:
                return False            # a start of the manager session runs now: read again at the next round
        if keeps:
            note_begun(self.agent, old, session)    # its file is there: a later start that misses it stops
        screen = tmux("capture-pane", "-p", "-t", target)
        asks = screen.returncode == 0 and shows_question(screen.stdout)
        if asks and limit_menu(old, keeps):     # its runtime's usage-limit menu: the close answers nothing in it
            asks = False
            self.say_manager(f"manager: the session of the previous runtime ({old}) waits in its usage-limit menu; it "
                             f"closes without an answer there, so that no choice of that menu is made")
        if screen.returncode != 0 or WORKING.search("\n".join(screen.stdout.rstrip().split("\n")[-15:])) or asks:
            self.manager_retiring = None
            if asks:
                self.say_manager(f"manager: the session of the previous runtime ({old}) shows a question on its "
                                 f"screen; it closes when the question is answered")
                self.report_question()
            return False
        try:
            mark = (screen.stdout, os.path.getmtime(keeps) if keeps else None)
        except OSError:
            self.manager_retiring = None
            return False
        if self.manager_retiring is None or self.manager_retiring[1] != mark:
            self.manager_retiring = (time.monotonic(), mark)
            return False
        if time.monotonic() - self.manager_retiring[0] < RETIRE_QUIET:
            return False
        # Also what earlier readings saw: a process that this reading misses (it left the pane) still ends.
        before = {**still_running(self.manager_procs), **self.task_work("manager", "", keep_runtime=False)}
        self.manager_left_start = True
        save_closing(self.agent, before)        # first: a helper that restarts after the close still knows them
        tmux("kill-window", "-t", target)
        time.sleep(1)
        if "manager" in self.look():
            save_closing(self.agent, {})
            return False
        forgot = keeps == "" and self.forget_record(old, session)     # as soon as it is gone: no restart keeps it
        quiet, _, left = self.quiesce("manager", "", keep_runtime=False, also=before)
        self.manager_retiring, self.manager_seen_old = None, None
        self.drop_procs()
        self.manager_left = {} if quiet else self.task_work("manager", "", False, also=before)
        save_closing(self.agent, self.manager_left)
        self.say(f"manager: the previous runtime's session ({old}) was quiet and is closed; " +
                 ("it never began a conversation, so its record is removed" if forgot else
                  "its conversation stays for a switch back") +
                 ("" if not self.manager_left else f"; processes still run: {left}"))
        if self.manager_left:
            self.lifecycle.report()     # still pending: the hub keeps the manager's messages until they end
            return False
        return self.release_manager(True)

    def report_question(self):
        """Tell the hub that the manager session's screen shows a question (Handler.helper_dialog), which the owner
        then sees. During a switch no inbox signal reaches that session to find it. Again each minute while it stays,
        because the hub shows it for 90 seconds."""
        if self.manager_asked is not None and time.monotonic() - self.manager_asked < 60:
            return
        self.manager_asked = time.monotonic()
        with contextlib.suppress(urllib.error.URLError, OSError, ValueError):
            http("POST", f"/helper/dialog/{self.agent}/manager", {}, timeout=10)

    def manager_gone(self):
        """True when no standing manager window is there and nothing is closing or starting one, so its start is due
        again.

        The manager is the one agent whose session always stands, so its availability is the fleet's. Until
        19 October 2026 nothing watched that window: the start was due at the helper's start, after a sign-in
        (restart_after_login) and after a switch, and at no other moment. So a session that ended by itself — a
        crash, a runtime error, an out-of-memory kill, `tmux kill-window` — stayed gone. The hub raised
        `manager_down`, the app showed it, and the only way back was `systemctl --user restart fleet-<agent>` on
        that machine, by hand, once somebody noticed. The owner proved it on the hub machine that night: the window
        was killed and the manager was still gone five minutes later.

        The care of the rounds that already exist is kept. A reading that tmux does not answer is unknown, never
        "no window" (manager_reading). A window that still runs the previous runtime is a window: retire_old_manager
        closes it and makes the start due itself. While such a close is open — its processes have not ended
        (manager_left), its lock is still held, or a session of the previous runtime was seen (manager_seen_old) —
        this says nothing, because that close owns the start and holds its order. A start is tried at most each
        10 seconds (manage), and start_manager reads the windows again under the start lock, so a window that is
        starting is never fought over."""
        if self.manager_left or self.manager_lock is not None or self.manager_seen_old is not None:
            return False
        try:
            return self.manager_reading() is None
        except Refused:
            return False

    def manage(self):
        """One round of the standing manager session in the helper's loop. Its start is due (manager_due) when the
        helper's start found no window, when this round finds none (manager_gone), when this helper closed the
        previous runtime's session after a switch, or when the owner pressed the control that the hub's manager_down
        warning carries (owner feature manager_start); it is tried at most each 10 seconds, and it stays due until a
        start succeeds, also when a later step of the round fails."""
        self.restart_for_tools()
        if self.conf.get("role") == "manager" and not self.manager_due and self.manager_gone():
            self.manager_due = True
            self.say_manager("manager: its session is not running; the helper starts it again")
        if self.manager_due and (self.manager_tried is None or time.monotonic() - self.manager_tried > 10):
            self.manager_tried = time.monotonic()
            self.manager_due = self.start_manager()
            self.manager_fresh = self.manager_fresh and self.manager_due   # only the start that it was due for
        if self.retire_old_manager():
            self.manager_due = True
        self.report_manager_bound()

    def report_manager_bound(self):
        """Report the status at once when the manager's standing session changes (manager_bound, or the windows and
        processes of the previous runtime), whoever started or closed it: the hub ends a switch's hold of the
        manager's messages on the report of its new session with nothing of the previous one left."""
        if self.conf.get("role") != "manager":
            return
        # Both fields from one reading: the hub ends a switch's hold on them (switch_state), for example when the
        # processes of a session that `fleet.py start` replaced have ended, with no change of manager_bound.
        window = self.manager_state()
        seen = (self.manager_bound(window), tuple(self.other_runtime_windows(window)))
        if seen == self.manager_seen:
            return
        # A report that failed goes again, at most each 30 seconds: each one runs every status command, and the
        # hub can be down for a while (its restart).
        if self.manager_report_failed is not None and time.monotonic() - self.manager_report_failed < 30:
            return
        if self.lifecycle.report():
            self.manager_seen, self.manager_report_failed = seen, None
        else:
            self.manager_report_failed = time.monotonic()

    def retire_conversations(self):
        """Close a conversation window that was quiet for CONVERSATION_IDLE seconds (CHAT-APPS.md 7.2). Nothing is
        lost: the next message of that person starts a new window, whose prompt carries the hub's recap. A window
        that this helper did not start itself counts from the first time it saw it."""
        open_windows = [w for w in self.look() if CONVERSATION_WINDOW.fullmatch(w)]
        for window in [w for w in self.conversation_seen if w not in open_windows]:
            self.conversation_seen.pop(window, None)
        for window in open_windows:
            quiet = time.monotonic() - max(self.conversation_seen.setdefault(window, time.monotonic()),
                                           self.signalled.get(window, 0))
            if quiet > CONVERSATION_IDLE:
                tmux("kill-window", "-t", f"{self.session}:={window}")
                self.conversation_seen.pop(window, None)
                self.say(f"{window}: closed after {CONVERSATION_IDLE // 60} quiet minutes; the next message of this "
                         f"person starts a new window")

    def retire_old_chat(self):
        """After a runtime switch, close the owner chat window that still runs the previous runtime, but only when it
        is quiet: two readings at least RETIRE_QUIET seconds apart that show no running turn, the same screen and the
        same conversation file time. A chat that is still finishing (for example after tell_owner) goes on, and so
        does a chat without a recorded conversation or with a program that cannot be read: the hub holds new chats
        until this window and every process of it are gone. Its conversation and record stay for a switch back."""
        if self.retired_left:           # processes of the closed chat that did not end yet: the existing cleanup
            quiet, _, left = self.quiesce("owner-chat", "", keep_runtime=False, also=self.retired_left)
            self.retired_left = {} if quiet else self.task_work("owner-chat", "", False, also=self.retired_left)
            if not self.retired_left:
                self.say("owner-chat: the processes of the previous runtime's chat have ended")
                self.lifecycle.report()
            return
        old, target = self.window_runtime("owner-chat"), f"{self.session}:=owner-chat"
        if old in (None, self.conf.get("runtime")) or self.working(target):
            self.retiring = None
            return
        binding = self.chat_binding(old, self.window_generation("owner-chat"))
        if binding is None:
            self.retiring = None
            problem = (f"owner-chat: the chat of the previous runtime ({old or 'unknown'}) stays open, because its "
                       f"conversation is not recorded" + (f"; record it with: fleet.py chat-session {self.agent} "
                                                          f"<conversation id> --runtime {old}"
                                                          if old in MANAGER_RUNTIMES
                                                          else "; its program cannot be read"))
            if getattr(self, "chat_problem", (None, 0.0))[0] != problem or time.monotonic() - self.chat_problem[1] > 300:
                self.chat_problem = (problem, time.monotonic())
                self.say(problem)
            return
        try:
            mark = (tmux("capture-pane", "-p", "-t", target).stdout, os.path.getmtime(binding))
        except OSError:
            self.retiring = None
            return
        if self.retiring is None or self.retiring[1] != mark:
            self.retiring = (time.monotonic(), mark)
            return
        if time.monotonic() - self.retiring[0] < RETIRE_QUIET:
            return
        before = self.task_work("owner-chat", "", keep_runtime=False)
        tmux("kill-window", "-t", target)
        time.sleep(1)
        if "owner-chat" in self.look():
            return
        quiet, _, left = self.quiesce("owner-chat", "", keep_runtime=False, also=before)
        self.retiring = None
        self.retired_left = {} if quiet else self.task_work("owner-chat", "", False, also=before)
        self.say("owner-chat: the previous runtime's chat was quiet and is closed; its conversation stays for a "
                 "switch back" + ("" if not self.retired_left else f"; processes still run: {left}"))
        self.lifecycle.report()         # the hub delivers chats again once no such window or process is left

    def interrupt(self, window):
        """Stop the running turn with Esc, at most three times. Confirmed when the screen shows no running
        turn and the session's record file has been quiet for 5 seconds."""
        target = f"{self.session}:={window}"
        if window not in self.look():
            return True, "no session was running"
        if tmux("display-message", "-p", "-t", target, "#{pane_in_mode}").stdout.strip() not in ("", "0"):
            tmux("send-keys", "-t", target, "-X", "cancel")       # someone scrolled back; keys would not arrive
        if self.runner(target):
            # Ctrl-C: the runner interrupts its Codex turn, ends its own process group and exits.
            for _ in range(2):
                tmux("send-keys", "-t", target, "C-c")
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    time.sleep(1)
                    if window not in self.look():
                        return True, "the screen runner interrupted its turn and stopped"
            return False, "the screen runner did not stop after two interrupts"
        for _ in range(3):
            if self.working(target):
                tmux("send-keys", "-t", target, "Escape")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                time.sleep(1)
                if window not in self.look():
                    return True, "the session ended"
                if not self.working(target) and self.tailer.quiet(window, 5):
                    return True, "interrupted; the session is idle"
        return False, "the session still shows a running turn after three interrupts"

    def pane(self, target):
        pid, _, start = tmux("display-message", "-p", "-t", target, "#{pane_pid}\t#{pane_start_command}").stdout.strip().partition("\t")
        return (int(pid), start) if pid.isdigit() else (None, "")

    def runtime(self, target, rows):
        """A paused session's own processes: the pane, its wrappers, the program the helper started and that
        program's known helper. Everything else in the session, or with the task's environment, is work."""
        pane, start = self.pane(target)
        if pane is None:
            return set()
        try:
            words = launch_words(start)
        except (ValueError, IndexError):
            words = []
        program = os.path.basename(words[0]) if words else ""
        children = {}
        for pid, ppid, args, _ in rows:
            children.setdefault(ppid, []).append((pid, args))
        args_of = {pid: args for pid, _, args, _ in rows}
        keep, current = {pane}, pane
        for _ in range(4):          # down through shell and env wrappers to the program the helper started
            name = os.path.basename(args_of.get(current, "").split(" ", 1)[0]).lstrip("-")
            if not program or name == program or name not in ("sh", "bash", "zsh", "dash", "env"):
                break
            if len(children.get(current, [])) != 1:
                break
            current = children[current][0][0]
            keep.add(current)
        if program and os.path.basename(args_of.get(current, "").split(" ", 1)[0]) != program:
            return {pane}           # the program is not known: keep only the pane
        keep.update(pid for pid, args in children.get(current, []) if any(h in args for h in RUNTIME_HELPERS))
        return keep

    def task_work(self, window, generation, keep_runtime, also=None):
        """{pid: args} of the task's processes: the session's descendants (by parent links, so also platform
        binaries on macOS, whose environment is hidden), every process whose environment names the task (also
        re-parented ones on Linux), and still-running processes of an earlier snapshot. A paused session keeps
        its own runtime."""
        target = f"{self.session}:={window}"
        rows = process_table(window, generation)
        args_of = {pid: args for pid, _, args, _ in rows}
        pane = self.pane(target)[0] if window in self.look() else None
        found = {pid for pid, _, _, mine in rows if mine}
        if pane is not None:
            found |= descendants(rows, pane) | ({pane} if not keep_runtime else set())
        found |= {pid for pid, args in (also or {}).items() if args_of.get(pid) == args}
        keep = self.runtime(target, rows) if keep_runtime and pane is not None else set()
        return {pid: args_of[pid] for pid in found - keep - {os.getpid()} if pid in args_of}

    def quiesce(self, window, generation, keep_runtime, also=None):
        """End every process of this task (TERM, then KILL), except a paused session's own runtime.
        Returns (quiet, ended, survivors). A confirmed hold needs quiet."""
        ended, work = 0, {}
        for _ in range(3):
            work = self.task_work(window, generation, keep_runtime, also)
            if not work:
                return True, ended, ""
            left = end_processes(list(work))
            ended += len(work) - len(left)
        return False, ended, ", ".join(f"{pid} {args[:60]}" for pid, args in list(work.items())[:5])

    def request_job(self, task, extra):
        """call_api: the vault sends a request for a hub job. It runs in a thread of its own, as a check does, so that
        a pause or stop of its task is served while it runs (control has the vault end it). No Fleet update restarts
        the helper until the report is sent. An event that comes again while its job runs starts nothing: the vault
        would answer that it does not know the outcome, and that answer could come first."""
        job = extra.get("job")
        with self.outside_lock:
            if job in self.requests:
                return
        self.begin_outside()
        with self.outside_lock:
            self.requests[job] = task
        threading.Thread(target=self.request_thread, args=(task, job), daemon=True, name=f"request-{job}").start()

    def request_thread(self, task, job):
        try:
            try:
                report = do_request(self.agent, job)
            except Unknown as e:        # the request can have gone out
                report = {"ok": False, "unknown": True, "text": str(e)}
            except Refused as e:        # the vault proved that nothing went out, or it was never asked
                report = {"ok": False, "text": str(e)}
            except Exception as e:      # never pass details on; after the vault was asked, an error proves nothing
                report = {"ok": False, "unknown": True,
                          "text": f"the helper hit an error ({type(e).__name__}). Ask your requester."}
            took = self.post_job(job, report)
            self.say(f"{task}: request job {job}: " + ("HTTP " + str(report["status"]) if report["ok"] else
                                                       "outcome not known" if report.get("unknown") else "not sent") +
                     ("" if took else "; the hub took no report"))
        finally:
            with self.outside_lock:
                self.requests.pop(job, None)
            self.end_outside()

    def post_job(self, job, report):
        """Send the report of a request job. Without an answer of the hub, send the same report again, at most three
        times: the vault keeps no answer, so a report that is lost here cannot be had again."""
        for pause in (0, 2, 5, 15):
            time.sleep(pause)
            try:
                code, _ = http("POST", f"/helper/job/{self.agent}/{job}", report, timeout=15)
            except Exception:           # no answer in any form, also an answer that was cut: the same report goes again
                continue
            if code in (200, 409):      # 409: the hub already has a report of this job
                return True
        return False

    def end_requests(self, task, extra):
        """(proved, ended): have this machine's vault end every request of this task attempt (call_api), and wait
        until its child process is gone. The scope is the hub generation, the task and the attempt of this pause or
        stop, for this agent's key. The vault is asked only when a request of the task can run: this helper has one
        in a thread, or the hub counts one that is not finished. Without one there is nothing to end or to prove (the
        hub gives no request to a vault during a hold). The cancel goes to the vault directly, with no status
        question before it: a vault answers a cancel at once, but its status only after the request that runs. Proof
        is only its answer that the requests of this task attempt stopped. A refusal, no answer or no vault is no
        proof, and then the pause or stop is not confirmed."""
        with self.outside_lock:
            mine = task in self.requests.values()
        need = mine or bool(extra.get("requests"))      # the hub's count also covers a helper that started again
        key = load_agent_key(self.agent)
        if not need or vault_mode(self.agent) != "broker" or not key:
            return not need, 0
        try:
            reply = vault_call({"op": "cancel", "db": str(extra.get("generation") or ""), "task": task,
                                "attempt": extra.get("attempt"), "agent": self.agent, "agent_key": key}, timeout=30)
        except Refused:
            return False, 0
        if not (reply.get("stopped") is True and reply.get("task") == task and reply.get("attempt") == extra.get("attempt")):
            return False, 0
        ended = reply.get("ended")
        return True, ended if isinstance(ended, int) and not isinstance(ended, bool) and ended > 0 else 0

    def control(self, task, extra):
        """Pause: interrupt and keep the session. Stop: interrupt, then close it. The hub works out unknown outcomes."""
        ran, stop = task in self.look(), extra.get("action") == "stop"
        generation = str(extra.get("generation") or "")
        proved, requests_ended = self.end_requests(task, extra)    # first: a request of the task must not go on
        before = self.task_work(task, generation, keep_runtime=False)    # all of the task, before anything closes
        confirmed, detail = self.interrupt(task)
        flushed = self.tailer.flush(task)       # every record before the result, while the window still exists
        if flushed is None:
            raise Refused("the session records could not be sent to the hub")
        ended = 0
        # A stopped turn is not enough: a command that it started can still run (Codex background sessions).
        if confirmed and task in self.look():
            confirmed, ended, left = self.quiesce(task, generation, keep_runtime=True)
            detail = detail if confirmed else f"processes of the task still run: {left}"
        if confirmed and stop and task in self.look():
            tmux("kill-window", "-t", f"{self.session}:={task}")
            time.sleep(1)
            confirmed = task not in self.look()
            detail = "interrupted and closed" if confirmed else "the session window did not close"
        if confirmed and task not in self.look():       # a closed session leaves nothing of the task behind
            confirmed, more, left = self.quiesce(task, generation, keep_runtime=False, also=before)
            ended += more
            detail = detail if confirmed else f"processes of the task still run: {left}"
        if confirmed and ended:
            detail += f"; ended {ended} command process(es)"
            deadline = time.monotonic() + 10    # a live runtime writes the end of each ended command; wait for it
            while task in self.look() and time.monotonic() < deadline and not self.tailer.quiet(task, 3):
                time.sleep(0.5)
        flushed = self.tailer.flush(task) or flushed     # include what an ended command wrote
        if not proved:
            confirmed, detail = False, "the vault did not prove that its request of the task ended"
        elif confirmed and requests_ended:
            detail += f"; the vault ended {requests_ended} request(s)"
        code, data = http("POST", f"/helper/control/{self.agent}",
                          {"hold": extra.get("hold"), "confirmed": confirmed, "detail": detail, "flushed": flushed,
                           "ran": ran, "ended": ended})
        if code not in (200, 409):          # 409: the hold was released or settled meanwhile
            raise Refused(f"the hub did not take the {extra.get('action')} result ({code})")
        self.say(f"{task}: {extra.get('action')} {'confirmed' if confirmed else 'NOT confirmed'}: {detail}")

    def login_job(self, kind, task, extra):
        """A sign-in or a handover an agent asked for. It happens here, outside the agent's session."""
        login, to = str(extra.get("login") or ""), str(extra.get("to") or "")
        unknown = False
        self.begin_outside()
        try:
            try:
                text = (do_save(self.agent, extra.get("job"), kind) if kind in SAVES else
                        do_signin(login, self.agent, task or "", extra.get("job")) if kind == "signin" else
                        do_handover(login, self.agent, to, task or "", extra.get("job")))
                ok = True
            except Unknown as e:        # a save: the item can exist. The job stays open, to be looked for again
                ok, unknown, text = False, True, str(e)
            except Refused as e:
                ok, text = False, str(e)
            except Exception as e:      # never pass details on: they could hold a login
                ok, text = False, f"the helper hit an error ({type(e).__name__}). Ask your requester."
                unknown = kind in SAVES         # after the request went out, an error here proves nothing either
            http("POST", f"/helper/job/{self.agent}/{extra.get('job')}",
                 dict({"ok": ok, "text": text}, **({"unknown": True} if unknown else {})))
        finally:
            self.end_outside()
        self.say(f"{task}: {kind}" + (f" with {login}" if login else "") +
                 f": {'done' if ok else 'outcome not known' if unknown else 'refused: ' + text[:120]}")


def cmd_run(args):
    agent, conf = args.agent, agent_conf(args.agent)
    if not shutil.which("tmux"):
        die("tmux is not installed")
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    lock = open(os.path.join(HOME, f"helper-{agent}.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        die(f"another {agent} helper is already running")
    claim_checkout(agent)
    h = Helper(agent, conf)
    ensure_session(h.session)
    # The helper's start runs the manager session once. With a key it needs its credential, so the start waits in the
    # loop below until the hub answers; a session that runs already, or one closed later, is left as it is.
    ready_runtime_config()          # before the first session: nobody is there to answer the runtime's first screen
    h.manager_due = conf.get("role") == "manager" and h.manager_first()
    print(f"helper for {agent} is running; persistent inbox delivery; sessions in {h.session}", flush=True)
    collector = Collector(KIT, agent, conf["runtime"],
                          lambda payload: http("POST", f"/helper/usage/{agent}", payload, timeout=5),
                          screen_command=conf.get("screen_command"), local=local_model(conf),
                          previous=next(iter(conf.get("runtime_policies") or {}), None))   # owner feature runtime_switch
    threading.Thread(target=collector.run, name="usage-collector", daemon=True).start()
    h.lifecycle = Lifecycle(KIT, agent, conf, h.post_status,
                            busy=lambda: any(re.fullmatch(r"T-\d+", w) for w in h.look()) or h.outside > 0 or
                            h.manager_lock is not None,     # no restart while a previous runtime's manager closes
                            vault=lambda: vault_status(agent), probe=lambda job: probe_logins(agent, job),
                            legacy=h.legacy_windows, switch=h.switch_state)
    threading.Thread(target=h.lifecycle.run_forever, name="machine-status", daemon=True).start()
    threading.Thread(target=h.tailer.run, name="thread-records", daemon=True).start()
    has_key, keyed, enrolled_at = False, False, 0.0
    while True:
        try:
            h.keep_session()        # the tmux session is not this helper's to assume (keep_session says why)
            h.manage()
            has_key = has_key or register_key(agent)
            # Owner API 1.12: enroll the agent key while no task window runs, so that no hook of a running task can
            # call the hub between the hub's registration and the key file's arrival.
            if not keyed and time.monotonic() - enrolled_at > 60 and not any(re.fullmatch(r"T-\d+", w) for w in h.look()):
                enrolled_at = time.monotonic()
                keyed = enroll_key(agent)
            h.sweep_desktop()
            h.sweep_sessions()
            h.retire_old_chat()
            h.retire_conversations()
            alive = urllib.parse.quote(",".join(h.look()))
            code, data = http("GET", f"/events/{agent}?delivery=1&after=0&wait=8&alive={alive}")
            if code == 403 and data.get("auth") == "no_key" and load_agent_key(agent):
                os.remove(key_file(agent))      # the owner removed this agent's key: legacy again until it enrolls
                keyed, enrolled_at = False, 0.0
                h.say("the hub has no key for this agent any more; it enrolls again when it can")
            if code != 200:
                raise Refused(f"hub answered {code}")
            h.handle_events(data.get("events", []))
            time.sleep(2)
        except (urllib.error.URLError, OSError, Refused) as e:
            print(f"hub unavailable ({type(e).__name__}); retry in 5 seconds", flush=True)
            time.sleep(5)


def cmd_service(args):
    """Install only this pilot's service definitions; leave activation explicit."""
    path_env = os.pathsep.join([os.path.expanduser("~/.local/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    if sys.platform == "darwin":
        if args.name != "watch" and TEAM["agents"].get(args.name, {}).get("role") not in ("worker", "lead"):
            die("macOS services require a configured worker or lead name, or watch")
        label = "com.agentfleet." + args.name
        folder = os.path.expanduser("~/Library/LaunchAgents")
        os.makedirs(folder, exist_ok=True)
        os.makedirs(HOME, mode=0o700, exist_ok=True)
        argv = [sys.executable, os.path.join(KIT, "fleet.py")]
        argv += ["watch"] if args.name == "watch" else ["run", args.name]
        plist = {"Label": label, "ProgramArguments": argv, "WorkingDirectory": KIT,
                 "EnvironmentVariables": {"PATH": path_env}, "RunAtLoad": True,
                 "KeepAlive": True, "ThrottleInterval": 10, "LimitLoadToSessionType": "Aqua",
                 "ProcessType": "Interactive", "StandardOutPath": os.path.join(HOME, args.name + ".log"),
                 "StandardErrorPath": os.path.join(HOME, args.name + ".err")}
        path = os.path.join(folder, label + ".plist")
        with open(path, "wb") as f:
            plistlib.dump(plist, f)
        print(f"Installed {path}. Activate in the desktop session: launchctl bootstrap gui/{os.getuid()} {shlex.quote(path)}")
        return
    name = args.name
    if name not in ("hub", "git") and name not in TEAM["agents"]:
        die("Linux services require hub, git or a configured agent name")
    linger = subprocess.run(["loginctl", "show-user", str(os.getuid()), "-p", "Linger", "--value"], capture_output=True, text=True)
    if linger.stdout.strip() != "yes":
        die("enable linger first: loginctl enable-linger --no-ask-password " + os.environ.get("USER", "claude"))
    if name == "hub":
        argv = [sys.executable, os.path.join(KIT, "hub.py")]
    elif name == "git":     # fetch only: git:// has no authentication, so pushes go over SSH (PILOT.md "Pushes")
        host = TEAM["addresses"][TEAM["hub"]["machine"]]
        argv = [shutil.which("git"), "daemon", "--reuseaddr", f"--listen={host}", "--forbid-override=receive-pack",
                "--base-path=" + os.path.expanduser("~"), os.path.expanduser("~/fleet-pilot.git")]
    else:
        argv = [sys.executable, os.path.join(KIT, "fleet.py"), "run", name]
    def unit_quote(x):
        return json.dumps(x.replace("%", "%%"))
    # StartLimitIntervalSec=0 turns off systemd's own rate limit, which otherwise gives up after five starts in
    # ten seconds and leaves the unit failed. With Restart=always and RestartSec=5 that is about twenty seconds,
    # and a customer has no console to bring it back: whatever made the process exit, the fleet is then gone for
    # as long as nobody looks. Keeping it trying costs one restart every five seconds and can recover by itself
    # from a cause that passes — a disk that was full, a database that was locked, a machine still booting.
    unit = ("[Unit]\nDescription=Founderizer " + name + "\nAfter=network-online.target\nStartLimitIntervalSec=0\n"
            "[Service]\nType=simple\nWorkingDirectory=" + KIT.replace("%", "%%").replace(" ", r"\x20") + "\n"
            "Environment=" + unit_quote("PATH=" + path_env) + "\nExecStart=" + " ".join(map(unit_quote, argv)) + "\n"
            "Restart=always\nRestartSec=5\nKillMode=process\nTimeoutStopSec=15\n"
            "[Install]\nWantedBy=default.target\n")
    folder = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"fleet-{name}.service")
    with open(path, "w") as f:
        f.write(unit)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", f"fleet-{name}.service"], check=True)
    print(f"Installed and enabled {path}. Start after stopping the old pilot process: systemctl --user start fleet-{name}")


def cmd_start(args):
    """Start the manager session again without starting a second helper: a Claude manager continues its recorded
    conversation (manager_start). While the session runs, this records its conversation and changes nothing else
    (adopt_manager)."""
    agent, conf = args.agent, agent_conf(args.agent)
    if conf.get("role") != "manager":
        die("only the manager has a standing session; worker sessions start with their tasks")
    session = f"fleet-{agent}"
    try:
        with manager_start_lock(agent):
            ensure_session(session)
            if "manager" in windows(session, checked=True):
                print("the manager session is already running")
                try:
                    print(adopt_manager(agent, conf, session))
                except (urllib.error.URLError, TimeoutError) as e:
                    die(f"the hub cannot be reached ({e}), so nothing was recorded")
                return
            left = load_closing(agent)
            if left:        # after them its helper starts the session only when no start by hand came between
                then = "and then starts the session" if closing_start(agent) else \
                    "but starts no session after them: run this command again when they have ended"
                die(f"processes of the previous runtime's manager session still run ({', '.join(map(str, sorted(left)))}). "
                    f"Its helper ends them {then}; nothing was started")
            claim_checkout(agent)
            generation = manager_generation(agent)
            command, sid, record, note = manager_start(agent, conf, generation, manager_credential(agent, generation))
            save_handover(agent, start_handover(agent, generation))
            if record:      # first: no window runs without it (a start that never begins is replaced next time)
                save_chat_record(agent, record, conf.get("runtime"), "manager")
            try:
                start_window(session, "manager", command, {"@fleet_session": sid})
            except subprocess.CalledProcessError as e:
                die(f"tmux did not start the manager window ({(e.stderr or '').strip() or e.returncode}). Look with: "
                    f"tmux list-windows -t {session}; then run this command again")
            print(f"started the manager session{f' ({note})' if note else ''}. Watch it with: tmux attach -t {session}")
    except Refused as e:
        die(str(e))


# ---------------------------------------------------------------- setting up

def cmd_addresses(args):
    path = os.path.join(KIT, "team.json")
    team = load_team()
    table = team.setdefault("addresses", {})
    known = sorted({a.get("machine", "") for a in team["agents"].values()} | {"owner"})
    for pair in args.pairs:
        name, sep, ip = pair.partition("=")
        if not sep or name not in known:
            die(f"'{pair}': write name=address, with a name from: {', '.join(known)}")
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            die(f"'{ip}' is not an address. `tailscale ip -4` prints a machine's own; "
                f"`tailscale ip -4 <name>` prints another machine's.")
        table[name] = ip
    if args.pairs:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(team, f, indent=2)
            f.write("\n")
    for name in known:
        print(f"  {name:12} {table.get(name) or 'NOT SET'}")
    url = hub_url(team)
    print(f"  hub          {url or 'NOT SET'}" + (f"   board: {url}/board" if url else ""))
    missing = [m for m in known if not table.get(m)]
    if missing:
        print(f"Still to set: {', '.join(missing)}. Until then the hub takes those names from any address.")


CODEX_BLOCK = '''
[mcp_servers.fleet]
url = "{url}"
tool_timeout_sec = 120
default_tools_approval_mode = "approve"
'''


def cmd_connect(args):
    agent, conf = args.agent, agent_conf(args.agent)
    if not HUB:
        die("no hub address yet. On the hub's machine run: python3 fleet.py addresses ...")
    url = f"{HUB}/mcp/{agent}"
    if conf.get("runtime") == "claude":
        if not shutil.which("claude"):
            die("the claude command is not on this machine's PATH")
        subprocess.run(["claude", "mcp", "remove", "fleet"], cwd=KIT, capture_output=True)
        r = subprocess.run(["claude", "mcp", "add", "--transport", "http", "fleet", url],
                           cwd=KIT, capture_output=True, text=True)
        if r.returncode != 0:
            die(f"claude mcp add failed: {(r.stderr or r.stdout).strip()[:300]}")
        print(f"Claude Code, in this folder only, now has the team tools of {agent}: {url}")
        print("Undo with: claude mcp remove fleet")
    elif conf.get("runtime") == "codex":
        path = os.path.expanduser("~/.codex/config.toml")
        block = CODEX_BLOCK.format(url=url)
        text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        if "[mcp_servers.fleet]" in text:
            if f'url = "{url}"' in text:
                print(f"Codex already has the team tools of {agent} in {path}")
            else:
                print(f"{path} already has a [mcp_servers.fleet] block that points elsewhere. Make it read:{block}")
                sys.exit(1)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(block)
            print(f"Codex now has the team tools of {agent}. Added to {path}:{block}")
    else:
        die(f"connect knows claude and codex, not '{conf.get('runtime')}'")


def chrome_probe(wait=120):
    """macOS only: can this terminal drive Chrome, and may Chrome run page scripts for it?
    Returns (answer, hint) where answer is True, False or None for "could not tell"."""
    try:
        r = subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to count windows'],
                           capture_output=True, text=True, timeout=wait)
        if r.returncode != 0:
            return False, "macOS asks once whether this terminal may control Chrome: say yes"
        if r.stdout.strip() in ("", "0"):
            return None, "Chrome needs an open window on an ordinary web page"
        r = subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to execute active tab of '
                                               'front window javascript "1+1"'],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return False, ("in Chrome choose View > Developer > Allow JavaScript from Apple Events, "
                           "with an ordinary web page in front")
        return True, ""
    except subprocess.TimeoutExpired:
        return None, "macOS is waiting for an answer on this Mac's screen: may this terminal control Chrome?"
    except (OSError, subprocess.SubprocessError):
        return None, "could not run osascript"


def cmd_check(args):
    agent = args.agent
    conf = TEAM["agents"].get(agent)
    ok = True

    def line(good, what, hint=""):
        nonlocal ok
        if good is None:
            print(f"  --    {what}" + (f"  ({hint})" if hint else ""))
            return
        ok = ok and bool(good)
        print(f"  {'ok  ' if good else 'FAIL'}  {what}" + (f"  ({hint})" if hint and not good else ""))

    print(f"Checking this machine for agent '{agent}'")
    line(sys.version_info >= (3, 9), f"Python {sys.version.split()[0]}", "needs 3.9 or later")
    line(bool(conf), "agent is listed in team.json")
    table = TEAM.get("addresses") or {}
    unset = [m for m in sorted({a.get("machine", "") for a in TEAM["agents"].values()} | {"owner"}) if not table.get(m)]
    line(not unset, "every machine's address is in team.json", f"missing: {', '.join(unset)}. Run fleet.py addresses")
    line(bool(HUB), f"hub address: {HUB or 'not set'}", "run fleet.py addresses on the hub's machine, then pull")
    if HUB:
        try:
            code, data = http("GET", "/health", timeout=8)
            line(code == 200 and data.get("ok"), "hub answers")
        except (urllib.error.URLError, OSError) as e:
            line(False, "hub answers", str(e))
    if conf and HUB:
        try:
            c, r = http("POST", f"/mcp/{agent}", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, timeout=8)
            names = [t["name"] for t in r.get("result", {}).get("tools", [])]
            if c == 403 and r.get("auth") == "credential_required":     # owner API 1.12: a shared machine
                line(None, f"the hub takes this machine as {agent}", "on a shared machine only sessions with their "
                                                                      "credential reach the team tools")
            else:
                line(c == 200 and bool(names), f"the hub takes this machine as {agent} ({len(names)} team tools)",
                     r.get("error") if isinstance(r.get("error"), str) else "")
        except (urllib.error.URLError, OSError) as e:
            line(False, f"the hub takes this machine as {agent}", str(e))
    if conf:
        runtime = conf.get("runtime", "")
        line(bool(shutil.which("tmux")), "tmux installed")
        line(bool(shutil.which("git")), "git installed")
        line(bool(shutil.which(runtime)), f"{runtime} command found")
        # These two read what a runtime somebody starts **by hand in a terminal here** would use: Claude's
        # registered servers and Codex's own configuration file. An agent's own sessions use neither. The helper
        # puts the team tools on the command line when it starts one (`start_command`): Codex gets
        # `-c mcp_servers.fleet.url=...`, and Claude gets `--strict-mcp-config --mcp-config ...`, which uses only
        # that configuration and so ignores the registered server on purpose. So neither line says anything about
        # whether the agent reaches its team tools, and a failure here failed a machine that works.
        url, hand = f"{HUB}/mcp/{agent}", f"run: python3 fleet.py connect {agent}. An agent's own sessions get the " \
                                          f"team tools on the command line and need this for nothing"
        if runtime == "claude" and shutil.which("claude") and HUB:
            try:
                r = subprocess.run(["claude", "mcp", "list"], cwd=KIT, capture_output=True, text=True, timeout=90)
                mine = [l for l in r.stdout.split("\n") if l.startswith("fleet:")]
                line(True if bool(mine) and url in mine[0] and "Connected" in mine[0] else None,
                     "Claude Code in a terminal here reaches the team tools", hand)
            except (OSError, subprocess.SubprocessError):
                line(None, "Claude Code in a terminal here reaches the team tools", "could not run `claude mcp list`")
        if runtime == "codex" and HUB:
            path, text = os.path.expanduser("~/.codex/config.toml"), ""
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            line(True if f'url = "{url}"' in text else None,
                 "Codex in a terminal here reaches the team tools", hand)
    print("For the login tests:")
    token = os.path.join(HOME, "bws-token")
    if vault_mode(agent) == "file":
        # Only this user's own vault files use them. A broker keeps its store, its region and its programs in its own
        # root-owned configuration, and one that opens the fleet's own store has no Bitwarden account at all; an
        # agent with no credential access has nothing to open either way. In both of those this failed a machine
        # that is exactly as it should be, for something that is not its business.
        line(True if shutil.which("bws") else None, "Bitwarden Secrets Manager CLI (bws) installed", "not installed")
        line(TEAM.get("bitwarden_region") in ("eu", "us"), "Bitwarden region is eu or us",
             "set bitwarden_region in team.json")
    if vault_mode(agent) == "none":
        line(None, "no credential access, so this agent has no login to test",
             "the owner gives an agent its machine's vault in Founderizer")
    elif vault_mode(agent) == "broker":
        readable = vault_readable()
        line(not readable, "vault token and handover key are not readable by this user (vault broker)",
             "move ~/.fleet/" + " and ~/.fleet/".join(readable) + " to the vault")
        try:
            status = vault_call({"op": "status"}, timeout=5)
            line(True, f"vault broker answers (kept by {status.get('user') or 'the signed helper'})")
            store = status.get("backend")           # P5: the fleet's own store, or a Bitwarden project
            line(store in ("store", "bitwarden") or None,
                 "vault broker opens " + ("the fleet's own store" if store == "store" else
                                          "a Bitwarden project" if store == "bitwarden" else "a store it does not name"),
                 "this broker is older than the fleet's own store")
            line(status.get("recipient") == conf.get("vault_recipient"), "vault broker key is the one pinned in team.json",
                 "set vault_recipient for this agent to the broker's public key")
            # The broker runs a root-owned copy of vault.py that no release reinstalls, so this is the one line that
            # says whether it runs the vault of this checkout. The hub says the same against the revision it serves.
            try:
                with open(os.path.join(KIT, "vault.py"), "rb") as f:
                    mine = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                mine = None
            line(None if not (status.get("source") and mine) else status["source"] == mine,
                 "vault broker runs the vault.py of this checkout",
                 "this broker says nothing about its program" if not status.get("source") else
                 "install vault.py again and restart fleet-vault (WORKER-INSTALL.md, \"Vault broker\")")
        except Refused as e:
            line(False, "vault broker", str(e))
    elif os.path.exists(token):
        try:
            bws_token()
            line(True, "Bitwarden token file is private and nonempty")
        except Refused as e:
            line(False, "Bitwarden token file", str(e))
    else:
        line(None, "Bitwarden token in ~/.fleet/bws-token", "not there; login tests are not ready")
    line(True if shutil.which("age") else None, "age installed", "not installed")
    if sys.platform == "darwin" and conf and "browser" in conf.get("can", []):
        if os.path.exists("/Applications/Google Chrome.app"):
            good, hint = chrome_probe()
            line(True if good else None, "Chrome takes sign-ins from this terminal", hint)
        else:
            line(None, "Google Chrome installed", "not in /Applications")
    print("READY" if ok else "NOT READY: fix the lines marked FAIL")
    sys.exit(0 if ok else 1)


def cmd_verify(args):
    """PRODUCT.md 4.1: is this machine of the fleet healthy? Read-only, no root, Linux and macOS. The installers
    end with it, the app shows it as the owner's checklist, and the support duty runs it from a heartbeat."""
    import verify
    results = verify.checks(KIT, TEAM, HUB, agent=AGENT or os.environ.get("FLEET_AGENT") or None)
    print(json.dumps(results, indent=2) if args.json else verify.report(results))
    sys.exit(1 if verify.failed(results) else 0)


# ---------------------------------------------------------------- owner commands

def cmd_verify_case(args):
    """The recorded pilot case checker (PILOT.md), which held the name `verify` until 2026-10-17."""
    from verify_case import verify
    code, records = http("GET", "/records")
    if code != 200:
        die("cannot read verification records from the owner machine")
    ok = verify(records, KIT, case_id=args.case, run=args.run)
    sys.exit(0 if ok else 1)


def cmd_watch(args):
    code, data = http("GET", "/events/owner?after=-1")
    if code != 200:
        die(f"the hub will not take this machine as yours: {data.get('error', code)}")
    cursor, db = data.get("last", 0), data.get("db")
    print("watching for Needs-you items. Ctrl-C to stop.", flush=True)
    while True:
        try:
            code, data = http("GET", f"/events/owner?after={cursor}&wait=25")
        except (urllib.error.URLError, OSError):
            time.sleep(5)
            continue
        if code != 200:
            time.sleep(5)
            continue
        if data.get("db") != db:        # the hub came back with a new or emptied database
            cursor, db = 0, data.get("db")
            continue
        cursor = max(cursor, data.get("last", cursor))
        for ev in data.get("events", []):
            cursor = max(cursor, ev["id"])
            print(time.strftime("%H:%M:%S"), ev["text"], flush=True)
            if sys.platform == "darwin":
                msg = ev["text"].replace("\\", " ").replace('"', "'")
                result = subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "Founderizer" sound name "Ping"'],
                                        capture_output=True, text=True)
                print(f"notification event {ev['id']}: exit {result.returncode}" +
                      (f"; {result.stderr.strip()}" if result.stderr else ""), flush=True)
            else:
                print("\a", end="", flush=True)


def cmd_say(args):
    code, data = http("POST", "/say", {"text": args.text})
    print("sent" if code == 200 else f"not sent: {data.get('error', code)}")


def cmd_board(args):
    code, data = http("GET", "/board.txt")
    print(data.get("text", data))


def cmd_hub(args):
    if not HUB:
        die("no hub address yet. On the hub's machine run: python3 fleet.py addresses ...")
    print(HUB)


# ---------------------------------------------------------------- logins

def bws_token():
    """Read only this helper's token. Never use an inherited account or print a token."""
    try:
        with open(os.path.join(HOME, "bws-token"), encoding="utf-8") as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise Refused("the Bitwarden token must belong to this user and have mode 600: ~/.fleet/bws-token")
            token = f.read().strip()
    except (OSError, UnicodeError):
        raise Refused("this machine has no readable Bitwarden token in ~/.fleet/bws-token") from None
    if not token:
        raise Refused("this machine's Bitwarden token file is empty")
    return token


def vault_socket():
    """Where this machine's vault broker listens: a broker user's service on Linux, the signed Fleet helper on a Mac."""
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/AgentFleet/vault.sock")
    return "/run/fleet-vault/vault.sock"


def vault_mode(agent):
    """What opens this agent's logins, from team.json: "broker", its machine's vault broker, which holds the fleet's
    own store or a Bitwarden token; "file", the Bitwarden token of this user, which the pilot started with; or
    "none", no credential access at all (owner API 1.12).

    **A team.json that says nothing about an agent means "none"** (PRODUCT.md P5): a fresh fleet has no Bitwarden
    account and no token file, so a default of "file" pointed every new agent at a file that is not there. An agent
    gets credential access when the owner gives it, which is also what hub.sh already writes for the first manager.
    A command that names no agent is the owner's own on his own machine (fleet.py register), and it still reads the
    token file of this user, which he put there himself."""
    if not agent:
        return "file"
    mode = (TEAM.get("agents", {}).get(agent) or {}).get("vault")
    return mode if mode in ("broker", "file") else "none"


NO_CREDENTIALS = "this agent has no credential access; only the owner changes that"


def broker_status(timeout=5):
    """The public status of this machine's vault broker (its status operation only: no job, no login, no file), or
    None when no broker answers."""
    if not os.path.exists(vault_socket()):
        return None
    try:
        return vault_call({"op": "status"}, timeout=timeout)
    except Refused:
        return None


def broker_job_request(agent, request, timeout=90):
    """A job request to this machine's vault broker. When the broker serves callers (its status says callers: true)
    and this agent has a key, the request names the agent and carries its key, which the broker uses for the hub
    calls of this request only. An older broker never gets these fields (shared broker callers)."""
    key = load_agent_key(agent)
    status = broker_status() if key else None
    if key and status and status.get("callers") is True:
        request = dict(request, agent=agent, agent_key=key)
    return vault_call(request, timeout=timeout)


SAVES = {"create": "creates", "capture": "captures"}     # the vault operations that save an item, and their status field
NO_ANSWER_IS_UNKNOWN = set(SAVES) | {"request"}          # after one of these went to the vault, silence proves nothing
REPLY_LIMIT = 1 << 20           # bytes of one vault answer: a request's answer has a body


class Unknown(Refused):
    """The vault could not tell whether its one create call made the item (create_login, save_key). This is no
    refusal: nothing is asked again by itself, and the same job is only looked for again. For call_api: the request
    can have reached the service, and it is never sent again."""


def vault_call(request, timeout=90):
    """One request to this machine's vault broker: a job number goes in, an outcome or a locked copy comes back."""
    path = vault_socket()
    try:
        info = os.stat(path)
    except OSError:
        raise Refused("this machine's vault broker is not running; ask your requester") from None
    if sys.platform != "darwin" and info.st_uid == os.getuid():
        raise Refused("this machine's vault broker runs as the agents' user, so it keeps nothing apart; ask your requester")
    sent = False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path)
            sent = True
            s.sendall((json.dumps(dict(request, v=2)) + "\n").encode())
            data = b""
            while not data.endswith(b"\n") and len(data) < REPLY_LIMIT:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        reply = json.loads(data)
    except (OSError, ValueError):
        if sent and request.get("op") in NO_ANSWER_IS_UNKNOWN:      # it can have reached the vault: no answer proves nothing
            raise Unknown("this machine's vault broker gave no answer for the " +
                          ("request" if request.get("op") == "request" else "new login")) from None
        raise Refused("this machine's vault broker did not answer; ask your requester") from None
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        error = reply.get("error") if isinstance(reply, dict) else None
        if isinstance(reply, dict) and reply.get("unknown") is True:
            raise Unknown("the vault could not confirm the outcome" + (f": {error}" if isinstance(error, str) else ""))
        if request.get("op") == "request" and not (isinstance(reply, dict) and reply.get("ok") is False):
            # call_api: only the vault's own refusal (ok: false) proves that nothing went out
            raise Unknown("this machine's vault broker gave no clear answer for the request")
        raise Refused("the vault broker refused" + (f": {error}" if isinstance(error, str) else ""))
    return reply


LOGIN_REF = re.compile(r"bws://([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})")


def probe_logins(agent, job):
    """The owner's login probe (owner API 1.9): which logins of the profile this machine's vault can open. The answer
    has fixed codes only, and no part of a login leaves the vault. In broker mode the broker does it."""
    if vault_mode(agent) == "broker":
        return broker_job_request(agent, {"op": "probe", "job": job}, timeout=60)
    code, data = http("POST", f"/helper/vault/{agent}", {"job": job, "kind": "probe"}, timeout=15)
    if code != 200 or not isinstance(data, dict) or data.get("ok") is not True or data.get("job") != job:
        raise Refused("the hub did not confirm the probe job")
    logins = [x for x in data.get("logins") or [] if isinstance(x, str) and LOGIN_REF.fullmatch(x)][:32]
    if vault_mode(agent) == "none":
        return {"ok": True, "results": []}     # no vault file is read
    try:
        token = bws_token()
    except Refused:
        token = None
    return {"ok": True, "results": [probe_one(ref, token) for ref in logins]}


def probe_one(ref, token):
    """One login of the profile: can this vault open it? Fixed codes only."""
    def result(state, reason):
        return {"login": ref, "state": state, "reason": reason}
    region = TEAM.get("bitwarden_region")
    if not token:
        return result("not_readable", "no_token")
    if not shutil.which("bws") or region not in ("eu", "us"):
        return result("not_readable", "error")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BWS_", "RUST_"))}
    env["BWS_ACCESS_TOKEN"] = token
    secret_id = LOGIN_REF.fullmatch(ref).group(1)
    try:
        r = subprocess.run(["bws", "--config-file", os.path.join(KIT, "bws.toml"), "--profile", region, "--output",
                            "json", "secret", "get", secret_id], capture_output=True, text=True, env=env, timeout=30)
    except subprocess.TimeoutExpired:
        return result("not_readable", "timeout")
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return result("not_readable", "error")
    if r.returncode != 0:               # the CLI's text is only searched, never kept or shown
        text = (r.stdout + r.stderr).lower()
        return result("not_readable", "not_found" if "404" in text or "not found" in text else
                      "no_access" if any(w in text for w in ("401", "403", "unauthorized", "forbidden")) else "error")
    try:
        item = json.loads(r.stdout)
        value = json.loads(item["value"])
        good = str(item["id"]).lower() == secret_id.lower() and isinstance(value, dict) and \
            isinstance(value.get("username", ""), str) and isinstance(value.get("password"), str) and value["password"]
    except (ValueError, KeyError, TypeError):
        good = False
    return result("readable", "ok") if good else result("not_readable", "bad_format")


def vault_readable():
    """The vault files that this user (and so its agents) can read: the token and the handover key."""
    return [name for name in ("bws-token", "age.key") if os.access(os.path.join(HOME, name), os.R_OK)]


def vault_status(agent):
    """For the machine report: the vault mode, whether this machine's agents can read neither vault file, and, in
    every mode, the public status of this machine's broker (shared broker callers): callers, its public key, what its
    own program hashes to and when it was read. The hub compares that key with the store key that the owner trusts;
    it never takes it.

    `broker_source` is the SHA-256 of the broker program that the broker runs. A release never reinstalls that file
    (WORKER-INSTALL.md, "Vault broker"), so this machine's revision says nothing about its vault, and the hub
    compares this checksum with its own vault.py instead. None when no broker answers, or when the one that answers
    does not say — a Mac's signed helper, or a broker older than this — because unknown is not behind."""
    status = broker_status()
    recipient, source = (status.get("recipient"), status.get("source")) if status else (None, None)
    broker = {"callers": (status.get("callers") is True) if status else None,
              "creates": (status.get("creates") is True) if status else None,       # create_login
              "captures": (status.get("captures") is True) if status else None,     # save_key
              "requests": (status.get("requests") is True) if status else None,     # call_api
              "broker_recipient": recipient if isinstance(recipient, str) else None,
              "broker_source": source if isinstance(source, str) and re.fullmatch(r"[0-9a-f]{64}", source) else None,
              "broker_read_at": time.time() if status else None}
    mode = vault_mode(agent)
    if mode == "none":
        return dict(broker, mode="none", isolated=None)
    readable = bool(vault_readable())
    if mode == "broker":
        return dict(broker, mode="broker", isolated=status is not None and not readable)
    return dict(broker, **({"mode": "file", "isolated": False} if readable else {"mode": None, "isolated": None}))


def broker_job(agent, job):
    """In broker mode the helper only passes a hub job to the vault broker. It never holds a login."""
    readable = vault_readable()
    if readable:
        raise Refused("this machine uses the vault broker, but ~/.fleet/" + " and ~/.fleet/".join(readable) +
                      " can still be read by the agents' user; the owner must move them to the vault")
    if not isinstance(job, int) or isinstance(job, bool) or job < 1:
        raise Refused("with the vault broker, a login is used only for a sign-in or handover job from the hub")
    return job


def bws_get(secret_id):
    """Fetch one secret (file mode). Token and plaintext stay in this helper and its CLI child."""
    return login_from_item(bws_item(secret_id), secret_id)


def read_values(agent):
    """read in file mode (owner feature secrets): the hub names the API keys that this agent reads in its sessions;
    this helper reads each with its own token, as for a sign-in, and registers the value's fingerprint at the hub
    before a session gets it. {"env": {NAME: value}, "missing": [NAME]}."""
    code, data = http("POST", f"/helper/vault/{agent}/read", {}, timeout=15)
    found = data.get("secrets") if isinstance(data, dict) else None
    if code != 200 or not isinstance(found, list) or data.get("ok") is not True:
        reason = data.get("error") if isinstance(data, dict) else None
        raise Refused("the hub did not name the secrets to read" + (f": {reason}" if isinstance(reason, str) else ""))
    env, missing = {}, []
    for x in found[:32]:
        name, ref, salts = (x.get("env"), x.get("login"), x.get("fingerprint_salts")) if isinstance(x, dict) else (None,) * 3
        salt = salts.get("password") if isinstance(salts, dict) else None
        if not (isinstance(name, str) and ENV_NAME.fullmatch(name) and isinstance(ref, str) and LOGIN_REF.fullmatch(ref)
                and isinstance(salt, str) and re.fullmatch(r"[0-9a-f]{32}", salt)):
            raise Refused("the hub named no valid secrets to read")
        try:
            sid = LOGIN_REF.fullmatch(ref).group(1)
            item = bws_item(sid)
            value = json.loads(item["value"]).get("password") if item.get("id", "").lower() == sid.lower() else None
            if not isinstance(value, str) or not value:
                raise Refused("the item has no value")
            if len(value) >= 8 and not re.search(r"\s", value):     # what the hub's filter can find in a text
                prints = [{"kind": "password", "hash": hashlib.sha256((salt + value).encode()).hexdigest(), "len": len(value)}]
                code, done = http("POST", f"/helper/vault/{agent}/fingerprints", {"login": ref.lower(), "fingerprints": prints},
                                  timeout=15)
                if code != 200 or not isinstance(done, dict) or done.get("ok") is not True or \
                        (done.get("stored") or 0) + (done.get("known") or 0) != 1:
                    raise Refused("the value could not be protected in messages")
        except (Refused, ValueError, KeyError, TypeError, AttributeError):
            missing.append(name)
            continue
        env[name] = value
    return {"env": env, "missing": missing}


def bws_item(secret_id):
    """One Bitwarden item as the CLI gives it (file mode): {id, key (its name), value}. Token and plaintext stay in
    this helper and its CLI child."""
    if not shutil.which("bws"):
        raise Refused("the Bitwarden Secrets Manager CLI (bws) is not installed on this machine")
    region = TEAM.get("bitwarden_region")
    if region not in ("eu", "us"):
        raise Refused("set bitwarden_region to eu or us in team.json before using Bitwarden")
    # Do not let an inherited CLI profile, server URL or debug setting override the pilot.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BWS_", "RUST_"))}
    env["BWS_ACCESS_TOKEN"] = bws_token()
    try:
        result = subprocess.run(["bws", "--config-file", os.path.join(KIT, "bws.toml"),
                                 "--profile", region, "--output", "json", "secret", "get", secret_id],
                                capture_output=True, text=True, env=env, timeout=30)
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise Refused("Bitwarden could not be reached by the helper; ask your requester") from None
    if result.returncode != 0:
        # CLI errors can contain its response or token. Never return either stream.
        raise Refused("Bitwarden refused this login; check this machine's token, project access, region and secret ID")
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def login_from_item(item, secret_id):
    """A Bitwarden item, from the CLI or the vault broker, as {username, password, url, label[, totp]}. An API key
    (owner feature secrets) is such an item with an empty user name."""
    try:
        if not isinstance(item, dict) or item.get("id", "").lower() != secret_id.lower():
            raise ValueError()
        value = {"username": "", **json.loads(item["value"])}
        if not isinstance(value["username"], str) or not all(isinstance(value.get(k), str) and value[k]
                                                            for k in ("password", "url")):
            raise ValueError()
        url = urllib.parse.urlsplit(value["url"])
        if url.scheme not in ("https", "http") or not url.hostname or url.username or url.password:
            raise ValueError()
        if not isinstance(item.get("key"), str) or not item["key"]:
            raise ValueError()
        extra = {}
        if value.get("totp") is not None:
            if not isinstance(value["totp"], str):
                raise ValueError()
            totp_code(value["totp"])                    # an unusable seed is refused here, not at sign-in
            extra["totp"] = value["totp"]
        return {k: value[k] for k in ("username", "password", "url")} | extra | {"label": item["key"]}
    except (ValueError, KeyError, TypeError, AttributeError):
        raise Refused("Bitwarden login is invalid: its value must be JSON with username, password and an http(s) url, "
                      "and an optional authenticator seed in totp") from None


def totp_code(seed, at=None):
    """An RFC 6238 authenticator code from a base32 seed or an otpauth:// URI. Only the helper sees either."""
    digits, period, algorithm = 6, 30, "sha1"
    if seed.startswith("otpauth://"):
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(seed).query))
        seed = params.get("secret", "")
        digits, period = int(params.get("digits", 6)), int(params.get("period", 30))
        algorithm = params.get("algorithm", "SHA1").lower()
    if algorithm not in ("sha1", "sha256", "sha512") or not 6 <= digits <= 8 or not 1 <= period <= 300:
        raise ValueError("unsupported authenticator settings")
    seed = re.sub(r"[\s-]", "", seed).upper()
    key = base64.b32decode(seed + "=" * (-len(seed) % 8))
    if not key:
        raise ValueError("empty authenticator seed")
    mac = hmac.new(key, struct.pack(">Q", int((time.time() if at is None else at) // period)),
                   getattr(hashlib, algorithm)).digest()
    offset = mac[-1] & 0x0F
    return str((struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % 10 ** digits).zfill(digits)


def split_ref(ref):
    m = re.fullmatch(r"bws://([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})", ref.strip())
    if not m:
        raise Refused("a Bitwarden login is named bws://<secret-id>, where secret-id is its UUID")
    return m.group(1)


def read_login(ref, agent=None, task=""):
    """Return {"username", "password", "url", "label"}. Nothing here is ever printed. A handover opened for a task
    belongs to that task from then on, and the hub throws it away when the task ends. Never in broker mode: there
    only the vault broker opens logins."""
    if vault_mode(agent) == "none":
        raise Refused(NO_CREDENTIALS)
    if vault_mode(agent) == "broker":
        raise Refused("this machine uses the vault broker: only the broker opens logins")
    ref = ref.strip()
    if ref.startswith("sealed:"):
        sid = re.sub(r"\D", "", ref)
        code, data = http("GET", f"/sealed/{sid}?" + urllib.parse.urlencode({"agent": agent or "", "task": task or ""}))
        if code != 200:
            raise Refused(f"cannot open {ref}: {data.get('error', code)}")
        if not shutil.which("age"):
            raise Refused("age is not installed on this machine, so it cannot open a handover")
        r = subprocess.run(["age", "-d", "-i", os.path.join(HOME, "age.key")], input=data["ciphertext"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise Refused(f"cannot unlock {ref} with this machine's key")
        login = json.loads(r.stdout)
        login["label"] = data.get("label") or ref
        return login
    return bws_get(split_ref(ref))


def apple_string(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


FILL_JS = ("(function(u,p){var pw=document.querySelector('input[type=password]');if(!pw){return 'no-form';}"
           "var f=pw.form;var user=null;if(f){user=f.querySelector('input[type=email],input[type=text],input:not([type])');}"
           "function set(el,v){var d=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value');"
           "d.set.call(el,v);el.dispatchEvent(new Event('input',{bubbles:true}));"
           "el.dispatchEvent(new Event('change',{bubbles:true}));}"
           "if(user&&u){set(user,u);}set(pw,p);if(f){if(f.requestSubmit){f.requestSubmit();}else{f.submit();}}"
           "return 'filled';})(%s,%s)")
OTP_FIELD = ("input[autocomplete=one-time-code],input[name*=otp i],input[id*=otp i],input[name*=totp i],"
             "input[id*=totp i],input[name=code],input[id=code]")
# A page that is still loading can get its form later. Without a field it is 'loading', never 'none': do_signin takes
# only 'none' for signed in and only 'otp' for a code page. Every other answer is a refusal.
CHECK_JS = ("(function(){return document.querySelector('input[type=password]')?'form':"
            "(document.querySelector('%s')?'otp':(document.readyState==='loading'?'loading':'none'));})()" % OTP_FIELD)
OTP_JS = ("(function(c){var el=document.querySelector('%s');if(!el){return 'no-form';}"
          "var d=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value');d.set.call(el,c);"
          "el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));"
          "var f=el.form;if(f){if(f.requestSubmit){f.requestSubmit();}else{f.submit();}}return 'filled';})(%%s)" % OTP_FIELD)
# Submit only one form. Return its stable Chrome tab ID and check only that tab.
# Submitting all matching tabs can race when the site replaces its session cookie.
CHROME_SCRIPT = """tell application "Google Chrome"
	repeat with w in windows
		repeat with t in tabs of w
			if %s is "0" or (id of t as text) is %s then
				if (URL of t) starts with %s then
					set r to (execute t javascript %s)
					if r is %s then return id of t
				end if
			end if
		end repeat
	end repeat
	return 0
end tell
"""


def chrome_tab(origin, js, expect, tab_id=0):
    wanted = apple_string(str(int(tab_id)))  # Chrome's scripting dictionary defines tab IDs as text.
    script = CHROME_SCRIPT % (wanted, wanted, apple_string(origin + "/"), apple_string(js), apple_string(expect))
    r = subprocess.run(["osascript", "-"], input=script, capture_output=True, text=True)
    if r.returncode != 0:       # stderr is never echoed: it can quote the script, and the script holds the login
        if "JavaScript" in r.stderr and ("turned off" in r.stderr or "Apple Events" in r.stderr):
            raise Refused("Chrome refuses scripts. Someone must turn on View > Developer > "
                          "Allow JavaScript from Apple Events in Chrome on this machine.")
        code = re.search(r"\(-?\d+\)", r.stderr)
        raise Refused(f"Chrome could not be scripted {code.group(0) if code else ''}".strip())
    try:
        return int(r.stdout.strip() or 0)
    except ValueError:
        return 0


PROVIDER_SITES = ("claude.ai", "chatgpt.com", "x.ai")   # the sign-in sites of the model providers themselves


def provider_site(host):
    """True for a model provider's own sign-in site, with its subdomains. Fleet holds no provider credential: the
    owner signs in to their own account in their own session, through the provider's own flow, and that is the
    condition on which Anthropic permits an operator to host the unmodified Claude Code at all. So no login of
    Fleet's may be one, and sign_in, create_login and save_key refuse these sites. An API key of a provider's paid
    API (console.anthropic.com) is another thing and stays allowed: it is a key the owner pays per call, not the
    subscription credential of a session."""
    host = str(host or "").lower().rstrip(".")
    return any(host == site or host.endswith("." + site) for site in PROVIDER_SITES)


PROVIDER_REFUSAL = ("Fleet signs in to no model provider: " + ", ".join(PROVIDER_SITES) + ". The owner signs in to "
                    "their own account in their own session, through the provider's own flow. Ask your requester.")


# ---------------------------------------------------------------- the owner signs this machine in (machine_signin)
#
# The owner starts it in the app; the provider's own flow runs here, on the machine the credential belongs to, and
# the provider's client writes the credential here. The hub only passes the link along and hands back the code that
# the provider gave the owner. Nothing of that is written down, printed or logged by Fleet, and Grok is refused
# because xAI's terms do not let a third party run it this way.
LOGIN_SESSION = "fleet-login-%s"            # a tmux session of its own: the agent's own session keeps its windows
LOGIN_URL_WAIT = 90                         # seconds for the provider's link to appear on the screen
LOGIN_CODE_WAIT = 840                       # seconds for the owner's code to come back through the hub
LOGIN_SETUP_ANSWERS = 5                     # first-run questions of its own that a sign-in may be asked past
LOGOUT_WAIT = 120                           # seconds for the runtime to report that it is signed out again
LOGIN_DONE_WAIT = 900                       # seconds for the runtime to report that it is signed in: as long
                                            # as the hub keeps the sign-in open (hub.LOGIN_LIFE), because the
                                            # owner is in a browser and three minutes called a real sign-in
                                            # failed. The hub ends it the moment this machine reports it.
# The sign-in a runtime's entry describes (lifecycle.RUNTIMES "sign_in"): the provider's own link as it is read
# from the screen, the code the provider prints, and whether it waits for a code the owner pastes back.
LOGIN_URLS = {name: e["sign_in"]["url"] for name, e in RUNTIMES.items() if e["sign_in"]}
LOGIN_SHOWN_CODE = {name: e["sign_in"]["shows_code"] for name, e in RUNTIMES.items()
                    if e["sign_in"] and e["sign_in"]["shows_code"]}
LOGIN_WANTS_CODE = tuple(name for name, e in RUNTIMES.items() if (e["sign_in"] or {}).get("wants_code"))
# How each runtime's own first-run question shows that it waits on a choice; absent for one that asks none.
LOGIN_SETUP_CURSOR = {name: e["sign_in"]["setup_cursor"] for name, e in RUNTIMES.items()
                      if e["sign_in"] and e["sign_in"]["setup_cursor"]}
# What may be typed into a sign-in window, and nothing else: letters, digits, dashes, and the `#` and the
# second half that Claude's own code carries (`<code>#<state>`, about 94 characters). No space, no control
# character and no shell or tmux metacharacter, because this text reaches a terminal as keys.
LOGIN_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._#-]{3,200}")
LOGIN_LABELS = {name: e["label"] for name, e in RUNTIMES.items() if e["sign_in"]}


def ready_runtime_config(home=None, kit=None):
    """Claude Code runs a first-run wizard — a theme, then a sign-in method — and it stands in front of everything
    else: the agent's own session and the owner's sign-in both wait at it for a person who is never there. The
    wizard runs until `hasCompletedOnboarding` is in the runtime's own file, whatever else is in it, and it asks
    once more for each folder it is started in ("Do you trust the files in this folder?", kept per folder as
    `hasTrustDialogAccepted`). The folder is the agent's own checkout, which the owner's own installer made. So the
    helper writes those two answers and the theme with them, and touches no other key. True when it wrote one.

    It is the helper that does this, not the installer, because every machine of every fleet has a helper and it
    runs before the first session of its agent."""
    path = os.path.join(home or os.path.expanduser("~"), ".claude.json")
    folder = os.path.realpath(kit or KIT)
    try:
        with open(path) as f:
            kept = json.load(f)
    except (OSError, ValueError):
        kept = {}
    if not isinstance(kept, dict):
        kept = {}
    projects = kept.get("projects")
    if not isinstance(projects, dict):
        projects = {}
    mine = projects.get(folder) if isinstance(projects.get(folder), dict) else {}
    if kept.get("hasCompletedOnboarding") and mine.get("hasTrustDialogAccepted"):
        return False
    kept["hasCompletedOnboarding"] = True
    kept.setdefault("theme", "dark")
    # Its offer of the new renderer comes back until it has been shown three times, and it blocks the window while
    # it stands there. Nobody is at this terminal to take it, so it is counted out before it is ever drawn.
    kept["fullscreenUpsellSeenCount"] = max(int(kept.get("fullscreenUpsellSeenCount") or 0), 3)
    projects[folder] = dict(mine, hasTrustDialogAccepted=True)
    kept["projects"] = projects
    try:
        tmp = path + ".fleet-new"
        with open(tmp, "w") as f:
            json.dump(kept, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def runtime_login_command(runtime, exe):
    """The provider's own sign-in, as one window command, from that runtime's own entry (lifecycle.RUNTIMES
    "sign_in"). A runtime whose entry carries none is refused here, and says why itself: Grok's terms do not let a
    third party run it this way, as conversation_command refuses Codex."""
    why = no_sign_in(runtime)
    if why:
        raise Refused(why)
    flow = runtime_fact(runtime, "sign_in")
    if not flow:
        raise Refused(f"Fleet drives no sign-in for {runtime}")
    return "exec " + shlex.join([exe, *flow["args"]])


def runtime_logout_command(runtime, exe):
    """The provider's own sign-out, as one window command, from that runtime's own entry (lifecycle.RUNTIMES
    "sign_out"). It exists for one reason: a provider will not start a second sign-in while one is live, so an
    owner who wants a different account of the same provider needs this half first."""
    flow = runtime_fact(runtime, "sign_out")
    if not flow:
        raise Refused(f"Fleet drives no sign-out for {runtime}")
    return "exec " + shlex.join([exe, *flow["args"]])


# A link, a code, or anything long enough to be one. `login_prompt_line` takes these out before a word of a
# sign-in window leaves the machine.
LOGIN_SECRETISH = re.compile(r"https?://\S+|[A-Za-z0-9][A-Za-z0-9_.#-]{15,}")


def login_prompt_line(screen):
    """One safe line saying what the sign-in window is waiting at, so a sign-in that gave up says which screen it
    was stuck on instead of "printed no link".

    **The screen itself never leaves this machine.** It can hold the provider's link, the code it printed or the
    code the owner pasted, so anything that could be one of those is taken out first and what is left is one short
    line of ordinary words — the question, where there is one, else the first line that reads as a sentence."""
    lines = []
    for raw in (screen or "").splitlines():
        line = LOGIN_SECRETISH.sub("", raw).strip(" \t\u2502\u2503|*\u2500\u2501-=_\u276f\u203a>")
        if len(re.findall("[A-Za-z]", line)) >= 4:
            lines.append(line[:80])
    for line in lines:
        if line.endswith((":", "?")):
            return line
    return lines[0] if lines else ""


# **A sign-in window is read as lines**: which question is waiting, and whether a cursor stands on one of them.
# `CONTROL` is for text about to be typed into a terminal, where a newline is a keypress like any other, so it
# takes the newlines out with the rest — and the whole window came back as one long line. Nothing matched a
# question then, no first-run question was ever answered, and every sign-in gave up after ninety seconds saying
# it was waiting at the banner, which was simply the start of that one line (the owner, 21 October 2026).
SCREEN_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")      # every control character but the newline


# **A link the runtime drew across more than one line of its window.** Claude Code prints its sign-in link at
# the full width of the window and breaks it itself, so no line of the capture holds the whole of it and tmux's
# own join leaves those lines apart. The owner opened what the first line held and the provider answered "Missing
# redirect_uri parameter" (21 October 2026). A line that runs to the edge of a real window is continued by the
# line under it, for as long as that line is link and nothing else.
LINK_TAIL = re.compile(r"^[A-Za-z0-9/_?=&.%#+~:@!$'()*,;-]+$")
LINK_WIDTH = 40                             # narrower than any terminal: then no line of it reaches an edge


def whole_link(screen, pattern):
    """The provider's own link, put back together from the lines it was drawn on. None where the screen has none."""
    raw = screen.splitlines()
    width = max((len(line) for line in raw), default=0)
    edge = [width >= LINK_WIDTH and len(line) >= width for line in raw]
    lines = [line.strip(" \t\u2502\u2503\u2551|") for line in raw]
    for i, line in enumerate(lines):
        found = pattern.search(line)
        if not found:
            continue
        link, goes_on = found.group(0), edge[i] and found.end() == len(line)
        for j in range(i + 1, len(lines)):
            if not goes_on or not lines[j] or not LINK_TAIL.fullmatch(lines[j]):
                break
            link += lines[j]
            goes_on = edge[j]
        return link
    return None


def login_screen(session):
    """What the sign-in window shows now, as its lines. `capture-pane -p` gives the text without escapes; -J joins
    a link that the terminal wrapped over two lines."""
    r = tmux("capture-pane", "-p", "-J", "-t", f"{session}:=login")
    return SCREEN_CONTROL.sub(" ", r.stdout) if r.returncode == 0 else ""


def type_login_code(session, code):
    """The one code the provider gave the owner, into the window that is waiting for it. Letters, digits and dashes
    only: anything else would act as keys in that terminal. The code is never printed or kept."""
    if not LOGIN_CODE.fullmatch(code or ""):
        raise Refused("that is not a sign-in code")
    target = f"{session}:=login"
    for keys in (("-l", "--", code), ("Enter",)):
        if keys == ("Enter",):
            time.sleep(0.6)             # let the window take the text in before the Enter
        if tmux("send-keys", "-t", target, *keys).returncode != 0:
            raise Refused("the sign-in window did not take the code")


def do_signin(name, agent, task="", job=None):
    if vault_mode(agent) == "none":
        raise Refused(NO_CREDENTIALS)
    if sys.platform != "darwin":
        raise Refused("signing in needs a Mac with Google Chrome")
    if vault_mode(agent) == "broker":           # the signed Mac vault helper fills the form; no login comes here
        done = broker_job_request(agent, {"op": "signin", "job": broker_job(agent, job)})
        return (f"Signed in on {done.get('origin')} with the login \"{done.get('label')}\"" +
                (", with its authenticator code" if done.get("authenticator") is True else "") +
                ". The password was never shown to you.")
    login = read_login(name, agent, task)
    parts = urllib.parse.urlsplit(login.get("url") or "")
    if not parts.scheme or not parts.netloc:
        raise Refused("this login has no website address in the vault, so there is no safe place to type it")
    if provider_site(parts.hostname):
        raise Refused(PROVIDER_REFUSAL)
    origin = f"{parts.scheme}://{parts.netloc}"
    filled = chrome_tab(origin, FILL_JS % (json.dumps(login["username"]), json.dumps(login["password"])), "filled")
    if not filled:
        raise Refused(f"no Chrome tab on {origin} shows a sign-in form. Open the site's sign-in page first, then ask again.")
    time.sleep(3)
    code = False
    signed = chrome_tab(origin, CHECK_JS, "none", tab_id=filled) == filled
    if not signed and login.get("totp"):
        # an authenticator code from the seed in the vault; a text-message code still goes to the requester
        if chrome_tab(origin, CHECK_JS, "otp", tab_id=filled) == filled and \
                chrome_tab(origin, OTP_JS % json.dumps(totp_code(login["totp"])), "filled", tab_id=filled) == filled:
            time.sleep(3)
            code = True
            signed = chrome_tab(origin, CHECK_JS, "none", tab_id=filled) == filled
        if not signed:
            raise Refused(f"the sign-in on {origin} could not be confirmed after the authenticator code step. "
                          f"Ask your requester.")
    if not signed:
        raise Refused(f"the form on {origin} was filled, but sign-in could not be confirmed on that tab. "
                      f"It may still show a form, have closed, or have left this site. If it asks for a one-time code: "
                      f"an authenticator code needs its seed in the vault login; a text-message code goes to your requester.")
    return (f"Signed in on {origin} with the login \"{login['label']}\"" + (", with its authenticator code" if code else "") +
            ". The password was never shown to you.")


def do_save(agent, job, op):
    """create_login (op create) and save_key (op capture): have this machine's vault make and save a new login for a
    hub job. Only the job number goes to the vault and only the login's name comes back: the helper never has a value.
    The vault confirms the job with the hub and takes the request from that answer. For a create it makes the
    password; for a capture it reads the key from the Chrome page itself. It sends at most one create call."""
    if vault_mode(agent) != "broker":
        raise Refused("a new login is made only by this machine's vault broker, and this agent has no broker access")
    status = broker_status()
    if not (status and status.get("callers") is True and status.get(SAVES[op]) is True) or not load_agent_key(agent):
        raise Refused("this machine's vault cannot " + ("save a key" if op == "capture" else "make a login"))
    done = broker_job_request(agent, {"op": op, "job": broker_job(agent, job)})
    if done.get("job") != job or not isinstance(done.get("login"), str) or not LOGIN_REF.fullmatch(done["login"]):
        raise Unknown("the vault gave no name for the new login")
    return (f"The vault saved the key as {done['login'].lower()}. The key never left the vault." if op == "capture" else
            f"The vault made {done['login'].lower()}. Its password never left the vault.")


def do_request(agent, job):
    """call_api: have this machine's vault send the request of a hub job. Only the job number goes to the vault: it
    takes the request and the login from the hub's confirmation of the job, and the key never comes here. Returns the
    report for the hub: the status of the service's answer and the body that the vault gave. Refused: the vault
    proved that nothing went out, or it was not asked. Unknown: the request can have gone out."""
    if vault_mode(agent) != "broker":
        raise Refused("an API request is sent only by this machine's vault broker, and this agent has no broker access")
    status = broker_status()
    if not (status and status.get("callers") is True and status.get("requests") is True) or not load_agent_key(agent):
        raise Refused("this machine's vault cannot send an API request")
    done = broker_job_request(agent, {"op": "request", "job": broker_job(agent, job)})
    code = done.get("status")
    if done.get("job") != job or not isinstance(code, int) or isinstance(code, bool) or not 100 <= code <= 599:
        raise Unknown("the vault gave no status for the request")
    report = {"ok": True, "status": code, "text": f"HTTP {code}"}
    if done.get("withheld") in ("key", "binary"):       # the vault held the body back
        report["withheld"] = done["withheld"]
    elif isinstance(done.get("body"), str):
        report["body"] = done["body"]
    if done.get("truncated") is True:
        report["truncated"] = True
    return report


def do_handover(name, agent, to, task="", job=None):
    if vault_mode(agent) == "none":
        raise Refused(NO_CREDENTIALS)
    if vault_mode(agent) == "broker":           # the broker locks the login for the receiver's pinned key
        done = broker_job_request(agent, {"op": "handover", "job": broker_job(agent, job)})
        if done.get("to") != to or "AGE ENCRYPTED FILE" not in str(done.get("ciphertext")):
            raise Refused("the vault broker gave no locked copy for " + to)
        code, data = http("POST", "/sealed", {"sender": agent, "recipient": to, "task": task or "", "job": job,
                                             "label": done.get("label") or "", "ciphertext": done["ciphertext"]})
        if code != 200:
            raise Refused(f"the hub refused the handover: {data.get('error', code)}")
        return f"{data['ref']} is the name of the locked copy. Only {to} can open it. Pass this name on, never the login."
    if not shutil.which("age"):
        raise Refused("age is not installed on this machine, so nothing can be locked")
    login = read_login(name, agent, task)
    code, data = http("GET", f"/keys/{urllib.parse.quote(to)}")
    if code != 200:
        raise Refused(f"{to} has no key with the hub (is its helper running, with age installed?)")
    r = subprocess.run(["age", "-a", "-r", data["pubkey"]], capture_output=True, text=True,
                       input=json.dumps({k: login[k] for k in ("username", "password", "url", "totp") if k in login}))
    if r.returncode != 0:
        raise Refused("could not lock the login")
    code, data = http("POST", "/sealed", {"sender": agent, "recipient": to, "task": task or "", "job": job,
                                         "label": login["label"], "ciphertext": r.stdout})
    if code != 200:
        raise Refused(f"the hub refused the handover: {data.get('error', code)}")
    return f"{data['ref']} is the name of the locked copy. Only {to} can open it. Pass this name on, never the login."


def cmd_signin(args):
    try:
        print(do_signin(args.login, me(args)))
    except Refused as e:
        die(str(e))


def cmd_handover(args):
    try:
        print(do_handover(args.login, me(args), args.to, args.task or ""))
    except Refused as e:
        die(str(e))


def cmd_register(args):
    try:
        login = read_login(args.login)
    except Refused as e:
        die(str(e))
    pw = login["password"]
    if len(pw) < 8 or any(c.isspace() for c in pw):
        die("not registered: Fleet finds a login in text only when it has 8 or more characters and no spaces")
    salt = secrets.token_hex(8)
    data = {"label": login["label"], "salt": salt, "len": len(pw),
            "hash": hashlib.sha256((salt + pw).encode()).hexdigest(),
            "username": login["username"] if args.site else ""}
    code, resp = http("POST", "/secrets", data)
    if code == 200:
        print(f"registered \"{login['label']}\": the hub now knows this login's fingerprint, not the login")
    else:
        die(f"not registered: {resp.get('error', code)}")


def cmd_leakcheck(args):
    try:
        login = read_login(args.login, me(args) if args.login.strip().startswith("sealed:") else None)
    except Refused as e:
        die(str(e))
    needle = login["password"].encode()
    hits, scanned = [], 0
    for root in [os.path.expanduser(p) for p in ("~/.claude", "~/.codex", "~/.fleet", KIT)]:
        for folder, dirs, files in os.walk(root):
            for name in files:
                path = os.path.join(folder, name)
                try:
                    if os.path.getsize(path) > 200 * 1024 * 1024:
                        continue
                    with open(path, "rb") as f:
                        scanned += 1
                        if needle in f.read():
                            hits.append(path)
                except OSError:
                    continue
    r = subprocess.run(["git", "-C", KIT, "log", "--all", "-p", "--no-color"], capture_output=True)
    if r.returncode == 0 and needle in r.stdout:
        hits.append("the git history of this folder")
    r = tmux("list-panes", "-a", "-F", "#{session_name}:#{window_name}.#{pane_index}") if shutil.which("tmux") else None
    for pane in [p for p in (r.stdout.split("\n") if r else []) if p.startswith("fleet-")]:
        cap = tmux("capture-pane", "-p", "-J", "-S", "-", "-t", pane.replace(":", ":=", 1))
        if login["password"] in cap.stdout:
            hits.append(f"tmux screen {pane}")
    try:
        code, data = http("GET", "/leakcheck")
        hub_hits = data.get("hits", []) if code == 200 else [f"hub check failed ({code})"]
    except (urllib.error.URLError, OSError):
        hub_hits = ["hub unreachable"]
    print(f"Looked through {scanned} files on this machine, this folder's git history, its fleet sessions, "
          f"and the hub's records.")
    for h in hits:
        print(f"  FOUND on this machine: {h}")
    for h in hub_hits:
        print(f"  FOUND in the hub: {h}")
    if not hits and not hub_hits:
        print("  0 places hold the login. PASS")
    sys.exit(1 if hits or hub_hits else 0)


def cmd_git_credential(args):
    """git's credential helper in a task session (owner feature integrations; git_credentials): the session's token
    for the github.com repository that git names, from FLEET_GIT_TOKENS. Only get answers; git stores nothing here."""
    if args.action != "get":
        return
    asked = dict(line.split("=", 1) for line in sys.stdin.read().splitlines() if "=" in line)
    try:
        tokens = json.loads(os.environ.get("FLEET_GIT_TOKENS") or "{}")
    except ValueError:
        return
    path = asked.get("path", "").strip("/").lower()
    repo = path[:-4] if path.endswith(".git") else path
    token = tokens.get(repo) if isinstance(tokens, dict) and (asked.get("protocol"), asked.get("host")) == \
        ("https", "github.com") else None
    if isinstance(token, str) and GITHUB_TOKEN.fullmatch(token):
        print(f"username=x-access-token\npassword={token}")


def main():
    ap = argparse.ArgumentParser(description="Founderizer helper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("addresses"); p.add_argument("pairs", nargs="*", metavar="name=address")
    p.set_defaults(fn=cmd_addresses)
    p = sub.add_parser("connect"); p.add_argument("agent"); p.set_defaults(fn=cmd_connect)
    p = sub.add_parser("check"); p.add_argument("agent"); p.set_defaults(fn=cmd_check)
    p = sub.add_parser("run"); p.add_argument("agent")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("start"); p.add_argument("agent"); p.set_defaults(fn=cmd_start)
    p = sub.add_parser("hook"); p.add_argument("kind", choices=["permission", "activity"])
    p.add_argument("agent"); p.add_argument("task"); p.set_defaults(fn=cmd_hook)
    p = sub.add_parser("service"); p.add_argument("name"); p.set_defaults(fn=cmd_service)
    p = sub.add_parser("key"); p.add_argument("agent"); p.add_argument("--rotate", action="store_true")
    p.set_defaults(fn=cmd_key)
    p = sub.add_parser("chat-session"); p.add_argument("agent"); p.add_argument("session")
    p.add_argument("--runtime", choices=MANAGER_RUNTIMES,
                   help="the runtime of that conversation, when it is not the agent's runtime now (after a switch)")
    p.set_defaults(fn=cmd_chat_session)
    p = sub.add_parser("verify"); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("verify-case"); p.add_argument("--case"); p.add_argument("--run")
    p.set_defaults(fn=cmd_verify_case)
    p = sub.add_parser("watch"); p.set_defaults(fn=cmd_watch)
    p = sub.add_parser("say"); p.add_argument("text"); p.set_defaults(fn=cmd_say)
    p = sub.add_parser("board"); p.set_defaults(fn=cmd_board)
    p = sub.add_parser("hub"); p.set_defaults(fn=cmd_hub)
    p = sub.add_parser("register"); p.add_argument("login"); p.add_argument("--site", action="store_true")
    p.set_defaults(fn=cmd_register)
    p = sub.add_parser("signin"); p.add_argument("login"); p.add_argument("--agent"); p.set_defaults(fn=cmd_signin)
    p = sub.add_parser("handover"); p.add_argument("--to", required=True); p.add_argument("--login", required=True)
    p.add_argument("--task"); p.add_argument("--agent"); p.set_defaults(fn=cmd_handover)
    p = sub.add_parser("leakcheck"); p.add_argument("login"); p.add_argument("--agent"); p.set_defaults(fn=cmd_leakcheck)
    p = sub.add_parser("git-credential"); p.add_argument("action"); p.set_defaults(fn=cmd_git_credential)
    args = ap.parse_args()
    global AGENT
    AGENT = getattr(args, "agent", None) or os.environ.get("FLEET_AGENT") or None      # whose key http() sends
    try:
        args.fn(args)
    except KeyboardInterrupt:
        print()
    except BrokenPipeError:         # the reader of our output went away, as in `fleet.py board | head`
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        die(f"cannot reach the hub at {HUB}: {getattr(e, 'reason', e)}")


if __name__ == "__main__":
    main()

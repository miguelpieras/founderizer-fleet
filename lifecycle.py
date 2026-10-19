"""Machine status reports and idle-only Fleet updates for one helper (owner API 1.2).

The helper reports what its machine runs and how it is protected, using only commands that need
no root. It updates its own Fleet checkout only when it has no task session: a fast-forward to the
hub's revision, then a restart of its own service. A failed step is reported; nothing is forced.
While an update runs, no task or chat session can start (see session_start).
"""
import contextlib
import json
import os
import plistlib
import pwd
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

REPORT_EVERY = 300          # seconds between status reports when nothing changes
OWNER_ONLY = re.compile(r"(?:^|/)(?:test_[^/]*|onboard|ui_fixture|selftest|verify|pilot|vault)\.py$")  # never loaded here
TICK = 15                   # seconds between idle checks
RESTART_GRACE = 120         # seconds this process may live on after it asked for its own restart
CONFIG_BRANCH = "config"    # the configuration ref that the fleet runs: the hub writes it, the code branch waits
                            # for a deployment (RULES.md "Where work goes")
BRANCH_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,100}")


FEATURES = ["trigger_check", "agent_setup", "trigger_code", "screen_model", "agent_key", "broker_callers",
            "runtime_switch", "login_create", "login_capture", "api_request",
            "app_server_tasks",     # helper abilities that the hub may use; app_server_tasks: app_server_task below
            "manager_switch",       # the manager's helper closes its previous runtime's session after a switch
            "mcp_program"]          # it resolves a command MCP server's program for POST /ui/mcp/check
SWITCH_RUNTIMES = ("codex", "claude")       # the runtimes that an owner can switch between (owner feature runtime_switch)
# Where this helper leaves its sign-in reading for `fleet.py verify` to read, in the home of the user whose agents
# these are: ~/.fleet/signed-in-<agent>.json. verify imports this one path, so the writer and the reader of the file
# cannot drift apart (Lifecycle.remember_signed_in says why only a Mac needs it).
FLEET_DIR = ".fleet"
SIGNED_IN_FILE = "signed-in-%s.json"
MODELS_EVERY = 6 * 3600         # seconds between readings of the runtime's model options
MODEL_NAME = re.compile(r"[A-Za-z0-9._:/\[\]-]{1,120}")
EFFORT_NAME = re.compile(r"[a-z]{1,20}")
LOGIN_REASONS = ("ok", "not_found", "no_access", "bad_format", "no_token", "timeout", "error")
APP_SERVER = "app-server"       # the start policy of a Codex worker or lead whose tasks run in screen_runner.py
# GitHub repositories (owner feature integrations): owner/name in lower case, and the access that the owner gives.
REPO_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,38}/(?!\.\.?$)[a-z0-9._-]{1,100}")
REPO_ACCESS = ("read", "write")
# A GitHub App installation token, as the hub gets it and a session uses it: GitHub's current ones have about 400
# characters, with - and . in them.
GITHUB_TOKEN = re.compile(r"[A-Za-z0-9_.-]{20,4096}")
# The MCP servers that the owner can give an agent (owner feature integrations). The hub checks and shows this one
# catalog; the helper adds an agent's chosen servers to its Claude and Codex task sessions. A server has a command
# (with args) or a url. env: {the server's variable: the session variable whose value it gets}; bearer: the session
# variable whose value is a url server's bearer token. A session that lacks one of these variables gets no such server.
TOOL_CATALOG = {
    "github": {"title": "GitHub", "command": "github-mcp-server", "args": ["stdio"],
               "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "GH_TOKEN"}, "runtimes": ["claude", "codex"]},
}
# The owner's own MCP servers (owner feature mcp): team.json "mcp" holds them, the hub checks each one and the owner
# gives them to agents as it gives a catalog tool. mcp_server turns one into a catalog entry, so the hub, the helper
# and the sessions handle an owner server and a catalog tool in exactly one way.
MCP_NAME = re.compile(r"[a-z][a-z0-9-]{0,30}")       # the server's name, which is also the prefix of its tools
MCP_LIMIT = 32                                       # the most servers that team.json holds
MCP_RUNTIMES = ("claude", "codex")                   # the runtimes whose task sessions load a server
# How a url server proves who it is: nothing (a public server), the hub's own OAuth tokens (the owner connects it
# once and no refresh token ever leaves the hub), or an API key that the agent reads (owner feature secret_grants).
MCP_AUTH = ("none", "oauth", "key")
MCP_PROGRAM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")    # a stdio server's program, on PATH or in ~/.local/bin
MCP_ARG = re.compile(r"[ -~]{1,200}")
MCP_VAR = re.compile(r"[A-Z][A-Z0-9_]{0,59}")        # a variable name, as the secrets feature writes it
MCP_RESERVED = re.compile(r"(?:FLEET|BWS|LD|DYLD|ANTHROPIC|CLAUDE|CODEX)_[A-Z0-9_]*|PATH|HOME|SHELL|USER|LOGNAME|"
                          r"TMPDIR|TERM|PWD|IFS|ENV|BASH_ENV|ZDOTDIR|NODE_OPTIONS|PYTHONPATH|PYTHONHOME")
MCP_FIELDS = {"title", "url", "command", "args", "env", "auth", "key", "runtimes"}


def signed_in_path(home, agent):
    """The file where that agent's helper leaves its sign-in reading."""
    return os.path.join(home, FLEET_DIR, SIGNED_IN_FILE % agent)


def mcp_variable(name):
    """The session variable that holds the access token of one oauth server. FLEET_* is reserved for Fleet, so no API
    key of the owner can have this name."""
    return "FLEET_MCP_" + name.upper().replace("-", "_")


def mcp_address(value):
    """One url server's address, or None: https anywhere, http on 127.0.0.1 only, with a host, no user part and no
    fragment, at most 300 characters."""
    if not isinstance(value, str) or not 1 <= len(value) <= 300 or value.strip() != value or \
            any(c.isspace() for c in value):
        return None
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return None
    if parts.scheme == "http" and parts.hostname not in ("127.0.0.1", "localhost"):
        return None
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password or parts.fragment:
        return None
    return value


def mcp_server(name, entry):
    """One server of team.json "mcp" as a catalog entry (TOOL_CATALOG's shape, with custom and auth), or ValueError
    with the reason. A url server has url and, for auth "oauth" or "key", bearer: the session variable that holds its
    token. A stdio server has command, args and env ({the server's variable: the session variable whose value it
    gets}). A session that lacks one of these variables gets no such server."""
    if not isinstance(name, str) or not MCP_NAME.fullmatch(name):
        raise ValueError(f"the MCP server name {name!r} must be 1 to 31 characters a-z 0-9 - that start with a letter")
    if name in TOOL_CATALOG or name == "fleet":
        raise ValueError(f"the name {name} is Fleet's own: choose another one")
    if not isinstance(entry, dict) or set(entry) - MCP_FIELDS:
        raise ValueError(f"the MCP server {name} takes only {', '.join(sorted(MCP_FIELDS))}")
    title = entry.get("title")
    if not (isinstance(title, str) and 1 <= len(title.strip()) <= 60 and "\n" not in title):
        raise ValueError(f"the MCP server {name} needs a title of one line and 1 to 60 characters")
    auth = entry.get("auth", "none")
    if auth not in MCP_AUTH:
        raise ValueError(f"the MCP server {name}: auth must be {', '.join(MCP_AUTH)}")
    runtimes = entry.get("runtimes", list(MCP_RUNTIMES))
    if not isinstance(runtimes, list) or not runtimes or len(set(runtimes)) != len(runtimes) or \
            any(r not in MCP_RUNTIMES for r in runtimes):
        raise ValueError(f"the MCP server {name}: runtimes must be different names of {', '.join(MCP_RUNTIMES)}")
    variables = entry.get("env") or {}
    if not isinstance(variables, dict) or len(variables) > 8 or not all(
            isinstance(k, str) and MCP_VAR.fullmatch(k) and isinstance(v, str) and MCP_VAR.fullmatch(v)
            and not MCP_RESERVED.fullmatch(v) for k, v in variables.items()):
        raise ValueError(f"the MCP server {name}: env maps up to 8 of its own variable names to the names of API keys "
                         f"that the agent reads (A-Z 0-9 _, and not a name that a session sets itself)")
    made = {"title": title.strip(), "runtimes": sorted(runtimes), "custom": True, "auth": auth}
    if ("url" in entry) == ("command" in entry):
        raise ValueError(f"the MCP server {name} needs a url (a remote server) or a command (a program on the "
                         f"machine), not both")
    if "url" in entry:
        if not mcp_address(entry["url"]):
            raise ValueError(f"the MCP server {name}: url must be an https address (http only on 127.0.0.1) with a "
                             f"host, no user part and no #fragment, at most 300 characters")
        if "args" in entry:
            raise ValueError(f"the MCP server {name}: args is for a command server")
        made["url"] = entry["url"]
        if auth == "oauth":
            made["bearer"] = mcp_variable(name)
        elif auth == "key":
            key = entry.get("key")
            if not (isinstance(key, str) and MCP_VAR.fullmatch(key) and not MCP_RESERVED.fullmatch(key)):
                raise ValueError(f"the MCP server {name}: auth \"key\" needs key, the name of an API key that the "
                                 f"agent reads in its sessions")
            made["bearer"] = key
        if variables:
            raise ValueError(f"the MCP server {name}: env is for a command server; a url server uses key")
    else:
        if auth != "none":
            raise ValueError(f"the MCP server {name}: a command server has no auth; give its key in env")
        if "key" in entry:
            raise ValueError(f"the MCP server {name}: key is for a url server with auth \"key\"")
        args = entry.get("args", [])
        if not isinstance(entry["command"], str) or not MCP_PROGRAM.fullmatch(entry["command"]):
            raise ValueError(f"the MCP server {name}: command must be the name of a program on the machine (1 to 64 "
                             f"characters A-Z a-z 0-9 . _ -), which the machine has on its PATH or in ~/.local/bin")
        if not isinstance(args, list) or len(args) > 16 or not all(isinstance(a, str) and MCP_ARG.fullmatch(a) for a in args):
            raise ValueError(f"the MCP server {name}: args must be up to 16 arguments of 1 to 200 visible characters")
        made.update(command=entry["command"], args=list(args), env=dict(sorted(variables.items())))
    return made


def tool_needs(tool):
    """The session variables that one catalog entry needs: the variables of a stdio server, and the bearer variable of
    a url server. A session that lacks one of them gets no such server."""
    return sorted(set((tool.get("env") or {}).values()) | ({tool["bearer"]} if tool.get("bearer") else set()))


def tool_catalog(team):
    """The catalog of this team: Fleet's own tools and the owner's MCP servers of team.json "mcp". A server that does
    not pass mcp_server is left out, so a hand-written entry can never reach a session."""
    catalog = {name: dict(tool, custom=False, auth="none") for name, tool in TOOL_CATALOG.items()}
    for name, entry in sorted(((team or {}).get("mcp") or {}).items()):
        try:
            catalog[name] = mcp_server(name, entry)
        except ValueError:
            continue
    return catalog


def app_server_task(conf, needs):
    """True when a task with these needs runs in screen_runner.py (Codex app-server) and not in a terminal CLI: every
    screen task, and every task of a Codex worker or lead whose start policy is exactly app-server. The helper and the
    hub use this one rule."""
    return needs == "screen" or (conf.get("runtime") == "codex" and conf.get("start") == APP_SERVER
                                 and conf.get("role") in ("worker", "lead"))


def exe(name, home=None):
    """A runtime's executable: on PATH, else in ~/.local/bin; None when neither has it."""
    local = os.path.join(home or os.path.expanduser("~"), ".local", "bin", name or "")
    return shutil.which(name or "") or (local if os.path.exists(local) else None)


def codex_model_list(exe, timeout=20):
    """(options, error): the models and efforts that this machine's Codex account offers, from one app-server
    query (initialize, initialized, model/list). No thread and no turn starts, so no model runs."""
    folder = tempfile.mkdtemp(prefix="fleet-models-")
    try:
        proc = subprocess.Popen([exe, "app-server"], cwd=folder, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    except OSError as e:
        shutil.rmtree(folder, ignore_errors=True)
        return None, f"codex app-server did not start: {type(e).__name__}"
    lines, end, reply = queue.Queue(), time.monotonic() + timeout, None

    def pump():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    def answer(number):
        while time.monotonic() < end:
            try:
                line = lines.get(timeout=max(0.05, end - time.monotonic()))
            except queue.Empty:
                return None
            if line is None:
                return None
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("id") == number:
                return message
        return None

    threading.Thread(target=pump, daemon=True).start()
    try:
        proc.stdin.write(json.dumps({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "founderizer_models", "title": "Founderizer", "version": "1"}}}) + "\n")
        proc.stdin.flush()
        first = answer(1)
        if first and "result" in first:
            proc.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
            proc.stdin.write(json.dumps({"id": 2, "method": "model/list",
                                         "params": {"limit": 100, "includeHidden": False}}) + "\n")
            proc.stdin.flush()
            reply = answer(2)
    except (OSError, ValueError):
        reply = None
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
        for pipe in (proc.stdin, proc.stdout):
            try:
                pipe.close()
            except OSError:
                pass
        shutil.rmtree(folder, ignore_errors=True)
    if not reply:
        return None, f"codex app-server did not answer model/list in {timeout} seconds"
    if "error" in reply:
        message = (reply["error"] or {}).get("message") if isinstance(reply["error"], dict) else None
        return None, "codex app-server refused model/list" + (f": {str(message)[:200]}" if message else "")
    data = (reply.get("result") or {}).get("data") if isinstance(reply.get("result"), dict) else None
    options = []
    for d in data if isinstance(data, list) else []:
        model = d.get("model") or d.get("id") if isinstance(d, dict) else None
        if d.get("hidden") is True if isinstance(d, dict) else True:
            continue
        if not isinstance(model, str) or not MODEL_NAME.fullmatch(model):
            continue
        efforts = [e.get("reasoningEffort") for e in d.get("supportedReasoningEfforts") or [] if isinstance(e, dict)]
        default = d.get("defaultReasoningEffort")
        options.append({"model": model, "display_name": str(d.get("displayName") or model)[:120],
                        "default": d.get("isDefault") is True,
                        "efforts": [e for e in efforts if isinstance(e, str) and EFFORT_NAME.fullmatch(e)][:16],
                        "default_effort": default if isinstance(default, str) and EFFORT_NAME.fullmatch(default) else None})
    if not options:
        return None, "codex app-server listed no models"
    return options[:64], None


def claude_model_list(help_text):
    """(options, error) from the installed `claude --help`: the model aliases that it names and its effort levels.
    Claude also takes full model names that it does not list."""
    flat = " ".join(help_text.split())
    model = re.search(r"--model <model>(.*?)(?: --[a-z]|$)", flat)
    levels = re.search(r"--effort <level>[^(]*\(([a-z, ]+)\)", flat)
    aliases = list(dict.fromkeys(re.findall(r"'([a-z0-9][a-z0-9.\[\]-]{0,40})'", model.group(1)))) if model else []
    efforts = [x.strip() for x in levels.group(1).split(",") if EFFORT_NAME.fullmatch(x.strip())] if levels else []
    if not aliases:
        return None, "claude --help names no model aliases"
    return [{"model": a, "display_name": a, "default": False, "efforts": efforts, "default_effort": None}
            for a in aliases[:64]], None


class Updating(Exception):
    """No session may start: this helper is updating Fleet. The event stays pending for the new process."""


def _run(argv, timeout=20, cwd=None):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd, stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def checkout_branch(kit, run=None):
    """The branch that this checkout is on, or None when it is detached."""
    code, out, _ = (run or _run)(["git", "-C", kit, "symbolic-ref", "--short", "--quiet", "HEAD"])
    out = out.strip()
    return out if code == 0 and BRANCH_NAME.fullmatch(out) else None


def follow_config_ref(kit, run=None):
    """Bring this checkout onto the configuration ref and up to date with it, fast-forward only. The hub commits the
    owner's team.json, profiles and knowledge there and nothing else writes it, so an agent reads the current
    RULES.md, its profile and the owner's knowledge, while the code branch reaches this machine only with a
    deployment. Returns None when the checkout follows the ref, else the one reason why it stays as it is: a
    checkout with its own changes is never touched. An origin without the ref is a Fleet before the split of
    2026-10-17, and is brought up to date as it was before."""
    run = run or _run

    def git(*args, timeout=60):
        code, out, err = run(["git", "-C", kit, *args], timeout=timeout)
        return code, out.strip(), err.strip()

    code, _, err = git("fetch", "-q", "origin")
    if code != 0:
        return "git fetch failed: " + err[-200:]
    if git("rev-parse", "--verify", "--quiet", "refs/remotes/origin/" + CONFIG_BRANCH)[0] != 0:
        code, out, err = git("pull", "-q", "--ff-only")
        return None if code == 0 else "git pull failed: " + (err or out)[-200:]
    if git("status", "--porcelain", "--untracked-files=no")[1]:
        return "the checkout has its own changes, so it stays where it is"
    if checkout_branch(kit, run) != CONFIG_BRANCH:
        here = git("rev-parse", "--verify", "--quiet", "refs/heads/" + CONFIG_BRANCH)[0] == 0
        code, out, err = git("checkout", "-q", CONFIG_BRANCH) if here else \
            git("checkout", "-q", "-b", CONFIG_BRANCH, "--track", "origin/" + CONFIG_BRANCH)
        if code != 0:
            return f"the checkout did not move onto {CONFIG_BRANCH}: " + (err or out)[-200:]
    code, out, err = git("merge", "-q", "--ff-only", "origin/" + CONFIG_BRANCH)
    return None if code == 0 else "git merge failed: " + (err or out)[-200:]


class Lifecycle:
    def __init__(self, kit, agent, conf, post, busy, restart=None, run=_run, clock=time.time,
                 platform_name=sys.platform, home=None, vault=None, probe=None, models=codex_model_list, legacy=None,
                 switch=None, which=None):
        self.kit, self.agent, self.conf, self.post, self.busy = kit, agent, conf, post, busy
        self.legacy = legacy            # () -> the windows without a session credential (owner API 1.12)
        self.switch = switch            # () -> {other_runtime_windows, owner_chat_bound}, for a manager also
                                        # manager_bound and manager_handover (owner feature runtime_switch)
        self.vault = vault              # () -> {mode, isolated}: where this machine keeps the vault token
        self.probe = probe              # (probe job) -> the vault's answer: which profile logins it can open
        self.codex_models = models      # (codex executable) -> (options, error)
        self.models = None              # the latest reading of the runtime's model options
        self.screen_models = None       # the same for the screen runtime (owner API 1.11)
        self.run, self.clock, self.platform = run, clock, platform_name
        self.which = which              # (runtime) -> its executable, where this process cannot look for it itself
        self.home = home or os.path.expanduser("~")
        self.restart = restart or self.restart_service
        self.pending = None             # the hub revision of a requested update
        self.state = self.detail = None
        self.last_report = 0.0
        self.lock = threading.Lock()
        self.report_lock = threading.Lock()     # one report at a time, so that the hub gets them in the order read
        self.gate = threading.Lock()    # held while a session starts, and from the idle check to the restart
        self.restarting = None          # when this process asked for its own restart
        self.loaded = self.revision()   # the revision this process runs; the checkout can move ahead of it

    # ---- status
    def revision(self):
        code, out, _ = self.run(["git", "-C", self.kit, "rev-parse", "HEAD"])
        out = out.strip()
        return out if code == 0 and re.fullmatch(r"[0-9a-f]{40}", out) else None

    def running(self):
        """The revision this helper runs: its checkout when it started, or a later checkout that changed none of
        the files a helper loads (Python files and team.json), for example task outputs or a profile."""
        head = self.revision()
        if head and self.loaded and head != self.loaded:
            code, out, _ = self.run(["git", "-C", self.kit, "diff", "--name-only", self.loaded, head])
            if code == 0 and not any((p.endswith(".py") and not OWNER_ONLY.search(p)) or p == "team.json"
                                     for p in out.split()):
                self.loaded = head
        return self.loaded or head

    def os_info(self):
        name = None
        if self.platform == "darwin":
            code, out, _ = self.run(["sw_vers", "-productVersion"])
            name = "macOS " + out.strip() if code == 0 and out.strip() else None
        else:
            try:
                with open("/etc/os-release") as f:
                    m = re.search(r'^PRETTY_NAME="?([^"\n]+)', f.read(), re.M)
                name = m.group(1) if m else None
            except OSError:
                pass
        code, out, _ = self.run(["uname", "-r"])
        return {"name": name, "kernel": out.strip() if code == 0 and out.strip() else None}

    def security(self):
        s = {"unattended_upgrades": None, "pending_security": None, "reboot_required": None,
             "firewall": None, "tailscale": None}
        if self.platform == "darwin":
            code, out, _ = self.run(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"])
            if code == 0 and out.strip():
                s["firewall"] = "active" if "enabled" in out.lower() else "inactive"
        else:
            code, out, _ = self.run(["systemctl", "is-enabled", "unattended-upgrades"])
            if out.strip():
                s["unattended_upgrades"] = out.strip() == "enabled"
            code, out, _ = self.run(["apt-get", "-s", "-o", "Debug::NoLocking=1", "upgrade"], timeout=90)
            if code == 0:
                s["pending_security"] = len(re.findall(r"^Inst .*[Ss]ecurity", out, re.M))
            s["reboot_required"] = os.path.exists("/var/run/reboot-required")
            try:
                with open("/etc/ufw/ufw.conf") as f:
                    m = re.search(r"^ENABLED=(\w+)", f.read(), re.M)
                if m:
                    s["firewall"] = "active" if m.group(1) == "yes" else "inactive"
            except OSError:
                pass
        app = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
        ts = shutil.which("tailscale") or (app if os.path.exists(app) else None)
        if ts:
            code, out, _ = self.run([ts, "status", "--json"])
            try:
                s["tailscale"] = code == 0 and json.loads(out or "{}").get("BackendState") == "Running"
            except ValueError:
                s["tailscale"] = False
        try:
            s["vault"] = self.vault() if self.vault else None
        except Exception:
            s["vault"] = None
        return s

    def access(self):
        """Whether the machine's own SSH server (or Tailscale SSH) and Screen Sharing are on. No root needed."""
        def listening(port):
            s = socket.socket()
            s.settimeout(1)
            try:
                s.connect(("127.0.0.1", port))
                return True
            except OSError:
                return False
            finally:
                s.close()
        if self.platform == "darwin":
            return {"ssh": listening(22), "screen_sharing": listening(5900)}
        ssh = None
        for unit in ("ssh", "sshd"):
            code, out, _ = self.run(["systemctl", "is-active", unit])
            if out.strip():
                ssh = ssh or out.strip() == "active"
        ts = shutil.which("tailscale")
        if ts and not ssh:
            code, out, _ = self.run([ts, "debug", "prefs"])
            try:
                ssh = bool(json.loads(out).get("RunSSH")) if code == 0 else ssh
            except ValueError:
                pass
        return {"ssh": ssh, "screen_sharing": None}

    def exe(self, name):
        """This runtime's executable.

        A reading made for ANOTHER OS user has to be given `which`, because this process cannot find that user's
        executable itself: its PATH is not that user's, and that user's home is its own at mode 700, so a look in
        its `~/.local/bin` is refused. Both ways of getting it wrong were live on 19 October 2026 — on one machine
        the service found nothing and read `signed_in: None`, on another it found its OWN codex and ran it as the
        agent, which answered for neither of them. The agent was signed in on both.
        """
        return self.which(name) if self.which else exe(name, self.home)

    def runtime(self, name=None):
        """{name, version, signed_in} of the agent's runtime, or of another one: status commands only, no model."""
        name = name or self.conf.get("runtime")
        result = {"name": name, "version": None, "signed_in": None}
        exe = self.exe(name)
        if not exe:
            return result
        code, out, _ = self.run([exe, "--version"])
        if code == 0 and out.strip():
            result["version"] = out.strip().splitlines()[0][:120]
        if name == "codex":
            code, out, err = self.run([exe, "login", "status"])
            text = out + err
            if text.strip():
                result["signed_in"] = "Logged in" in text and "Not logged in" not in text
        elif name == "claude":
            code, out, _ = self.run([exe, "auth", "status"])
            try:
                result["signed_in"] = bool(json.loads(out).get("loggedIn"))
            except ValueError:
                pass
        return result

    def collect(self):
        with self.lock:
            update = {"state": self.state, "detail": self.detail}
        # owner feature runtime_switch: both runtimes that an owner can switch between, with the same status commands
        runtimes = {name: self.runtime(name) for name in SWITCH_RUNTIMES}
        self.remember_signed_in(runtimes)
        name = self.conf.get("runtime")
        return {"revision": self.running(), "os": self.os_info(), "security": self.security(),
                "runtime": runtimes[name] if name in runtimes else self.runtime(),
                "runtimes": {n: {"version": r["version"], "signed_in": r["signed_in"]} for n, r in runtimes.items()},
                "access": dict(self.access(), user=pwd.getpwuid(os.getuid()).pw_name),
                "update": update, "features": list(FEATURES),
                "legacy_windows": self.legacy() if self.legacy else None,
                **(self.switch() if self.switch else {"other_runtime_windows": [], "owner_chat_bound": None}),
                "settings": self.settings(), "models": self.read_models(),
                "screen_settings": self.screen_settings(), "screen_models": self.read_screen_models()}

    def remember_signed_in(self, runtimes):
        """Leave this sign-in reading in ~/.fleet, where a later `fleet.py verify` of this machine reads it.

        Only a Mac needs it, and this is the reason: there the runtime keeps its credential in the login Keychain,
        which opens for the login session alone. This process is a LaunchAgent of that session and reads it; the
        same reading taken over the owner's SSH connection, where the app runs the check, reads `loggedIn: false`
        on a machine that is signed in - as the same user, and as root through `launchctl asuser` too (both proved
        on the owner's Mac Studio, 19 October 2026; an SSH session's keychain search list holds the System
        keychain and not the user's login one). So the check answers from the reading made here, and the app's two
        screens cannot disagree about one machine. A failure to write is no failure of the status: the next report
        writes it again.
        """
        if self.platform != "darwin":
            return
        path = signed_in_path(self.home, self.agent)
        said = {"at": self.clock(), "signed_in": {name: r.get("signed_in") for name, r in runtimes.items()}}
        try:
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            tmp = path + ".new"
            with open(tmp, "w") as f:
                json.dump(said, f)
            os.replace(tmp, path)       # whole or not at all: verify reads this file while the helper writes it
        except OSError:
            pass

    # ---- agent setup (owner API 1.9)
    def settings(self):
        """What this process loaded: new sessions get this model, effort and these capabilities. Its runtime is the
        name in the status's runtime (owner feature runtime_switch)."""
        return {"model": self.conf.get("model"), "effort": self.conf.get("effort"), "can": list(self.conf.get("can") or [])}

    def screen_settings(self):
        """What this process loaded for screen tasks (owner API 1.11): {model, effort}, or None without a screen
        runtime."""
        if not self.conf.get("screen_command"):
            return None
        screen = self.conf.get("screen") or {}
        return {"model": screen.get("model"), "effort": screen.get("effort")}

    def read_screen_models(self, force=False):
        """The screen runtime's own model options (its Codex account), as read_models: model/list only, no thread and
        no turn. None without a screen runtime."""
        command = self.conf.get("screen_command")
        if not command:
            return None
        if not force and self.screen_models and self.clock() - self.screen_models["read_at"] < MODELS_EVERY:
            return self.screen_models
        options, error = self.codex_models(command) if os.path.isfile(command) else \
            (None, "the screen runtime is not installed")
        self.screen_models = {"runtime": "codex", "ok": options is not None, "read_at": self.clock(),
                              "source": "codex app-server model/list", "other_names": False,
                              "error": None if options is not None else error, "options": options or []}
        return self.screen_models

    def read_models(self, force=False):
        """The runtime's own model options for this machine and account, read at most every six hours, and again
        for a machine check. No model runs for it."""
        if not force and self.models and self.clock() - self.models["read_at"] < MODELS_EVERY:
            return self.models
        name, options, error, source = self.conf.get("runtime"), None, None, None
        exe = self.exe(name)
        if name == "codex":
            source = "codex app-server model/list"
            options, error = self.codex_models(exe) if exe else (None, "codex is not installed")
        elif name == "claude":
            source = "claude --help"
            code, out, _ = self.run([exe, "--help"]) if exe else (1, "", "")
            options, error = claude_model_list(out) if code == 0 else (None, "claude is not installed" if not exe
                                                                        else "claude --help did not answer")
        else:
            error = "This runtime reports no model list."
        self.models = {"runtime": name, "ok": options is not None, "read_at": self.clock(), "source": source,
                       "other_names": name == "claude", "error": None if options is not None else error,
                       "options": options or []}
        return self.models

    def verify(self, status, probe_job=None):
        """The setup check: what this machine can prove now, with no AI. Each item is ok, failed or not_verified;
        the hub adds its own items and decides which ones are required."""
        items = []

        def add(kind, ref, state, detail):
            items.append({"kind": kind, "ref": ref, "state": state, "detail": detail[:300]})
        rt = status.get("runtime") or {}
        name = rt.get("name") or "unknown"
        if not rt.get("version"):
            add("runtime", name, "failed", f"{name} is not installed.")
        elif rt.get("signed_in") is True:
            add("runtime", name, "ok", f"{rt['version']}, signed in")
        elif rt.get("signed_in") is False:
            add("runtime", name, "failed", f"{rt['version']}, not signed in")
        else:
            add("runtime", name, "not_verified", f"{rt['version']}; the sign-in could not be read")
        add(*self.model_item(status.get("models")))
        user = pwd.getpwuid(os.getuid()).pw_name
        add("capability", "shell", "ok", f"Commands run as {user}.")
        add("capability", "web", "not_verified", "Web access is a runtime tool; Fleet cannot check it without a model turn.")
        if self.platform == "darwin":
            session = self.session_item(user)
            ready = session[2] == "ok"
            chrome = any(os.path.isdir(p) for p in ("/Applications/Google Chrome.app",
                                                     os.path.join(self.home, "Applications", "Google Chrome.app")))
            add("capability", "browser", "ok" if chrome and ready else "failed",
                "Google Chrome is installed, and the session is ready." if chrome and ready else
                "Google Chrome is not installed." if not chrome else
                f"The browser needs {user} at the console with the screen unlocked.")
            add("capability", "screen", "not_verified" if ready else "failed",
                "The session is ready, but Fleet cannot check the macOS screen recording and accessibility "
                "permissions that screen control needs." if ready else
                f"Screen control needs {user} at the console with the screen unlocked.")
            add(*self.autologin_item(user))
            add(*self.awake_item())
            add(*session)
        else:
            add("capability", "browser", "failed", "A Linux worker has no desktop session for a browser.")
            add("capability", "screen", "failed", "A Linux worker has no desktop session.")
        items += self.login_items(probe_job)
        code, out, _ = self.run(["id", "-Gn"])
        add("access", "os_user", "ok", f"{user} (uid {os.getuid()}), groups: " +
            (", ".join(out.split()) if code == 0 and out.strip() else "unknown"))
        add("access", "files", "ok", f"The agent can read and change every file that {user} can, including this Fleet "
                                     f"checkout.")
        add(*self.admin_item())
        return items

    def model_item(self, reading):
        model, effort = self.conf.get("model"), self.conf.get("effort")
        ref = model or "default"
        if not reading or not reading.get("ok"):
            return ("model", ref, "failed" if model or effort else "not_verified",
                    (reading or {}).get("error") or "There is no model list.")
        options = {o["model"]: o for o in reading["options"]}
        if model and model not in options:
            if reading.get("other_names") and re.fullmatch(r"claude-[a-z0-9.-]+", model):
                return ("model", ref, "not_verified", "Claude takes full model names that it does not list; the first "
                                                      "session shows the model that it uses.")
            return ("model", ref, "failed", f"{model} is not offered here.")
        o = options.get(model) if model else next((x for x in reading["options"] if x["default"]), None)
        efforts = o["efforts"] if o else sorted({e for x in reading["options"] for e in x["efforts"]})
        if effort and effort not in efforts:
            return ("model", ref, "failed", f"{effort} is not an effort of {model or 'the default model'} here.")
        return ("model", ref, "ok", ("Offered" if model else "The default model") +
                (" with the efforts " + ", ".join(efforts) + "." if efforts else "."))

    def session_item(self, user):
        """macOS: who is at the console, and whether its screen is locked. A heartbeat cannot show this."""
        code, out, _ = self.run(["ioreg", "-n", "Root", "-d1", "-a"])
        try:
            root = plistlib.loads(out.encode()) if code == 0 else None
        except Exception:
            root = None
        if not isinstance(root, dict):
            return ("mac", "session", "not_verified", "The console session could not be read.")
        console = next((u.get("kCGSSessionUserNameKey") for u in root.get("IOConsoleUsers") or []
                        if isinstance(u, dict) and u.get("kCGSSessionOnConsoleKey")), None)
        locked = root.get("IOConsoleLocked")
        if not console:
            return ("mac", "session", "failed", "No one is logged in at the console.")
        if console != user:
            return ("mac", "session", "failed", f"{console} is at the console, not {user}.")
        if locked is True:
            return ("mac", "session", "failed", f"{user} is at the console, and the screen is locked.")
        if locked is False:
            return ("mac", "session", "ok", f"{user} is at the console, and the screen is unlocked.")
        return ("mac", "session", "not_verified", f"{user} is at the console; its lock state could not be read.")

    def autologin_item(self, user):
        code, out, _ = self.run(["defaults", "read", "/Library/Preferences/com.apple.loginwindow", "autoLoginUser"])
        auto = out.strip() if code == 0 else ""
        if not auto:
            return ("mac", "automatic_login", "failed", "Automatic login is off.")
        if auto != user:
            return ("mac", "automatic_login", "failed", f"Automatic login is on for {auto}, not {user}.")
        return ("mac", "automatic_login", "ok", f"Automatic login is on for {user}.")

    def awake_item(self):
        code, out, _ = self.run(["pmset", "-g"])
        if code != 0:
            return ("mac", "awake", "not_verified", "The power settings could not be read.")

        def minutes(key):
            m = re.search(r"^\s*" + key + r"\s+(\d+)", out, re.M)
            value = int(m.group(1)) if m else None
            return "unknown" if value is None else "never" if value == 0 else f"after {value} minutes"
        restart = re.search(r"^\s*autorestart\s+(\d+)", out, re.M)
        system = minutes("sleep")
        words = (f"System sleep {system}; display sleep {minutes('displaysleep')}; restart after a power failure "
                 f"{'unknown' if not restart else 'on' if restart.group(1) == '1' else 'off'}.")
        return ("mac", "awake", "ok" if system == "never" else "not_verified" if system == "unknown" else "failed", words)

    def admin_item(self):
        sudo = shutil.which("sudo")
        if not sudo:
            return ("access", "admin", "not_verified", "sudo is not installed.")
        code, out, _ = self.run([sudo, "-n", "-l"])
        if code != 0:
            return ("access", "admin", "ok", "No administrator command runs without a password.")
        if re.search(r"\(ALL(?:\s*:\s*ALL)?\)\s*NOPASSWD:\s*ALL\b", out):
            return ("access", "admin", "ok", "Unrestricted administrator access without a password (sudo -n -l).")
        if "NOPASSWD" in out:
            return ("access", "admin", "ok", "Some administrator commands run without a password (sudo -n -l).")
        return ("access", "admin", "ok", "Administrator commands need a password.")

    def login_items(self, job):
        """The vault's fixed answer for each login of the profile. With no answer, the hub shows them as not probed."""
        if not job or not self.probe:
            return []
        try:
            answer = self.probe(job)
        except Exception:
            return []
        results = answer.get("results") if isinstance(answer, dict) and answer.get("ok") is True else None
        return [{"kind": "login", "ref": r["login"], "state": "ok" if r["state"] == "readable" else "failed",
                 "detail": r["reason"]} for r in (results if isinstance(results, list) else [])[:32]
                if isinstance(r, dict) and isinstance(r.get("login"), str) and r["login"].startswith("bws://")
                and r.get("state") in ("readable", "not_readable") and r.get("reason") in LOGIN_REASONS]

    def report(self, check=None, probe_job=None):
        """Send the status. The status thread and the helper's loop both report; one at a time, each from its own
        reading, so that an older reading never arrives after a newer one and stands at the hub until the next."""
        with self.report_lock:
            self.last_report = self.clock()
            try:
                if check:
                    self.read_models(force=True)    # a machine check reads the model options again
                    self.read_screen_models(force=True)
                status = self.collect()
                if check:
                    status["check"] = check         # this report answers the owner's "check this machine"
                    status["verify"] = self.verify(status, probe_job)
                code, data = self.post(status)
            except Exception:
                return False
        if code == 200 and isinstance(data, dict) and data.get("update_requested"):
            self.arm(data.get("desired_revision"))
        return code == 200

    # ---- updates
    def set(self, state, detail):
        with self.lock:
            self.state, self.detail = state, detail

    def arm(self, desired):
        """An update that the hub requests. Every helper takes it the same way, the manager's included: its checkout,
        its home and its user service are its own, so the fast-forward and the restart need no root (hub's
        `request_update` says why that was not always so)."""
        if desired is not None and not (isinstance(desired, str) and re.fullmatch(r"[0-9a-f]{7,40}", desired)):
            return
        with self.lock:
            if self.state == "updating":
                return
            self.pending = desired
            self.state, self.detail = "waiting_idle", "Waits until no task session is open."

    def check(self, extra):
        """The owner's "check this machine": collect and report now. The same checks as every report, no change."""
        check, job = (extra or {}).get("check"), (extra or {}).get("probe_job")
        if isinstance(check, int) and not isinstance(check, bool):
            self.report(check=check, probe_job=job if isinstance(job, int) and not isinstance(job, bool) else None)

    def request(self, extra):
        """An update event from the hub: arm it and say so at once."""
        self.arm((extra or {}).get("desired_revision"))
        self.report()

    @contextlib.contextmanager
    def session_start(self):
        """The helper opens every task and chat session inside this. It never waits: during an update it
        raises Updating, the event stays pending, and the restarted helper starts the session."""
        if not self.gate.acquire(blocking=False):
            raise Updating()
        try:
            yield
        finally:
            self.gate.release()

    def fail(self, detail):
        with self.lock:
            self.pending = None
        self.set("failed", detail)
        self.report()
        return False

    def upstream(self):
        """The remote configuration ref that this checkout follows: the branch it is on, at origin."""
        return "origin/" + (checkout_branch(self.kit, self.run) or CONFIG_BRANCH)

    def has(self, desired):
        """True when this checkout contains the requested commit and origin's configuration ref has the checkout's
        own commit too. The checkout can be past the requested commit: the hub machine's checkout is also the hub's,
        which can commit again before this helper takes its update (the hub pushes each of its commits first). A
        commit that only this checkout has is one that the hub cannot know, and its update would never close."""
        return self.run(["git", "-C", self.kit, "merge-base", "--is-ancestor", desired, "HEAD"], timeout=30)[0] == 0 and \
            self.run(["git", "-C", self.kit, "merge-base", "--is-ancestor", "HEAD", self.upstream()], timeout=30)[0] == 0

    def apply(self, desired):
        """Runs with the gate held. True when the restart was asked for: this process ends soon."""
        self.set("updating", "Updating Fleet while idle.")
        self.report()
        target = desired or self.upstream()
        # A checkout that has the requested commit already needs no fetch or merge: on the hub machine the checkout is
        # also the hub's, whose own git work a fetch here could disturb.
        if not (desired and self.has(desired)):
            for argv in (["git", "-C", self.kit, "fetch", "-q", "origin"],
                         ["git", "-C", self.kit, "merge", "-q", "--ff-only", target]):
                code, out, err = self.run(argv, timeout=120)
                if code != 0:
                    return self.fail("The checkout could not %s: %s" % (argv[3], (err or out).strip()[-300:]))
        if desired and not (self.revision() or "").startswith(desired) and not self.has(desired):
            return self.fail("The checkout is not at the requested revision.")
        with self.lock:
            self.pending = None
        if self.restart():
            return True                 # the new helper process reports the new revision
        return self.fail("Updated the checkout; restart the helper service to load it.")

    def restart_service(self):
        if self.platform == "darwin":
            argv = ["launchctl", "kickstart", "-k", "gui/%d/com.agentfleet.%s" % (os.getuid(), self.agent)]
        else:
            argv = ["systemctl", "--user", "--no-block", "restart", "fleet-%s.service" % self.agent]
        return self.run(argv, timeout=30)[0] == 0

    def tick(self):
        if self.restarting is not None and self.clock() - self.restarting >= RESTART_GRACE:
            self.restarting = None
            self.fail("Updated the checkout, but the helper did not restart. Restart its service to load it.")
            self.gate.release()
        if self.clock() - self.last_report >= REPORT_EVERY:
            self.report()
        with self.lock:
            ready = self.state == "waiting_idle"
            desired = self.pending
        if not ready or not self.gate.acquire(blocking=False):
            return                      # nothing to do, or a session is starting: try at the next tick
        restarted = False
        try:
            if not self.busy():         # checked while no session can start
                restarted = self.apply(desired)
        except Exception as e:
            self.fail("The update stopped: %s." % type(e).__name__)
        finally:
            if restarted:
                self.restarting = self.clock()      # keep the gate until this process ends
            else:
                self.gate.release()

    def run_forever(self):
        while True:
            try:
                self.tick()
            except Exception:
                pass
            time.sleep(TICK)

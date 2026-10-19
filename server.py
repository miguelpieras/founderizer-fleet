#!/usr/bin/env python3
"""The Fleet server service (owner feature servers): one on each server that joined with a join code, as the OS user of
its agents (fleet on Linux, the Mac user on a Mac). Standard library only.

    python3 server.py join --hub URL --kind linux|mac [--ssh-port N]    once, from the join line (join.sh); the join
                                                                        code comes on standard input
    python3 server.py service                                           write and start this server's service again
    python3 server.py run                                               the service

The service sends the server's reading every five minutes, takes the server's jobs from the hub and updates its own
checkout when it has no job. Two job kinds. install: an agent. Its steps on Linux: the agent's own OS user
fleet-<agent>, then its runtime, its checkout ~fleet-<agent>/fleet-<agent>, its service and the runtime's sign-in
when that user is not signed in (Codex: a link and a code in Fleet; Claude Code: the sign-in that the hub drives for
that agent, machine_signin, which the owner finishes in the app and never in a terminal). Everything of one agent is
in that user's home at mode 700, so no other agent of the machine can read its runtime credential or its vault token. This service has no root, so the user is made by the one root command the
installers put in place (/usr/local/sbin/fleet-agent-user, hubsetup.agent_helper) and the rest is run as that user
through the one sudoers line beside it. A Mac keeps one user for its agents (PRODUCT.md P2), and so does an agent
from before the move: there every step is this service's own user, as it was. prepare: the other runtime of an agent
on this server, before the owner switches it, with the same runtime and sign-in steps, under that agent's own user;
it changes no agent, checkout or service. The service runs no AI session and reads no secret. Its key is
~/.fleet/server.key (mode 600); the hub keeps only its SHA-256, and it opens only this server's routes.
"""
import argparse
import hashlib
import json
import os
import plistlib
import pwd
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

import lifecycle
import onboard

KIT = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
FLEET = os.path.join(HOME, ".fleet")
KEY_FILE = os.path.join(FLEET, "server.key")
CONF_FILE = os.path.join(FLEET, "server.json")
LABEL, UNIT = "com.agentfleet.server", "fleet-server.service"
REPORT_EVERY = 300              # seconds between readings
POLL = 5                        # seconds between looks for a job
SIGN_IN_WAIT = 900              # seconds a sign-in may take
HELPER_WAIT = 120               # seconds for a new agent's own helper to reach the hub, before its sign-in can start
PATH_ENV = os.pathsep.join([os.path.join(HOME, ".local", "bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
                            "/usr/sbin", "/sbin"])
LABELS = {"codex": "Codex", "claude": "Claude Code"}
NAME = re.compile(r"[a-z][a-z0-9-]{1,30}")
# One agent's own OS user (hubsetup.AGENT_USER), the one root command that makes it (hubsetup.HELPER), and the PATH
# of a command run as that user: its own ~/.local/bin, then the machine's.
AGENT_USER = "fleet-%s"
AGENT_HELPER = "/usr/local/sbin/fleet-agent-user"
AGENT_PATH = os.pathsep.join(["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class StepFailed(Exception):
    def __init__(self, step, detail):
        super().__init__(detail)
        self.step = step


class Gone(Exception):
    """The hub ended the job (the owner removed the server): stop working on it."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an answer, never a second request: the server key must not go to another place."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Hub:
    def __init__(self, url, key=None):
        self.url, self.key = url.rstrip("/"), key
        self.headers = {}               # tests: the caller's test address

    def call(self, method, path, body=None, timeout=30):
        headers = dict(self.headers, **{"Accept": "application/json", "X-Fleet-Client": "fleet-server/1"})
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        request = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
        try:
            with opener.open(request, timeout=timeout) as answer:
                return answer.status, json.loads(answer.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return 0, {"error": str(e)}


def account(user):
    """(uid, home) of an OS user, or None when this machine has no such user."""
    try:
        found = pwd.getpwnam(str(user or ""))
    except (KeyError, TypeError):
        return None
    return found.pw_uid, found.pw_dir


def die(text):
    print("Fleet join stopped: " + text, file=sys.stderr)
    sys.exit(1)


def tail(text, n=200):
    return ANSI.sub("", (text or "").strip())[-n:]


def server_key(make=False):
    """This server's key, or None. make: a new key when it has none (256 random bits, mode 600)."""
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    if not make:
        return None
    os.makedirs(FLEET, mode=0o700, exist_ok=True)
    os.chmod(FLEET, 0o700)
    key, new = secrets.token_urlsafe(32), KEY_FILE + ".new"
    fd = os.open(new, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    os.replace(new, KEY_FILE)
    return key


def load_conf():
    try:
        with open(CONF_FILE, encoding="utf-8") as f:
            conf = json.load(f)
        return conf if isinstance(conf, dict) and NAME.fullmatch(str(conf.get("name"))) else None
    except (OSError, ValueError):
        return None


def save_conf(conf):
    os.makedirs(FLEET, mode=0o700, exist_ok=True)
    with open(CONF_FILE + ".new", "w", encoding="utf-8") as f:
        json.dump(conf, f, indent=2)
    os.replace(CONF_FILE + ".new", CONF_FILE)


def cmd_join(args):
    """The join: the server key, POST /join with the code from standard input, then the server service. The same
    server again (the line once more after a failed step) is a repeat for the hub."""
    code = sys.stdin.readline().strip()
    if not code:
        die("No join code. Copy the whole line from Fleet: Servers, Add server.")
    key = server_key(make=True)
    status, answer = Hub(args.hub).call("POST", "/join", {
        "code": code, "key_sha256": hashlib.sha256(key.encode()).hexdigest(), "kind": args.kind,
        "user": pwd.getpwuid(os.getuid()).pw_name, "ssh_port": args.ssh_port})
    if status != 200:
        die(answer.get("error") or f"The hub did not answer ({status}). Check the private network, then run the line "
                                   f"again.")
    name = answer.get("server")
    if not NAME.fullmatch(str(name)) or not re.fullmatch(r"http://[0-9.:\[\]a-f]+", str(answer.get("hub"))):
        die("The hub gave an answer that Fleet cannot use.")
    save_conf({"name": name, "hub": answer["hub"], "repo": answer.get("repo")})
    print(f"Fleet: this machine is the server {name}" + (" (it had joined already)" if answer.get("replayed") else ""),
          flush=True)
    install_service()
    print("Fleet: the server service runs", flush=True)


def install_service(run=subprocess.run, platform=sys.platform):
    """Write this server's service and start it again: a LaunchAgent on a Mac, a systemd user service on Linux."""
    argv = [sys.executable, os.path.join(KIT, "server.py"), "run"]
    os.makedirs(FLEET, mode=0o700, exist_ok=True)
    if platform == "darwin":
        folder = os.path.join(HOME, "Library", "LaunchAgents")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, LABEL + ".plist")
        with open(path, "wb") as f:
            plistlib.dump({"Label": LABEL, "ProgramArguments": argv, "WorkingDirectory": KIT,
                           "EnvironmentVariables": {"PATH": PATH_ENV}, "RunAtLoad": True, "KeepAlive": True,
                           "ThrottleInterval": 10, "ProcessType": "Background",
                           "StandardOutPath": os.path.join(FLEET, "server.log"),
                           "StandardErrorPath": os.path.join(FLEET, "server.err")}, f)
        domain = f"gui/{os.getuid()}"
        run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
        started = run(["launchctl", "bootstrap", domain, path], capture_output=True, text=True)
        if started.returncode != 0:
            die("The server service did not start: " + tail(started.stderr))
        return path

    def quote(x):
        return json.dumps(x.replace("%", "%%"))
    folder = os.path.join(HOME, ".config", "systemd", "user")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, UNIT)
    with open(path, "w", encoding="utf-8") as f:
        f.write("[Unit]\nDescription=Founderizer server service\nAfter=network-online.target\n[Service]\nType=simple\n"
                "WorkingDirectory=" + KIT.replace("%", "%%").replace(" ", r"\x20") + "\nEnvironment=" +
                quote("PATH=" + PATH_ENV) + "\nExecStart=" + " ".join(map(quote, argv)) + "\nRestart=always\n"
                "RestartSec=5\nKillMode=process\nTimeoutStopSec=15\n[Install]\nWantedBy=default.target\n")
    for step in (["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", UNIT],
                 ["systemctl", "--user", "restart", UNIT]):
        done = run(step, capture_output=True, text=True)
        if done.returncode != 0:
            die("The server service did not start: " + tail(done.stderr))
    return path


def cmd_service(args):
    if not load_conf():
        die("This machine has not joined Fleet. Run the join line from Fleet: Servers, Add server.")
    print("Fleet: wrote and started " + install_service())


def cmd_run(args):
    conf, key = load_conf(), server_key()
    if not conf or not key:
        print("This machine has not joined Fleet; the server service waits.", flush=True)
        while True:
            time.sleep(600)
    Service(conf, Hub(conf["hub"], key)).loop()


class Service:
    """The loop of one server's service. run(argv, cwd, timeout, env) -> (code, out, err) runs a command; tests replace
    it, popen, the clock and the sleep."""

    def __init__(self, conf, hub, kit=KIT, home=HOME, run=None, clock=time.time, sleep=time.sleep,
                 platform=sys.platform, life=None, popen=subprocess.Popen, users=account, lives=None):
        self.name, self.repo, self.hub, self.kit, self.home = conf["name"], conf.get("repo"), hub, kit, home
        self.run, self.popen = run or self.command, popen
        self.clock, self.sleep, self.platform = clock, sleep, platform
        self.users = users              # (OS user) -> (uid, home) or None: which agent user this machine has
        self.lives = lives or self.agent_lifecycle      # (OS user) -> the reading of that agent's runtime
        self.kept = {}                  # one reading for each agent user, because each makes its own git call
        self.life = life or lifecycle.Lifecycle(kit, None, {}, None, lambda: False, platform_name=platform, home=home)
        self.loaded = self.revision()
        self.desired = self.last_report = None
        self.stuck = None               # a revision that did not fast-forward: no new try for it

    @staticmethod
    def command(argv, cwd=None, timeout=120, env=None):
        try:
            done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL, env=env)
        except (OSError, subprocess.SubprocessError) as e:
            return 1, "", str(e)
        return done.returncode, done.stdout, done.stderr

    def sh(self, script, cwd=None, timeout=120):
        return self.run(["/bin/sh", "-c", script], cwd=cwd, timeout=timeout,
                        env=dict(os.environ, PATH=PATH_ENV, HOME=self.home))

    # ---- each agent under its own OS user (PRODUCT.md, "What the move to per-agent users decided")

    def agent_user(self, agent):
        """The OS user whose home holds this agent's checkout, runtime, credential and service: `fleet-<agent>`
        where that user exists. None on a Mac, which keeps one user for its agents, and None for an agent from
        before the move, whose checkout then stays where this service made it."""
        if self.platform == "darwin":
            return None
        user = AGENT_USER % agent
        return user if self.users(user) else None

    def home_of(self, user):
        return (self.users(user) or (0, f"/home/{user}"))[1] if user else self.home

    def short_of(self, agent, user=None):
        """The agent's checkout as a line the owner reads: the whole path under its own user's home, and the old
        `~/fleet-<agent>` where the agents share this service's user."""
        return os.path.join(self.home_of(user), f"fleet-{agent}") if user else f"~/fleet-{agent}"

    def as_agent(self, user, argv):
        """argv as that agent's own OS user, through the one sudoers line the installers put in place: its own HOME,
        PATH and user services. This service has no root and the agent's home is of mode 700, so this is the only
        way it reaches it. Without a user, argv as it is."""
        if not user:
            return list(argv)
        uid, home = self.users(user) or (0, f"/home/{user}")
        return ["sudo", "-n", "-u", user, "--", "env", f"HOME={home}", f"XDG_RUNTIME_DIR=/run/user/{uid}",
                f"PATH={home}/.local/bin{os.pathsep}{AGENT_PATH}"] + list(argv)

    def in_folder(self, user, folder, argv):
        """argv as that agent's own user, from that folder: sudo cannot be given a folder this service may not
        open, so the folder is entered after it has become the user."""
        if not user:
            return list(argv), folder
        return self.as_agent(user, ["sh", "-c", 'cd "$1" && shift && exec "$@"', "sh", folder] + list(argv)), None

    def agent_run(self, user):
        """A `run` for another module (lifecycle), every command of it taken as that agent's own user."""
        def run(argv, cwd=None, timeout=120, env=None):
            return self.run(self.as_agent(user, argv), cwd=cwd, timeout=timeout, env=env)
        return run

    def agent_life(self, user):
        """The reading of one agent's runtime: this service's own user when the agents share it, else that agent's
        own user, where its credential is."""
        return self.life if not user else self.lives(user)

    def agent_lifecycle(self, user):
        """One agent's reading, made in that user's own home. It is the reading lifecycle makes everywhere else, so
        this service and the agent's own helper never disagree.

        Every part of it runs as that agent, the look for the executable first of all: this service may not open a
        home of mode 700, and its own PATH is not that agent's. Without that the reading was of the wrong user or
        of nobody, and the sign-in step of an install waited out its fifteen minutes on a machine that was signed
        in."""
        if user not in self.kept:
            self.kept[user] = lifecycle.Lifecycle(self.kit, None, {}, None, lambda: False,
                                                  platform_name=self.platform, home=self.home_of(user),
                                                  run=self.agent_run(user),
                                                  which=lambda name: self.agent_exe(name, user))
        return self.kept[user]

    def user_step(self, job, agent):
        """The agent's own OS user, made by the one root command the installers put in place. This service runs as
        the machine's service user and has no root, so that command and nothing else is what it may run as root."""
        if self.platform == "darwin":
            return None
        user = AGENT_USER % agent
        self.post(job, "running", "user", f"The OS user {user} of {agent}.")
        code, out, err = self.run(["sudo", "-n", AGENT_HELPER, user], timeout=120)
        if code != 0 or not self.users(user):
            raise StepFailed("user", f"The OS user {user} of {agent} could not be made: {tail(err or out)}. The "
                                     f"server needs {AGENT_HELPER} from the Fleet installer; run the join line "
                                     f"again on {self.name}.")
        return user

    def revision(self):
        code, out, _ = self.run(["git", "-C", self.kit, "rev-parse", "HEAD"])
        return out.strip() if code == 0 and re.fullmatch(r"[0-9a-f]{40}", out.strip()) else None

    # ---- the reading
    def reading(self):
        life = self.life
        runtimes = {n: life.runtime(n) for n in lifecycle.SWITCH_RUNTIMES}
        return {"revision": self.loaded, "os": life.os_info(), "security": life.security(),
                "runtimes": {n: {"version": r["version"], "signed_in": r["signed_in"]} for n, r in runtimes.items()},
                "access": dict(life.access(), user=pwd.getpwuid(os.getuid()).pw_name)}

    def report(self):
        status, answer = self.hub.call("POST", f"/server/{self.name}/status", self.reading())
        self.last_report = self.clock()
        if status == 200 and re.fullmatch(r"[0-9a-f]{40}", str(answer.get("desired_revision"))):
            self.desired = answer["desired_revision"]
        return status

    # ---- the loop
    def loop(self, rounds=None):
        while rounds is None or rounds > 0:
            rounds = None if rounds is None else rounds - 1
            if self.last_report is None or self.clock() - self.last_report >= REPORT_EVERY:
                self.report()
            status, answer = self.hub.call("GET", f"/server/{self.name}/jobs")
            if status == 403:
                print("The hub does not know this server, or its key: the service waits. It joins again with a new "
                      "join line.", flush=True)
                self.sleep(600)
                continue
            jobs = answer.get("jobs") if status == 200 else None
            for job in jobs or []:
                self.work(job)
                self.report()
            if jobs == []:
                self.update()
            self.sleep(POLL)

    def work(self, job):
        try:
            kind = {"install": self.install, "prepare": self.prepare}.get(job.get("kind"))
            if kind is None:
                raise StepFailed(None, f"This server service does not know the job {job.get('kind')}. Update Fleet.")
            kind(job)
        except StepFailed as e:
            self.post(job, "failed", e.step, str(e)[:500], quiet=True)
        except Gone:
            pass
        except Exception as e:          # one bad job never stops the service
            print(f"job {job.get('id')}: {e!r}", file=sys.stderr, flush=True)
            self.post(job, "failed", None, f"The server service failed: {str(e)[:300]}", quiet=True)

    def post(self, job, state, step, detail, prompt=None, quiet=False):
        status, answer = self.hub.call("POST", f"/server/{self.name}/jobs/{job['id']}",
                                       {"state": state, "step": step, "detail": detail, "prompt": prompt})
        if status in (404, 409) and not quiet:
            raise Gone()
        return status

    # ---- install an agent
    def install(self, job):
        agent, runtime = job.get("agent"), job.get("runtime")
        if not NAME.fullmatch(str(agent)) or runtime not in LABELS:
            raise StepFailed(None, "The job names no agent or runtime that Fleet knows.")
        user = self.user_step(job, agent)
        exe = self.runtime_step(job, runtime, user)
        folder = self.checkout_step(job, agent, user)
        self.service_step(job, agent, runtime, folder, user)
        self.sign_in_step(job, agent, runtime, exe, user)
        self.post(job, "done", "sign_in", f"{agent} runs on {self.name}. Check it and activate it in its Settings.")

    # ---- prepare the other runtime of an agent (owner feature runtime_switch)
    def prepare(self, job):
        """The runtime that the owner switches the agent to, with the runtime and sign-in steps of an install: the
        pinned version when the server has none, for Codex the trust of the agent's checkout, and the sign-in in that
        checkout when the server is not signed in. The agent keeps its runtime until the owner switches it."""
        agent, runtime = job.get("agent"), job.get("runtime")
        if not NAME.fullmatch(str(agent)) or runtime not in LABELS:
            raise StepFailed(None, "The job names no agent or runtime that Fleet knows.")
        user = self.agent_user(agent)
        folder = os.path.join(self.home_of(user), f"fleet-{agent}")
        if not self.ours(folder, agent, user):
            raise StepFailed(None, f"{agent} has no checkout {self.short_of(agent, user)} from Fleet on "
                                   f"{self.name}.")
        exe = self.runtime_step(job, runtime, user)
        if runtime == "codex":
            self.codex_trust(folder, "runtime", user)
        self.sign_in_step(job, agent, runtime, exe, user)
        self.post(job, "done", "sign_in", f"{LABELS[runtime]} is installed and signed in on {self.name}. Now switch {agent} "
                                          f"to it.")

    def runtime_step(self, job, runtime, user=None):
        """The runtime of this agent, in the home of its own user: the credential the vendor's client writes lives
        beside it, and a home of mode 700 cannot be shared, so a machine with several agents carries one copy for
        each."""
        label = LABELS[runtime]
        self.post(job, "running", "runtime", f"Looking for {label}.")
        exe = self.agent_exe(runtime, user)
        if exe:
            return exe
        pin = onboard.RUNTIME_PINS[runtime]
        self.post(job, "running", "runtime", f"Installing {label} {pin}.")
        script = 'mkdir -p "$HOME/.local/bin" && ' + onboard.runtime_install(runtime, pin)
        code, out, err = self.agent_sh(user, script, timeout=900)
        exe = self.agent_exe(runtime, user)
        if code != 0 or not exe:
            raise StepFailed("runtime", f"{label} did not install: {tail(err or out)}")
        if runtime == "claude":         # its updates follow the stable channel, as after the SSH setup
            self.run(self.as_agent(user, [sys.executable, "-c", onboard.CLAUDE_CHANNEL, exe]), timeout=120)
        return exe

    def agent_sh(self, user, script, timeout=120):
        """One shell script as that agent's own user, from its home."""
        if not user:
            return self.sh(script, timeout=timeout)
        return self.run(self.as_agent(user, ["/bin/sh", "-c", 'cd "$HOME" && ' + script]), timeout=timeout)

    def agent_exe(self, runtime, user=None):
        """This agent's runtime binary: lifecycle.exe for this service's own user, and the same two places read as
        the agent's user, because its home cannot be read from outside."""
        if not user:
            return lifecycle.exe(runtime, self.home)
        code, out, _err = self.run(self.as_agent(user, [
            "/bin/sh", "-c", 'command -v "$1" || { [ -x "$HOME/.local/bin/$1" ] && printf %s "$HOME/.local/bin/$1"; }',
            "sh", runtime]))
        return (out.strip() or None) if code == 0 else None

    def checkout_step(self, job, agent, user=None):
        """The agent's own checkout fleet-<agent> in the home of its own OS user, on the fleet's configuration ref:
        the branch that the hub writes, so the folder never starts on the code branch and the agent reads the
        owner's team.json, its profile and the owner's knowledge from its first task session. It is cloned as that
        user, over git:// like every worker's, so this service never writes into the agent's home. A folder that
        this service did not make stays as it is and stops the job."""
        folder, short = os.path.join(self.home_of(user), f"fleet-{agent}"), self.short_of(agent, user)
        self.post(job, "running", "checkout", f"The checkout {short}.")
        if self.there(folder, user):
            if not self.ours(folder, agent, user):
                raise StepFailed("checkout", f"{short} exists, and Fleet did not make it. It stays as it is.")
        else:
            if not self.repo:
                raise StepFailed("checkout", "This server has no repository address. Run the join line again.")
            code, out, err = self.run(self.as_agent(user, ["git", "clone", "-q", self.repo, folder]), timeout=300)
            if code != 0:
                raise StepFailed("checkout", f"git could not get the Fleet repository: {tail(err or out)}")
            mark = os.path.join(folder, ".git", "fleet-server")
            code, out, err = self.run(self.as_agent(user, ["/bin/sh", "-c", 'printf "%s\\n" "$2" > "$1"', "sh",
                                                           mark, agent]))
            if code != 0:
                raise StepFailed("checkout", f"{short} could not be marked as Fleet's: {tail(err or out)}")
        # an origin from before the split stays as it was
        problem = lifecycle.follow_config_ref(folder, self.agent_run(user))
        if problem:
            raise StepFailed("checkout", f"git could not bring {short} up to date: {problem}")
        code, out, _err = self.run(self.as_agent(user, ["cat", os.path.join(folder, "team.json")]))
        try:
            listed = code == 0 and agent in (json.loads(out or "{}").get("agents") or {})
        except ValueError:
            listed = False
        if not listed:
            branch = lifecycle.checkout_branch(folder, self.agent_run(user))
            raise StepFailed("checkout", f"The repository does not list {agent} yet on "
                                         f"{branch or 'this checkout'}. Try again in a minute.")
        return folder

    def there(self, folder, user=None):
        """Whether this folder is there, read as the agent's own user: its home is of mode 700, so this service
        cannot even look inside it."""
        if not user:
            return os.path.exists(folder)
        return self.run(self.as_agent(user, ["test", "-d", folder]))[0] == 0

    def ours(self, folder, agent, user=None):
        """True for the agent's checkout that this service made: its .git/fleet-server mark names the agent, read
        as the agent's own user."""
        if not user:
            try:
                with open(os.path.join(folder, ".git", "fleet-server"), encoding="utf-8") as f:
                    return f.read().strip() == agent
            except OSError:
                return False
        code, out, _err = self.run(self.as_agent(user, ["cat", os.path.join(folder, ".git", "fleet-server")]))
        return code == 0 and out.strip() == agent

    def service_step(self, job, agent, runtime, folder, user=None):
        """The agent's service from its own checkout, as `fleet.py service` writes it, then started. On Linux both
        happen as the agent's own OS user, so the unit is in that user's own systemd manager and its session runs
        as it. A Codex agent's checkout is trusted first, as the worker installer does it; an owner's own setting
        that does not trust it stays."""
        self.post(job, "running", "service", f"The service of {agent}.")
        if runtime == "codex":
            self.codex_trust(folder, "service", user)
        argv, cwd = self.in_folder(user, folder, [sys.executable, "fleet.py", "service", agent])
        code, out, err = self.run(argv, cwd=cwd)
        if code != 0:
            raise StepFailed("service", f"The service of {agent} could not be written: {tail(err or out)}")
        if self.platform == "darwin":
            job_name = f"gui/{os.getuid()}/com.agentfleet.{agent}"
            if self.run(["launchctl", "print", job_name])[0] == 0:
                code, out, err = self.run(["launchctl", "kickstart", job_name])
            else:
                plist = os.path.join(self.home, "Library", "LaunchAgents", f"com.agentfleet.{agent}.plist")
                code, out, err = self.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", plist])
        else:
            code, out, err = self.run(self.as_agent(user, ["systemctl", "--user", "enable", "--now",
                                                           f"fleet-{agent}.service"]))
        if code != 0:
            raise StepFailed("service", f"The service of {agent} did not start: {tail(err or out)}")

    def codex_trust(self, folder, step, user=None):
        """Codex's trust of the agent's checkout, with the worker installer's function, so that a Codex session there
        does not stop at its trust question. It is that agent's own setting in its own home. An owner's own setting
        that does not trust it stays and stops the job."""
        argv, cwd = self.in_folder(user, folder, [sys.executable, "-c", "import worker_install; "
                                                  "print(worker_install.trust_codex_checkout())"])
        code, out, err = self.run(argv, cwd=cwd)
        if code != 0:
            raise StepFailed(step, f"Codex's trust of {folder} could not be set: {tail(err or out)}")
        if out.strip() == "kept":
            raise StepFailed(step, f"Codex has its own setting for {folder} that does not trust it. Fleet leaves it as "
                                   f"it is. Change it, then try again.")

    def sign_in_step(self, job, agent, runtime, exe, user=None):
        """The sign-in of this agent's own runtime. The credential is in that agent's user's home, so the sign-in
        runs as that user and signs that agent in; every agent of a machine is signed in on its own row of
        Machines in the app."""
        label = LABELS[runtime]
        self.post(job, "running", "sign_in", f"Checking the sign-in of {label}.")
        if self.agent_life(user).runtime(runtime)["signed_in"] is True:
            return
        if runtime == "codex":
            return self.codex_sign_in(job, exe, user)
        return self.claude_sign_in(job, agent, runtime, user)

    def claude_sign_in(self, job, agent, runtime, user=None):
        """Claude Code's own sign-in, driven by the hub for this agent (machine_signin), which is the one way a
        runtime is ever signed in: the agent's own helper runs `claude /login` in that agent's own home, where the
        credential belongs, the owner reads the link in the app and pastes the code back there. This service asks
        for it and waits. No link, no code and no credential passes through it, and no console is opened.

        The agent's helper was started one step earlier, so a hub that answers 503 has not heard from it yet and is
        asked again; every other refusal fails this step with the hub's own words."""
        deadline = self.clock() + HELPER_WAIT
        while True:
            status, answer = self.hub.call("POST", f"/server/{self.name}/signin", {"agent": agent, "runtime": runtime})
            if status == 200:
                break
            if status != 503 or self.clock() >= deadline:
                raise StepFailed("sign_in", f"The hub could not start the sign-in of {LABELS[runtime]} for {agent}: "
                                            f"{answer.get('error') or status}")
            self.post(job, "running", "sign_in", f"Waiting for the helper of {agent} to reach Fleet.")
            self.sleep(10)
        self.post(job, "needs_owner", "sign_in", f"Sign {agent} in to {LABELS[runtime]} in Fleet.",
                  prompt={"kind": "signin", "purpose": "claude_login", "agent": agent, "login": answer["login"]["id"]})
        self.wait_signed_in(runtime, user)

    def codex_sign_in(self, job, exe, user=None):
        """Codex's device sign-in: its link and code go to Fleet, then a wait for the sign-in. It runs as the
        agent's own user, because the credential it writes is that agent's. The output of the login stays in a
        mode-600 file of this service, which reads the link and the code and removes it afterwards."""
        folder = os.path.join(self.home, ".fleet")
        log = os.path.join(folder, "codex-login.log")
        self.agent_sh(user, onboard.STOP_CODEX_LOGIN)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            login = self.popen(self.as_agent(user, [exe, "login", "--device-auth"]), stdout=fd,
                               stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
                               env=dict(os.environ, PATH=PATH_ENV))
        finally:
            os.close(fd)
        try:
            url = code = None
            for _ in range(15):
                with open(log, encoding="utf-8", errors="replace") as f:
                    text = ANSI.sub("", f.read())
                found_url = re.search(r"https://auth\.openai\.com/\S+", text)
                found_code = re.search(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,6}\b", text)
                if found_url and found_code:
                    url, code = found_url.group(0), found_code.group(0)
                    break
                self.sleep(2)
            if not url:
                raise StepFailed("sign_in", "Codex gave no sign-in link. Try again.")
            self.post(job, "needs_owner", "sign_in", "Sign in to Codex with this code.",
                      prompt={"kind": "open_url", "purpose": "codex_login", "url": url, "code": code})
            self.wait_signed_in("codex", user)
        finally:
            if login.poll() is None:
                login.terminate()
            try:
                os.remove(log)
            except OSError:
                pass

    def wait_signed_in(self, runtime, user=None):
        life = self.agent_life(user)
        deadline = self.clock() + SIGN_IN_WAIT
        while self.clock() < deadline:
            if life.runtime(runtime)["signed_in"] is True:
                return
            self.sleep(5)
        raise StepFailed("sign_in", f"{LABELS[runtime]} did not sign in within 15 minutes. Try again in Fleet.")

    # ---- updates
    def update(self):
        """With no job: the hub's revision, when it changes a Python file or team.json, by a fast-forward of this
        checkout and a new start of this process. The revision is a commit of the fleet's configuration ref, so the
        checkout follows that ref first (lifecycle.follow_config_ref: it moves a checkout that still stands on the
        code branch, and leaves an origin from before the split as it was). A revision that does not fast-forward
        gets no second try."""
        desired = self.desired
        if not desired or desired in (self.loaded, self.stuck):
            return
        if self.run(["git", "-C", self.kit, "fetch", "-q", "origin"], timeout=120)[0] != 0:
            return                      # no network: this revision keeps its try for the next round
        code, out, _ = self.run(["git", "-C", self.kit, "diff", "--name-only", self.loaded or "HEAD", desired])
        if code != 0 or lifecycle.follow_config_ref(self.kit, self.run) or \
                self.run(["git", "-C", self.kit, "merge", "-q", "--ff-only", desired], timeout=120)[0] != 0:
            self.stuck = desired
            print(f"Fleet could not fast-forward {lifecycle.checkout_branch(self.kit, self.run) or 'this checkout'} "
                  f"to {desired[:12]}; this server keeps {str(self.loaded)[:12]}.", file=sys.stderr, flush=True)
            return
        if any(p.endswith(".py") or p == "team.json" for p in out.split()):
            self.restart()
        self.loaded = desired

    def restart(self):
        os.execv(sys.executable, [sys.executable, os.path.join(self.kit, "server.py"), "run"])


def main():
    parser = argparse.ArgumentParser(description="The Fleet server service")
    sub = parser.add_subparsers(dest="cmd", required=True)
    join = sub.add_parser("join")
    join.add_argument("--hub", required=True)
    join.add_argument("--kind", choices=("linux", "mac"), required=True)
    join.add_argument("--ssh-port", type=int)
    sub.add_parser("service")
    sub.add_parser("run")
    args = parser.parse_args()
    {"join": cmd_join, "service": cmd_service, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Is this machine of a fleet healthy? The one check that PRODUCT.md 4.1 freezes, as `fleet.py verify`.

    python3 fleet.py verify           one readable line for each check, then READY
    python3 fleet.py verify --json    the array of 4.1 and nothing else

Each check answers {"check", "state": ok|warn|fail, "says", "fix"}, with the ids of CHECKS and nothing else.
A check that does not apply to this machine is absent from the array: a worker has no hub_answers and no
owner_claimed. An agent of this machine that a check does not read is named in it and warns - one team.json
keeps inactive until the owner has signed it in and activated it, and one the hub answers for that this
checkout does not name yet - because a machine never answers for its own agents in silence. Nothing failing
means READY and exit 0, because a warn is not a failure; else the failing ids and exit 1.

One check reads a file and not the machine: `team` runs the rules of hub.validate_team over this checkout's
team.json, because a hub starts on a file that does not validate and nothing else on a machine says so.

It changes nothing, ever: every command it runs reads, and it needs no root. It runs on Linux and on macOS, and
reads the places the helper's own status reading reads (lifecycle.Lifecycle.security and .runtime), so the two
never disagree - and on a Mac, where the runtime's credential is in the login Keychain and only the login session
opens it, `runtime_signed_in` is that helper's own reading (signed_in). On Linux each agent's session runs as its
own OS user, so the three checks that read a user -
services, runtime_installed and runtime_signed_in - are taken as that agent's user, and a reading this command
cannot take is named and warns, never passes (PRODUCT.md 4.1). Three lanes read this shape: the installers end with it, the app shows it as the owner's
checklist, and the support duty runs it from a heartbeat.

The recorded pilot case checker, which held this name until 2026-10-17, is verify_case.py.
"""
import ast
import email.utils
import ipaddress
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

from lifecycle import CONFIG_BRANCH, checkout_branch, exe, signed_in_path

# PRODUCT.md 4.1: these ids and nothing else. The order is the order of the readable lines.
CHECKS = ("services", "hub_answers", "owner_claimed", "tailnet", "firewall", "clock", "disk",
          "runtime_installed", "runtime_signed_in", "selftest", "version", "team")
OK, WARN, FAIL = "ok", "warn", "fail"

# The three checks that read an agent of this machine: its service, its runtime binary and that runtime's
# credential. They are the checks that must also name the agents of this machine they do not read - the ones
# team.json keeps inactive, and the ones it does not know yet - which `one` does for all three (and_agents).
AGENT_CHECKS = ("services", "runtime_installed", "runtime_signed_in")

# selftest.py passes whole on the code branch since 18 October 2026 (COORDINATION.md rule 5): of the five standing
# failures, two were counts the tool sets had outgrown, two were a fixture that owner API 1.12 made wrong, and two
# were defects of the hub, all of them fixed. So nothing is expected to fail and every failure is this machine's own
# or the code's. The name stays, because releases.py reads it and the release check and `fleet.py verify` must not
# drift apart about what a pass is; a baseline that ever has to be recorded again goes here.
SELFTEST_BASELINE = ()

# The checks a release cannot be judged by, because no release can pass them for the owner: signing a machine in to
# its provider is the owner's own step, in the app, and a fleet whose owner has not taken it yet is not an unhealthy
# fleet — it is a new one. releases.Update reads this, so the release gate and `fleet.py verify` agree about which
# failures are the code's. Everything else a release can break stays in the gate.
OWNER_STEPS = ("runtime_signed_in",)
SELFTEST_WAIT = 300         # it waits for the hub's own timers, so about a minute on a quiet machine
# How old a helper's own sign-in reading may be and still answer for this machine: three of its reports
# (lifecycle.REPORT_EVERY), so one missed report is not an unread check. Only a Mac answers from it (signed_in).
HELPER_READING = 900
CLOCK_LIMIT = 60            # seconds: a minute, because token expiry and signatures depend on this clock
DISK_FAIL, DISK_WARN = 1.0, 5.0         # GiB free for the database and the checkouts
UFW_CONF = "/etc/ufw/ufw.conf"          # readable without root, which `ufw status` is not
TAILNET = ipaddress.ip_network("100.64.0.0/10")     # the private network every machine of a fleet is on
HUB_SERVICES = ("hub", "git")           # the hub machine's own two, beside the service of each agent on it

# The OS user of one agent on Linux, which hubsetup.AGENT_USER makes: its home holds that agent's session, its
# runtime credential and its vault token, at mode 700, so no other agent's user can open it. A Mac keeps one user
# for its agents (PRODUCT.md P2), and so does a machine from before the move: there this user does not exist and
# every reading is the running user's own, exactly as before.
AGENT_USER = "fleet-%s"
AGENT_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# One reading of a runtime binary in another user's home, which shutil.which cannot make: that user's own PATH and
# then its ~/.local/bin, the two places lifecycle.exe looks in.
RUNTIME_PATH = 'command -v "$1" || { [ -x "$HOME/.local/bin/$1" ] && printf %s "$HOME/.local/bin/$1"; }'
LINGER_FILE = "/var/lib/systemd/linger/%s"      # systemd's own record of lingering, readable without root
BLIND_FIX = "read the whole machine: run this check as root, or run it as each agent's own user"


def _run(argv, timeout=20):
    """One command that reads. It never raises: a machine that cannot run it gives a reading, not a crash."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return done.returncode, done.stdout, done.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


def _fetch(url, timeout=8):
    """GET url: (code, body, headers), with code 0 and the reason in the body when it did not answer. The hub's
    /health needs no credential, so verify carries none."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return 0, str(getattr(e, "reason", e)), {}


def _count(number, thing):
    """"1 second", "2 seconds": these lines are read by the owner in the app."""
    return f"{number:.0f} {thing}" + ("" if round(number) == 1 else "s")


def _line(text):
    """One line, because `says` and `fix` are one line each."""
    return " ".join(str(text).split())[:300]


def _said(text):
    """One line the owner reads in the app, so it starts with a capital (the owner, 19 October 2026: every message
    he reads starts with one). A line that starts with a name this machine itself uses keeps that name as it is: a
    unit (fleet-hub), a file (team.json), an address, a count or a revision. So the rule reaches the words and not
    the names, and a fix that is a command to paste keeps the command's own spelling."""
    line = _line(text)
    head = line.split(" ", 1)[0]
    word = head[:-2] if head.endswith("'s") else head      # an agent's name in the possessive is a word too
    if word[:1].islower() and word.isalpha() and not re.fullmatch(r"[0-9a-f]{7,}", word):
        return line[0].upper() + line[1:]
    return line


def _passwd(user):
    """(uid, home) of an OS user, or None when this machine has no such user. The password database is readable
    without root, which is how this check knows whose services and whose credential it must read."""
    try:
        found = pwd.getpwnam(str(user or ""))
    except (KeyError, TypeError):
        return None
    return found.pw_uid, found.pw_dir


def _me():
    """(the user running this check, its uid)."""
    try:
        return pwd.getpwuid(os.geteuid()).pw_name, os.geteuid()
    except KeyError:
        return os.environ.get("USER") or os.environ.get("LOGNAME") or "this user", os.geteuid()


class Verify:
    """The reading of one machine. Every reading of the outside goes through run, fetch, which, disk and now, so a
    test gives this class a healthy machine and a broken one without touching the machine it runs on."""

    def __init__(self, kit, team, hub="", *, agent=None, run=_run, fetch=_fetch, which=exe, usage=shutil.disk_usage,
                 now=time.time, platform=sys.platform, home=None, ufw_conf=UFW_CONF, selftest_wait=SELFTEST_WAIT,
                 me=None, passwd=_passwd, linger_file=LINGER_FILE):
        # conf, not team: the check whose id is `team` reads this same dict, and one name cannot be both
        self.kit, self.conf, self.hub = kit, team or {}, (hub or "").rstrip("/")
        self.run, self.fetch, self.which, self.usage, self.now = run, fetch, which, usage, now
        self.asking = None              # the id of the check being answered, for answer()
        self.platform, self.ufw_conf, self.selftest_wait = platform, ufw_conf, selftest_wait
        self.passwd, self.linger_file = passwd, linger_file
        self.me, self.my_uid = me if me else _me()      # the user running this check, and its uid
        self.ways = {}                  # user -> how this check runs one reading as it, or None: see as_user
        self.home = home or os.path.expanduser("~")
        self.addresses = self.conf.get("addresses") or {}
        self.agent = agent              # FLEET_AGENT: the name this machine answers to, when the session has one
        self.address = self.tailnet_address()
        self.named = self.machine_of(self.agent)            # the machine team.json gives that agent
        self.machine = self.named or self.machine_here()    # which machine of team.json this is, or None
        self.is_hub = self.machine == self.hub_machine() if self.machine else self.hub_service_here()
        here = [(name, conf) for name, conf in sorted((self.conf.get("agents") or {}).items())
                if self.machine and conf.get("machine") == self.machine]
        self.agents = [name for name, conf in here if conf.get("active", True) is not False]
        # The agents team.json places here and keeps inactive: a new agent until the owner signs it in and
        # activates it, and one the owner switched off. Their service, runtime and credential need not be there,
        # so the three checks of AGENT_CHECKS do not read them - but they name them, because a machine that holds
        # an agent must not answer for it in silence (and_agents).
        self.inactive = [name for name, conf in here if conf.get("active", True) is False]
        self.unknown = None             # the agents the hub knows and this checkout does not: read once, lazily

    # ---- which machine is this

    def tailscale(self):
        app = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
        return self.which("tailscale") or (app if os.path.exists(app) else None)

    def tailnet_address(self):
        """This machine's address on the private network, or None. team.json names every machine by it."""
        ts = self.tailscale()
        code, out, _ = self.run([ts, "ip", "-4"]) if ts else (1, "", "")
        first = (out or "").split("\n")[0].strip()
        return first if code == 0 and first else None

    def machine_of(self, agent):
        return ((self.conf.get("agents") or {}).get(agent) or {}).get("machine") or None

    def machine_here(self):
        """The machine of team.json whose address this machine answers to."""
        return next((name for name, ip in sorted(self.addresses.items()) if ip and ip == self.address), None)

    def hub_machine(self):
        hub = self.conf.get("hub")
        if isinstance(hub, dict):
            return hub.get("machine") or None
        host = (str(hub or "").split("//")[-1].split(":")[0] or "").strip()      # a hub given as a URL
        return next((name for name, ip in sorted(self.addresses.items()) if ip and ip == host), None)

    def hub_service_here(self):
        """A hub whose private network is down still answers for itself: its own service is written here. This is
        the reading the hub installer needs, because it runs verify before any agent session exists."""
        return os.path.exists(os.path.join(self.home, ".config", "systemd", "user", "fleet-hub.service"))

    # ---- which user holds which service, which runtime and which credential (PRODUCT.md 4.1)

    def agent_user(self, agent):
        """The OS user whose service, runtime and sign-in belong to this agent: `fleet-<agent>` where that user
        exists, which is every Linux machine an installer or the agent-add path made, and otherwise the user running
        this check (a Mac, and a machine from before the move)."""
        if self.platform == "darwin":
            return self.me
        user = AGENT_USER % agent
        return user if self.passwd(user) else self.me

    def as_user(self, user, argv):
        """argv as that user, or None when this check cannot become it — and then the reading is not taken, because
        this check needs no root and changes nothing. It becomes the user itself always, any user as root, and the
        agents of this machine as the service user, which one sudoers line of the installers allows. The reading
        keeps that user's own HOME and PATH, so the runtime and its credential are read where the agent has them."""
        if not user or user == self.me:
            return list(argv)
        if user not in self.ways:
            if self.my_uid == 0:
                self.ways[user] = ["runuser", "-u", user, "--"]
            else:
                code, _out, _err = self.run(["sudo", "-n", "-u", user, "--", "true"])
                self.ways[user] = ["sudo", "-n", "-u", user, "--"] if code == 0 else None
        if self.ways[user] is None:
            return None
        uid, home = self.passwd(user) or (0, f"/home/{user}")
        return self.ways[user] + ["env", f"HOME={home}", f"XDG_RUNTIME_DIR=/run/user/{uid}",
                                  f"PATH={home}/.local/bin:{AGENT_PATH}"] + list(argv)

    def service_user(self):
        """The user that holds this machine's own two services, fleet-hub and fleet-git: the user running this
        check. None when it cannot be that user — an agent's own user never holds them, and root has a systemd
        manager of its own — and then the two are reported as unread instead of as stopped."""
        if self.me in {AGENT_USER % a for a in (self.conf.get("agents") or {})}:
            return None
        if self.my_uid == 0 and not self.hub_service_here():
            return None
        return self.me

    def holders(self):
        """{the fleet service on this machine: the user that holds it, or None when this check cannot know whose it
        is}, in the order the readable lines name them: the hub's own two, then each agent's as its own user."""
        units = {name: self.service_user() for name in HUB_SERVICES
                 if self.is_hub and self.platform != "darwin"}
        units.update({name: self.agent_user(name) for name in self.agents})
        return units

    # ---- the checks, in the order of CHECKS

    def services(self):
        """The fleet's services are running and enabled: the hub's own two and one for each agent on this machine,
        each read as the user that holds it. A service this check could not read is named and warns, so a reader
        never takes it for a pass."""
        holders = self.holders()
        if not holders:
            return None
        readings = {name: (self.service(name, user) if user and self.as_user(user, ["true"]) is not None else None)
                    for name, user in holders.items()}
        blind = [self.unit(n) for n, r in readings.items() if r is None]
        seen = {n: r for n, r in readings.items() if r is not None}
        stopped = [self.unit(n) for n, (active, _) in seen.items() if active is not True]
        off = [self.unit(n) for n, (active, enabled) in seen.items() if active is True and enabled is not True]
        no_linger = [user for user in sorted({u for u in holders.values() if u}) if self.linger(user) is False]
        unread = (f"{', '.join(blind)} could not be read: "
                  + ("it belongs" if len(blind) == 1 else "they belong")
                  + " to another user of this machine") if blind else ""
        if stopped:
            says = f"{', '.join(stopped)} is not running" if len(stopped) == 1 else \
                   f"{', '.join(stopped)} are not running"
            return self.answer(FAIL, self.and_unread(says, unread), self.start_fix(stopped, holders))
        if off:
            return self.answer(WARN, self.and_unread(f"{', '.join(off)} runs but is not enabled, so it does not "
                                                     f"come back", unread), self.enable_fix(off, holders))
        if blind:
            return self.answer(WARN, unread, BLIND_FIX)
        if no_linger:
            return self.answer(WARN, f"{', '.join(self.unit(n) for n in seen)} run, but "
                                     f"{', '.join(no_linger)} {'has' if len(no_linger) == 1 else 'have'} no "
                                     f"lingering, so they stop at a reboot",
                               "let them run without a login: loginctl enable-linger --no-ask-password "
                               + " ".join(no_linger))
        return self.answer(OK, f"{', '.join(self.unit(n) for n in seen)} are active and enabled")

    def hub_answers(self):
        """The hub answers its own address: the address every machine and the app use, never loopback."""
        if not self.is_hub:
            return None
        if not self.hub:
            return self.answer(FAIL, "this checkout's team.json gives the hub no address, so no machine can reach it",
                               "run on the hub: python3 fleet.py addresses hub=$(tailscale ip -4)")
        code, body, _ = self.fetch(self.hub + "/health")
        try:
            answer = json.loads(body or "{}")
        except ValueError:
            answer = {}
        if code == 200 and answer.get("ok"):
            return self.answer(OK, f"the hub answers {self.hub}/health for "
                                   f"{', '.join(answer.get('agents') or []) or 'no agent'}")
        return self.answer(FAIL, f"the hub does not answer {self.hub}/health: {_line(body) or code}",
                           "look at: systemctl --user status fleet-hub")

    def owner_claimed(self):
        """The hub has an owner address, which only the claim writes: without it no Mac reaches this fleet."""
        if not self.is_hub:
            return None
        owner = (self.addresses.get("owner") or "").strip()
        if not owner:
            return self.answer(FAIL, "no Mac has claimed this hub: team.json has no owner address",
                               "run the installer's claim step again; it shows a code the app takes")
        if not self.private(owner):
            return self.answer(WARN, f"the owner's address {owner} is not on the private network",
                               "check that the app reaches the hub over the private network")
        return self.answer(OK, f"the owner of this hub is the Mac at {owner}")

    def tailnet(self):
        """The private network is up and this machine answers to the address team.json gives it. The hub takes a
        name only from its own machine, so a wrong address here refuses every call this machine makes."""
        if not self.tailscale():
            return self.answer(FAIL, "Tailscale is not installed, so this machine is on no private network",
                               "install it: curl -fsSL https://tailscale.com/install.sh | sh")
        state = self.backend_state()
        if state != "Running":
            return self.answer(FAIL, f"the private network is not up (Tailscale is {state or 'not answering'})",
                               "bring it up: tailscale up --accept-dns=false")
        if not self.address:
            return self.answer(FAIL, "Tailscale gives this machine no address", "look at: tailscale status")
        want = self.addresses.get(self.named) if self.named else None
        if want and want != self.address:
            return self.answer(FAIL, f"this machine answers {self.address}, but team.json gives {self.named} "
                                     f"{want}, so the hub refuses its calls",
                               f"run: python3 fleet.py addresses {self.named}={self.address}")
        if not self.machine:
            return self.answer(FAIL, f"this machine's address {self.address} is in team.json for no machine",
                               f"run: python3 fleet.py addresses <name>={self.address}")
        return self.answer(OK, f"{self.address} is up, and team.json gives it to {self.machine}")

    def firewall(self):
        """The firewall is on: only SSH and the private network reach this machine."""
        state, how = self.firewall_state()
        if state is True:
            return self.answer(OK, f"the firewall is on ({how})")
        if state is False:
            return self.answer(FAIL, f"the firewall is off ({how})",
                               "turn it on in System Settings > Network > Firewall" if self.platform == "darwin"
                               else "turn it on: sudo ufw --force enable")
        return self.answer(WARN, f"the firewall could not be read ({how})",
                           "read it by hand: " + ("socketfilterfw --getglobalstate" if self.platform == "darwin"
                                                  else "ufw status"))

    def clock(self):
        """The clock is within a minute of true: token expiry and signatures depend on it."""
        skew, source = self.hub_skew()
        if skew is not None:
            if abs(skew) <= CLOCK_LIMIT:
                return self.answer(OK, f"the clock is {_count(abs(skew), 'second')} from {source}")
            return self.answer(FAIL, f"the clock is {_count(abs(skew), 'second')} from {source}, over the minute "
                                     f"that tokens and signatures allow", self.time_fix())
        synced = self.network_time()
        if synced is True:
            return self.answer(OK, "the clock follows network time")
        if synced is False:
            return self.answer(FAIL, "this machine does not follow network time, so its clock drifts", self.time_fix())
        return self.answer(WARN, "the clock could not be checked: no hub to compare with and no network-time reading",
                           self.time_fix())

    def disk(self):
        """The disk has room for the database and the checkouts."""
        try:
            total, _used, free = self.usage(self.kit)
        except OSError as e:
            return self.answer(WARN, f"the disk of {self.kit} could not be read: {_line(e)}",
                               f"read it by hand: df -h {self.kit}")
        gib, whole = free / 2 ** 30, total / 2 ** 30
        says = f"{gib:.1f} GiB free of {whole:.0f} GiB where the database and the checkouts are"
        if gib < DISK_FAIL:
            return self.answer(FAIL, says, "free space: the database, the checkouts and a task output need room")
        if gib < DISK_WARN:
            return self.answer(WARN, says, "free space soon: under 5 GiB leaves little for outputs and updates")
        return self.answer(OK, says)

    def runtime_installed(self):
        """The runtime binary of each agent on this machine is installed, in the home of that agent's own user."""
        if not self.agents:
            return None
        found = {name: (self.runtime_of(name), *self.runtime_path(name)) for name in self.agents}
        missing = [f"{name} has no {rt}" for name, (rt, path, read) in sorted(found.items()) if read and not path]
        blind = [f"{name}'s {rt} could not be read" for name, (rt, _p, read) in sorted(found.items()) if not read]
        if missing:
            return self.answer(FAIL, self.and_unread(", ".join(missing), ", ".join(blind)),
                               "install it from the vendor's own installer: python3 hubsetup.py runtime "
                               "--runtime <claude|codex>")
        if blind:
            return self.answer(WARN, ", ".join(blind), BLIND_FIX)
        return self.answer(OK, ", ".join(f"{name} runs {rt} ({path})"
                                         for name, (rt, path, _read) in sorted(found.items())))

    def runtime_signed_in(self):
        """The runtime of each agent is signed in, read as that agent's own user, because the credential is in that
        user's home and belongs to it alone. The owner signs each agent in from the app, and only they can. On a
        Mac the credential is in the login Keychain instead, so the reading comes from the one process that can
        open it, the agent's own helper in the login session (signed_in)."""
        if not self.agents:
            return None
        readings = {}
        for name in self.agents:
            runtime = self.runtime_of(name)
            path, read = self.runtime_path(name)
            readings[name] = (runtime, self.signed_in(runtime, path, self.agent_user(name), name) if path else None,
                              read)
        out = [f"{name} is not signed in to {rt}" for name, (rt, state, read) in sorted(readings.items())
               if read and state is False]
        why, by_hand = self.unread_signin()
        unknown = [f"{name}'s {rt} {why}" for name, (rt, state, read) in sorted(readings.items())
                   if read and state is None]
        blind = [f"{name}'s {rt} sign-in could not be read: it belongs to another user of this machine"
                 for name, (rt, _s, read) in sorted(readings.items()) if not read]
        if out:
            return self.answer(FAIL, self.and_unread(", ".join(out), ", ".join(blind)),
                               "sign this machine in from the app: Machines, then Sign in")
        if unknown:
            return self.answer(WARN, self.and_unread(", ".join(unknown), ", ".join(blind)), by_hand)
        if blind:
            return self.answer(WARN, ", ".join(blind), BLIND_FIX)
        return self.answer(OK, ", ".join(f"{name} is signed in to {rt}"
                                         for name, (rt, _s, _read) in sorted(readings.items())))

    def selftest(self):
        """selftest.py passes against its recorded baseline, which is not zero failures (COORDINATION.md rule 5)."""
        path = os.path.join(self.kit, "selftest.py")
        if not os.path.exists(path):
            return self.answer(FAIL, f"this checkout has no selftest.py in {self.kit}, so it cannot check itself",
                               "fetch the checkout again")
        code, out, err = self.run([sys.executable, path], timeout=self.selftest_wait)
        summary = self.summary(out)
        if not summary:
            return self.answer(FAIL, f"the self-test did not answer in {self.selftest_wait} seconds: "
                                     f"{_line(err or out) or 'no output'}",
                               f"run it by hand: cd {self.kit} && python3 selftest.py")
        passed, total, failed = summary
        says = f"{passed} of {total} checks passed"
        new = [name for name in failed if name not in SELFTEST_BASELINE]
        if new:
            return self.answer(FAIL, f"{says}, and {len(new)} of the failures are not expected: {', '.join(new)}",
                               f"run it by hand: cd {self.kit} && python3 selftest.py -v")
        if code != 0 and not failed:
            return self.answer(FAIL, f"the self-test ended with {code}: {says}",
                               f"run it by hand: cd {self.kit} && python3 selftest.py")
        return self.answer(OK, says + (f"; the {len(failed)} failures are the recorded baseline" if failed else ""))

    def version(self):
        """The checkout's revision, so the app and the release channel can compare."""
        code, out, _ = self.run(["git", "-C", self.kit, "rev-parse", "HEAD"])
        revision = (out or "").strip()
        if code != 0 or not re.fullmatch(r"[0-9a-f]{40}", revision):
            return self.answer(FAIL, f"{self.kit} has no revision: git cannot read it",
                               "this machine needs a git checkout of the fleet: fetch it again")
        branch = checkout_branch(self.kit, self.run)
        _c, out, _e = self.run(["git", "-C", self.kit, "status", "--porcelain"])
        changed = [line for line in (out or "").split("\n") if line.strip()]
        where = f"{revision[:12]} on {branch or 'no branch'}"
        if branch != CONFIG_BRANCH:
            return self.answer(WARN, f"{where}, not on {CONFIG_BRANCH}, the ref the fleet runs",
                               f"put it on the configuration ref: sh {self.kit}/config-ref.sh {self.kit}")
        if changed:
            return self.answer(WARN, f"{where}, with {_count(len(changed), 'changed file')}, so it is not the "
                                     f"published revision", f"look at: git -C {self.kit} status")
        return self.answer(OK, where)

    def team(self):
        """The team.json of this checkout is one the hub can run: the rules of hub.validate_team, which every
        owner write of the file goes through, read here on the file the machines actually have. A hub starts on a
        file that does not validate and says so itself (PRODUCT.md 4.8), so this is the reading an installer, the
        app and the support duty get for the same file on every machine. It applies to every machine, because
        every machine of a fleet has this checkout."""
        if not self.conf:
            return self.answer(FAIL, "this checkout has no team.json, so nothing here knows the fleet",
                               "fetch the checkout again")
        try:
            import hub                 # the rules only; nothing of the hub runs, and this reads no machine
        except Exception as e:
            return self.answer(WARN, f"this checkout cannot read the rules of team.json: {_line(e)}",
                               f"look at: cd {self.kit} && python3 -c 'import hub'")
        problem = hub.team_problem(self.conf)
        if problem:
            return self.answer(FAIL, problem,
                               f"put the rule right in {os.path.join(self.kit, 'team.json')}: a hub runs this "
                               f"file as it is, and refuses every owner write of it until the rule is met")
        machines = {a.get("machine") for a in (self.conf.get("agents") or {}).values()}
        return self.answer(OK, f"this team.json is one a hub can run: "
                               f"{_count(len(self.conf.get('agents') or {}), 'agent')} on "
                               f"{_count(len(machines), 'machine')}")

    # ---- the readings each check needs

    def unit(self, name):
        return f"com.agentfleet.{name}" if self.platform == "darwin" else f"fleet-{name}"

    def service(self, name, user=None):
        """(active, enabled) of one fleet service, each True, False or None when it could not be read. On Linux the
        unit is in that user's own systemd manager, so the two readings are taken as it."""
        if self.platform == "darwin":
            code, out, _ = self.run(["launchctl", "print", f"gui/{os.getuid()}/com.agentfleet.{name}"])
            if code != 0:           # no such job: nothing is loaded, so nothing starts at login either
                return False, False
            return "state = running" in out, True
        unit = f"fleet-{name}.service"
        said = []
        for what in ("is-active", "is-enabled"):
            argv = self.as_user(user, ["systemctl", "--user", what, unit])
            if argv is None:
                return None, None
            _code, out, _err = self.run(argv)
            said.append((out or "").strip())
        return (said[0] == "active" if said[0] else None, said[1] == "enabled" if said[1] else None)

    def linger(self, user=None):
        """Whether that user's services survive a reboot. systemd's own record is a file that any user can read, so
        no root is needed for another user's; loginctl answers only for a user it still holds. macOS LaunchAgents
        need no lingering."""
        if self.platform == "darwin":
            return None
        user = user or self.me
        if os.path.exists(self.linger_file % user):
            return True
        found = self.passwd(user)
        code, out, _ = self.run(["loginctl", "show-user", str(found[0] if found else user), "-p", "Linger",
                                 "--value"])
        return out.strip() == "yes" if code == 0 and out.strip() else None

    def unknown_here(self):
        """The agents the hub answers for that this checkout's team.json does not name and that have an OS user on
        this machine: an agent added to this machine while its checkout stayed behind. That is how a worker could
        answer READY for its own agents and say nothing about the one it had just installed - the server service
        leaves its checkout alone while a job of its own is open, so until the sign-in ends the team.json this
        check reads does not know the agent. The hub's /health needs no credential, so this check still carries
        none; a hub that does not answer, a Mac and a machine from before the per-agent users say nothing, and a
        checkout that is behind catches up by itself."""
        if self.unknown is None:
            self.unknown = []
            if self.hub and self.platform != "darwin":
                code, body, _ = self.fetch(self.hub + "/health")
                try:
                    answer = json.loads(body or "{}") if code == 200 else {}
                except ValueError:
                    answer = {}
                named = set(self.conf.get("agents") or {})
                self.unknown = sorted(name for name in (answer.get("agents") or []) if isinstance(name, str)
                                      and name not in named and self.passwd(AGENT_USER % name))
        return self.unknown

    def user(self):
        return self.me or "the fleet user"

    def holder_of(self, unit, holders):
        """The user that holds one unit, by the name the readable line uses."""
        return next((user for name, user in holders.items() if self.unit(name) == unit), None)

    def as_line(self, user, line):
        """One readable command, said as the user that must run it when that is not the one running this check."""
        return line if not user or user == self.me else f"sudo -u {user} {line}"

    def start_fix(self, units, holders=None):
        if self.platform == "darwin":
            return f"start it again: launchctl kickstart gui/{os.getuid()}/{units[0]}"
        user = self.holder_of(units[0], holders or {})
        mine = [u for u in units if self.holder_of(u, holders or {}) == user]
        return "start it again: " + self.as_line(user, "systemctl --user restart " + " ".join(mine))

    def enable_fix(self, units, holders=None):
        if self.platform == "darwin":
            return (f"make it come back after a reboot: launchctl bootstrap gui/{os.getuid()} "
                    f"~/Library/LaunchAgents/{units[0]}.plist")
        user = self.holder_of(units[0], holders or {})
        mine = [u for u in units if self.holder_of(u, holders or {}) == user]
        return "make it come back after a reboot: " + self.as_line(user, "systemctl --user enable " + " ".join(mine))

    def backend_state(self):
        ts = self.tailscale()
        code, out, _ = self.run([ts, "status", "--json"]) if ts else (1, "", "")
        try:
            return json.loads(out or "{}").get("BackendState") if code == 0 else None
        except ValueError:
            return None

    def private(self, address):
        try:
            return ipaddress.ip_address(address) in TAILNET
        except ValueError:
            return False

    def firewall_state(self):
        """(on, how): the same two places the helper's own status reading reads."""
        if self.platform == "darwin":
            code, out, _ = self.run(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"])
            if code != 0 or not out.strip():
                return None, "macOS did not answer"
            return "enabled" in out.lower(), "macOS application firewall"
        try:
            with open(self.ufw_conf, encoding="utf-8") as f:
                found = re.search(r"^ENABLED=(\w+)", f.read(), re.M)
        except OSError as e:
            return None, _line(e)
        if not found:
            return None, f"{self.ufw_conf} names no ENABLED"
        return found.group(1).lower() == "yes", "ufw"

    def hub_skew(self):
        """(seconds, source): this clock against the hub's own, which is the clock that checks this machine's
        tokens. None on the hub itself, because a clock cannot check itself."""
        if not self.hub or self.is_hub:
            return None, None
        before = self.now()
        code, _body, headers = self.fetch(self.hub + "/health")
        after = self.now()
        date = (headers or {}).get("Date")
        if not code or not date:
            return None, None
        try:
            theirs = email.utils.parsedate_to_datetime(date).timestamp()
        except (TypeError, ValueError):
            return None, None
        return (before + after) / 2 - theirs, "the hub's clock"

    def time_fix(self):
        return ("turn network time on: System Settings, General, Date & Time" if self.platform == "darwin"
                else "sudo timedatectl set-ntp true")

    def network_time(self):
        code, out, _ = self.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
        return out.strip() == "yes" if code == 0 and out.strip() else None

    def runtime_of(self, agent):
        return ((self.conf.get("agents") or {}).get(agent) or {}).get("runtime") or ""

    def runtime_path(self, agent):
        """(the path of this agent's runtime binary or None, whether the reading was taken). For the user running
        this check it is lifecycle.exe, so the helper and verify look in the same two places; for another user the
        same two places read as it, because a home of mode 700 cannot be read from outside."""
        runtime, user = self.runtime_of(agent), self.agent_user(agent)
        if user == self.me:
            return self.which(runtime, self.home), True
        argv = self.as_user(user, ["sh", "-c", RUNTIME_PATH, "sh", runtime])
        if argv is None:
            return None, False
        code, out, _err = self.run(argv)
        return (((out or "").strip() or None) if code == 0 else None), True

    def login_session(self):
        """Whether this run is inside the Mac's login session, the one session that opens the login Keychain.
        `Aqua` is launchd's own name for it: an SSH session answers `Background` and a daemon `System`."""
        code, out, _ = self.run(["launchctl", "managername"])
        return (out or "").strip() == "Aqua" if code == 0 else False

    def helper_signed_in(self, agent, runtime):
        """What this machine's own helper last read about that runtime's sign-in - True or False - or None when
        there is no reading of the last HELPER_READING seconds. The helper writes it as the user whose agents
        these are, which on a Mac is the user running this check (agent_user), so the file is this user's own."""
        try:
            with open(signed_in_path(self.home, agent)) as f:
                said = json.load(f)
            at = float(said["at"])
            state = said["signed_in"][runtime]
        except (OSError, ValueError, TypeError, KeyError):
            return None
        return state if isinstance(state, bool) and self.now() - at <= HELPER_READING else None

    def unread_signin(self):
        """Why a sign-in reading was not taken here, and what takes it, as (the words after the name, the fix). On
        a Mac outside the login session the reason is the machine's and not the agent's, and the answer is no
        command the owner can run from where the app runs this check: its helper reads it in that session."""
        if self.platform == "darwin" and not self.login_session():
            return ("sign-in is in the login Keychain, which opens for the login session alone, and this machine's "
                    "helper has left no reading of it",
                    "start this machine's helper, which reads the sign-in in that session, and check again")
        return ("sign-in could not be read",
                "read it by hand on this machine: claude auth status, or codex login status")

    def signed_in(self, runtime, path, user=None, agent=None):
        """True, False or None, from the runtime's own status command: the reading lifecycle.Lifecycle.runtime
        makes, so the app and verify never disagree, run as the user whose credential it is. No model runs and
        nothing is written.

        A Mac answers from the helper's own reading of that same command, because there the credential is in the
        login Keychain and no run outside the login session can open it (lifecycle.Lifecycle.remember_signed_in
        says what was proved). In that session - an installer in a Terminal, the agent's own duty session - the
        command is run here as everywhere else. With neither, the reading is not taken: never a pass and never a
        failure (PRODUCT.md 4.1)."""
        if self.platform == "darwin":
            said = self.helper_signed_in(agent, runtime)
            if said is not None or not self.login_session():
                return said
        if runtime == "codex":
            argv = self.as_user(user, [path, "login", "status"])
            _code, out, err = self.run(argv) if argv else (1, "", "")
            text = (out or "") + (err or "")
            return "Logged in" in text and "Not logged in" not in text if text.strip() else None
        if runtime == "claude":
            argv = self.as_user(user, [path, "auth", "status"])
            _code, out, _err = self.run(argv) if argv else (1, "", "")
            try:
                return bool(json.loads(out or "").get("loggedIn"))
            except (ValueError, AttributeError):
                return None
        return None

    def summary(self, out):
        """(passed, of, the names it failed) from selftest.py's own last line, or None when it printed none."""
        for line in reversed([line for line in (out or "").split("\n") if line.strip()]):
            found = re.search(r"(\d+) of (\d+) checks passed\.(?:\s*FAILED:\s*(\[.*\]))?\s*$", line)
            if not found:
                continue
            try:
                failed = list(ast.literal_eval(found.group(3))) if found.group(3) else []
            except (ValueError, SyntaxError):
                failed = []
            return int(found.group(1)), int(found.group(2)), failed
        return None

    # ---- the array

    def and_unread(self, says, unread):
        """One line that names what this check found and, after it, what it could not read: a reader of a failing
        check must not take the rest of the machine for a pass either (PRODUCT.md 4.1)."""
        return f"{says}, and {unread}" if unread else says

    def answer(self, state, says, fix=None):
        return {"check": self.asking, "state": state, "says": _said(says), "fix": _said(fix) if fix else None}

    def unanswered(self):
        """The agents of this machine that a check which reads an agent does not answer for, each as (one line,
        the fix): the ones team.json keeps inactive, and the ones it does not name at all."""
        said = []
        if self.inactive:
            one = len(self.inactive) == 1
            said.append((f"{', '.join(self.inactive)} {'is' if one else 'are'} not active, so nothing of "
                         f"{'it' if one else 'them'} is read here",
                         "finish the agent in the app: Machines, then Sign in, and then activate it"))
        if self.unknown_here():
            one = len(self.unknown_here()) == 1
            said.append((f"the hub knows {', '.join(self.unknown_here())}, which this checkout does not name, so "
                         f"{'that agent' if one else 'those agents'} of this machine is not read here",
                         f"bring this checkout up to date: git -C {self.kit} fetch origin && git merge "
                         f"--ff-only origin/{CONFIG_BRANCH}"))
        return said

    def and_agents(self, found):
        """One answer of a check that reads an agent, with the agents of this machine it does not answer for named
        after it. It never passes while one of them is there, and never fails for one either: both are states of a
        machine that is being set up, so they warn, and a failure of what was read stays a failure. An absent
        check is not a pass (PRODUCT.md 4.1), and neither is a check that answers for a machine's other agents."""
        said = self.unanswered()
        if not said:
            return found
        note = ", and ".join(line for line, _fix in said)
        if found is None:
            return self.answer(WARN, note, said[0][1])
        return self.answer(FAIL if found["state"] == FAIL else WARN, self.and_unread(found["says"], note),
                           found["fix"] or said[0][1])

    def one(self, name):
        """One check by its id: its answer, or None when it does not apply to this machine. The three checks that
        read an agent also name the agents of this machine they do not read, in that one answer (and_agents)."""
        self.asking = name
        found = getattr(self, name)()
        return self.and_agents(found) if name in AGENT_CHECKS else found

    def all(self):
        """The array of PRODUCT.md 4.1 for this machine, in the order of CHECKS and without what does not apply."""
        return [found for found in (self.one(name) for name in CHECKS) if found is not None]


def checks(kit, team, hub="", **how):
    """The array of PRODUCT.md 4.1 for the machine this runs on."""
    return Verify(kit, team, hub, **how).all()


def failed(results):
    """The ids that failed. A warn is not a failure."""
    return [r["check"] for r in results if r["state"] == FAIL]


def report(results):
    """The readable form: one line for each check, then READY or the failing ids."""
    lines = [f"  {r['state']:<4}  {r['check']}: {r['says']}" + (f"  (fix: {r['fix']})" if r["fix"] else "")
             for r in results]
    bad = failed(results)
    return "\n".join(lines + (["READY"] if not bad else ["NOT READY: " + ", ".join(bad)]))

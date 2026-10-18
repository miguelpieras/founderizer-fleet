#!/usr/bin/env python3
"""Install the existing Fleet worker as a user service. No credentials or root service."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import urllib.parse

import fleet


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def check(agent):
    if sys.version_info < (3, 10):
        raise ValueError("Install Python 3.10 or later.")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", agent):
        raise ValueError("Use a worker name with lowercase letters, numbers and hyphens.")
    conf = fleet.TEAM["agents"].get(agent)
    if not conf or conf.get("role") != "worker":
        raise ValueError("Register this worker name, machine address and profile in team.json on the hub first, then pull the repository here.")
    if not (Path(fleet.KIT) / "profiles" / (agent + ".md")).is_file():
        raise ValueError("The worker profile is missing.")
    if sys.platform not in ("darwin", "linux"):
        raise ValueError("This installer supports macOS and Linux with systemd.")
    for command in ("git", "tmux", conf["runtime"]):
        if not shutil.which(command):
            raise ValueError(f"Install {command} and put it on PATH, then run the installer again.")
    auth = (["claude", "auth", "status", "--json"] if conf["runtime"] == "claude" else ["codex", "login", "status"])
    signed_in = subprocess.run(auth, capture_output=True, text=True, timeout=20)
    if signed_in.returncode != 0:
        print(f"Sign-in needed: {conf['runtime']} tasks cannot run until this OS user signs in.", file=sys.stderr)
    if sys.platform == "darwin" and conf.get("screen_command"):
        if not Path(conf["screen_command"]).is_file() or not shutil.which("xcrun"):
            raise ValueError("Install the ChatGPT app with Codex Computer Use and Xcode Command Line Tools for screen tasks.")
        if subprocess.run([conf["screen_command"], "login", "status"], capture_output=True, timeout=20).returncode:
            print("Sign-in needed: sign in to Codex in the ChatGPT app before screen tasks.", file=sys.stderr)
    if sys.platform == "linux" and not shutil.which("systemctl"):
        raise ValueError("This Linux installer requires systemd user services.")
    configured = fleet.TEAM.get("addresses", {}).get(conf.get("machine"))
    parsed = urllib.parse.urlsplit(fleet.HUB)
    if not configured or not parsed.hostname:
        raise ValueError("The hub and worker private-network addresses must be configured.")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect((parsed.hostname, parsed.port or 80))
        actual = route.getsockname()[0]
    if actual != configured:
        raise ValueError("This is not the registered worker machine. Connect its private network or correct team.json; no service was changed.")
    code, health = fleet.http("GET", "/health", timeout=5)
    if code != 200 or agent not in health.get("agents", []):
        raise ValueError("The hub does not know this worker. Deploy its team.json entry first.")
    # Refuse a duplicate running helper at another checkout, and never disturb active work.
    fleet.AGENT = agent                 # a helper route: with the agent key once the agent has one (owner API 1.12)
    code, status = fleet.http("GET", "/worker/install/" + agent, timeout=5)
    if code != 200:
        raise ValueError("Update the hub before using this installer.")
    if status.get("active_tasks"):
        raise ValueError("This worker has active tasks. Finish or pause them before changing its service.")
    return conf


def trust_codex_checkout():
    """Codex asks "Trust this folder?" when it first starts in a folder, and a task or chat session cannot answer.
    Record trust for exactly this checkout in this user's Codex settings: add one table, change nothing else.
    An existing entry for this folder is the owner's choice and stays as it is."""
    kit = os.path.realpath(fleet.KIT)
    config = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    if json.dumps(kit) in text or "'%s'" % kit in text:
        try:
            import tomllib
            entry = tomllib.loads(text).get("projects", {}).get(kit, {})
            return "present" if entry.get("trust_level") == "trusted" else "kept"
        except ImportError:                     # Python 3.10: no TOML reader; the entry is there
            return "present"
        except ValueError:
            raise ValueError(f"{config} is not valid TOML; correct it, then run the installer again.") from None
    added = ("\n" if text and not text.endswith("\n") else "") + \
        ("\n" if text else "") + "[projects.%s]\ntrust_level = \"trusted\"\n" % json.dumps(kit)
    try:
        import tomllib
        if tomllib.loads(text + added)["projects"][kit]["trust_level"] != "trusted":
            raise ValueError()
    except ImportError:
        pass
    except (ValueError, KeyError, TypeError):
        raise ValueError(f"{config} is not valid TOML; correct it, then run the installer again.") from None
    config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with os.fdopen(os.open(str(config), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a", encoding="utf-8") as f:
        f.write(added)
    return "added"


def capture_app():
    app = Path.home() / ".fleet/Fleet Worker.app"
    binary = app / "Contents/MacOS/FleetWorker"
    binary.parent.mkdir(parents=True, exist_ok=True)
    info = {"CFBundleIdentifier": "local.agent-fleet.worker", "CFBundleName": "Fleet Worker",
            "CFBundleExecutable": "FleetWorker", "CFBundlePackageType": "APPL", "CFBundleVersion": "1",
            "LSUIElement": True, "LSMinimumSystemVersion": "14.0",
            "NSScreenCaptureUsageDescription": "Show this worker's desktop in Fleet while you watch an active screen task."}
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    run(["xcrun", "swiftc", "-parse-as-library", "-O", str(Path(fleet.KIT) / "FleetWorker.swift"), "-o", str(binary)])
    run(["codesign", "--force", "--sign", "-", str(app)], stdout=subprocess.DEVNULL)
    run(["codesign", "--verify", "--deep", "--strict", str(app)])
    return app


def service_path(agent):
    if sys.platform == "darwin":
        return Path.home() / "Library/LaunchAgents" / f"com.agentfleet.{agent}.plist"
    return Path.home() / ".config/systemd/user" / f"fleet-{agent}.service"


def activate(agent):
    if sys.platform == "darwin":
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", domain + f"/com.agentfleet.{agent}"], capture_output=True)
        run(["launchctl", "bootstrap", domain, str(service_path(agent))])
    else:
        run(["systemctl", "--user", "restart", f"fleet-{agent}.service"])


def main():
    # Match the worker service PATH, including standalone runtime installations.
    os.environ["PATH"] = os.pathsep.join([str(Path.home() / ".local/bin"), "/opt/homebrew/bin",
                                        "/usr/local/bin", os.environ.get("PATH", "")])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--check", action="store_true", help="Check prerequisites and identity without installing")
    parser.add_argument("--start", action="store_true", help="Start or restart this worker after installation")
    parser.add_argument("--authorize-screen", action="store_true", help="Open the macOS screen permission setup")
    args = parser.parse_args()
    try:
        conf = check(args.agent)
        print("Worker identity, private hub connection and prerequisites: passed.")
        if args.check:
            return
        if sys.platform == "linux":
            linger = run(["loginctl", "show-user", str(os.getuid()), "-p", "Linger", "--value"], capture_output=True)
            if linger.stdout.strip() != "yes":
                raise ValueError("Run loginctl enable-linger for this user, then run the installer again. An administrator may be needed.")
        # Preserve the current helper path if a service already exists elsewhere.
        path = service_path(args.agent)
        if path.exists() and str(Path(fleet.KIT)) not in path.read_text(errors="replace"):
            raise ValueError("This worker already has a service for a different checkout. Use that checkout to update it.")
        if conf["runtime"] == "codex":
            print({"added": "Codex now trusts this checkout: " + os.path.realpath(fleet.KIT),
                   "present": "Codex already trusts this checkout.",
                   "kept": "Codex has its own setting for this checkout; it was not changed."}[trust_codex_checkout()])
        if sys.platform == "darwin" and conf.get("screen_command"):
            app = capture_app()
        with contextlib.redirect_stdout(io.StringIO()):
            fleet.cmd_service(argparse.Namespace(name=args.agent))
        print(f"Installed {path}")
        if args.start:
            activate(args.agent)
            print("Worker service started. Check its status in Fleet → Team.")
        if args.authorize_screen and sys.platform == "darwin" and conf.get("screen_command"):
            run(["open", str(app), "--args", "--authorize"])
        print("Runtime sign-in stays with this OS user. No credentials, sleep settings or login settings were changed.")
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print("Install stopped: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

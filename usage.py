"""Read numeric CLI usage only. No prompts, provider credentials, or inference.

The helper runs this collector on a separate thread. Provider reads are limited
to one attempt per five minutes; transcript scans have size and time bounds.
"""
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import sqlite3
import subprocess
import time

import lifecycle

POLL_SECONDS = 300
SCAN_SECONDS = 30
MAX_BYTES = 256 * 1024 * 1024
MAX_LINE = 4 * 1024 * 1024
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write")
BY_SESSION = 500                    # per-session totals in one report: the newest sessions
SESSION_ID = re.compile(r"[A-Za-z0-9._:-]{1,80}")
SCOPE = "Retained Fleet workspace sessions"
SOURCES = {"claude": "Claude Code transcripts", "codex": "Codex session records"}
LIMIT_SOURCES = {"claude": "Claude Code /usage cache", "codex": "Codex account/rateLimits/read"}
SCREEN_SOURCE = "Codex screen task events"
SCREEN_SCOPE = "Recorded Fleet screen tasks"
NO_WINDOWS = "No account limits were reported."      # the runtime named its account, but gave no allowance window
SIGN_IN_REQUIRED = "CLI sign-in is required."        # the runtime itself said that it has no signed-in account
IDENTITY_CHANGED = "The signed-in account changed."  # the runtime named an account that this kept reading is not known to be
# A kept reading with one of these is history only: it is not the allowance of the runtime's account now.
IDENTITY_ERRORS = (SIGN_IN_REQUIRED, IDENTITY_CHANGED)
ERRORS = {"Token records could not be read.", "Account limits could not be refreshed.",
          SIGN_IN_REQUIRED, IDENTITY_CHANGED, "Account limit data is not current.", NO_WINDOWS}
EMAIL = re.compile(r"[^\s@\x00-\x1f\x7f-\x9f]+@[^\s@\x00-\x1f\x7f-\x9f]+")


class SignedOut(ValueError):
    """The runtime itself says that it has no signed-in account. This is no failed refresh: a reading that is kept is
    then no longer the runtime's identity."""


class Unrefreshed(ValueError):
    """The limits could not be read, but the runtime itself named its account before that: email is that account's
    email, "" for an account without one (for example an API key)."""

    def __init__(self, email):
        super().__init__("limits not refreshed")
        self.email = email


def number(value, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 2**53 - 1:
        raise ValueError("invalid number")
    if integer and not isinstance(value, int):
        raise ValueError("invalid count")
    return value


def timestamp(value):
    if isinstance(value, str):
        try:
            return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return number(value) if value is not None else None


def read_json(path, limit=4 * 1024 * 1024):
    with open(path, "rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("file too large")
    return json.loads(raw)


def account_key(provider, identity):
    return provider + ":" + hashlib.sha256((provider + ":" + identity).encode()).hexdigest()[:32] if identity else None


def transcript_tokens(runtime, kit, home=None):
    home, kit = Path(home or Path.home()), str(Path(kit).resolve())
    if runtime == "claude":
        root = home / ".claude/projects" / re.sub(r"[^A-Za-z0-9-]", "-", kit)
        paths = root.glob("**/*.jsonl")
    else:
        root = home / ".codex/sessions"
        paths = root.glob("**/*.jsonl")
    deadline, size, messages, sessions = time.monotonic() + 10, 0, {}, set()
    owner, recent = {}, {}            # message key -> session; session -> newest record time
    since, last = None, None
    for count, path in enumerate(paths):
        if count >= 5000 or time.monotonic() > deadline:
            raise ValueError("scan limit")
        if path.is_symlink():
            continue
        size += path.stat().st_size
        if size > MAX_BYTES:
            raise ValueError("scan limit")
        sid, root, included = None, None, runtime == "claude"
        with path.open("rb") as stream:
            while True:
                line = stream.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE or time.monotonic() > deadline:
                    raise ValueError("scan limit")
                if not line.endswith(b"\n"):
                    break  # a writer can still be finishing the last record
                try:
                    row = json.loads(line)
                except ValueError:
                    raise ValueError("invalid transcript") from None
                kind = row.get("type")
                if runtime == "codex" and kind == "session_meta":
                    meta = row.get("payload") or {}
                    included = meta.get("cwd") == kit
                    sid = meta.get("id")
                    root = meta.get("session_id") or sid    # a subagent's rollout names its root session
                    if not included:
                        break
                if not included:
                    continue
                if runtime == "claude":
                    if kind != "assistant" or row.get("cwd") != kit:
                        continue
                    message = row.get("message") or {}
                    usage = message.get("usage")
                    key, sid = message.get("id"), row.get("sessionId")
                    root = sid                              # subagent transcripts keep the root sessionId
                    if not usage or not key or not sid:
                        continue
                    values = [number(usage.get(k, 0), True) for k in
                              ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
                else:
                    payload = row.get("payload") or {}
                    if kind != "event_msg" or payload.get("type") != "token_count" or not sid:
                        continue
                    usage = (payload.get("info") or {}).get("total_token_usage")
                    if not usage:
                        continue
                    raw, out, read, write = [number(usage.get(k, 0), True) for k in
                                            ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens")]
                    values, key = [number(raw - read - write, True), out, read, write], sid
                # Claude can emit several blocks for one API message. Codex
                # records cumulative totals repeatedly. Neither is additive.
                previous = messages.get(key, [0, 0, 0, 0])
                messages[key] = [max(a, b) for a, b in zip(previous, values)]
                sessions.add(sid)
                owner[key] = root                       # per-session totals roll subagents up to their root
                ts = timestamp(row.get("timestamp"))
                if ts is not None:
                    since = min(since, ts) if since is not None else ts
                    last = max(last, ts) if last is not None else ts
                    recent[root] = max(recent.get(root) or ts, ts)
    if not messages:
        return None
    totals = dict(zip(TOKEN_KEYS, map(sum, zip(*messages.values()))))
    per = {}
    for key, values in messages.items():
        per[owner[key]] = per.get(owner[key], 0) + sum(values)
    newest = sorted(per, key=lambda sid: recent.get(sid) or 0, reverse=True)[:BY_SESSION]
    return dict(totals, total=sum(totals.values()), sessions=len(sessions), since=since,
                last_activity=last, collected_at=time.time(), source=SOURCES[runtime], scope=SCOPE, error=None,
                by_session=[{"session_id": sid, "total": per[sid], "last_activity": recent.get(sid)} for sid in newest])


def run_json(args, cwd, timeout=25, ok=(0,)):
    # Anonymous temporary files bound RAM even if a CLI emits unexpected output.
    # No output or provider error is printed or persisted by the collector.
    import tempfile
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(args, cwd=cwd, stdout=output, stderr=subprocess.DEVNULL,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            process.wait(timeout=timeout)
        finally:
            if process.poll() is None:
                import signal
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        if process.returncode not in ok or output.tell() > MAX_LINE:
            raise ValueError("CLI read failed")
        output.seek(0)
        return json.load(output)


def codex_limits(kit, command="codex"):
    process = subprocess.Popen([command, "app-server", "--listen", "stdio://"], cwd=kit,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               bufsize=0, start_new_session=True)
    reader = selectors.DefaultSelector()
    reader.register(process.stdout, selectors.EVENT_READ)
    deadline, pending = time.monotonic() + 20, b""

    def send(value):
        process.stdin.write((json.dumps(value) + "\n").encode())

    def response(identifier):
        nonlocal pending
        while time.monotonic() < deadline:
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                data = json.loads(line)
                if data.get("id") == identifier:
                    if "result" not in data:
                        raise ValueError("account read failed")
                    return data["result"]
            if not reader.select(0.2):
                continue
            data = os.read(process.stdout.fileno(), 65536)
            if not data or len(pending) + len(data) > MAX_LINE:
                raise ValueError("account read failed")
            pending += data
        raise ValueError("account read timed out")

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "fleet_usage", "version": "1.0"}}})
        response(1)
        send({"method": "initialized"})
        send({"id": 2, "method": "account/read", "params": {"refreshToken": False}})
        answer = response(2)
        if "account" in answer and answer["account"] is None:
            raise SignedOut("no account")        # the runtime's own answer, not a failed read
        account = answer.get("account") or {}
        try:
            send({"id": 3, "method": "account/rateLimits/read"})
            result = response(3)
            return normalize_codex_limits(result, account, time.time())
        except (OSError, ValueError, KeyError, TypeError):
            if isinstance(answer.get("account"), dict):         # the account is known; only its limits are not
                raise Unrefreshed(str(account.get("email") or "")) from None
            raise
    finally:
        import signal
        reader.close()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        process.stdin.close()
        process.stdout.close()


def safe_label(value, default):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9 ._()/:-]{1,70}", value) else default


def safe_email(value):
    """The account email that the signed-in runtime gave, or None (unknown): at most 254 characters, one @, and no
    whitespace or control character. It is for the owner only, and it never decides an account group."""
    return value if isinstance(value, str) and len(value) <= 254 and EMAIL.fullmatch(value) else None


def normalize_codex_limits(result, account, collected_at):
    windows = []
    buckets = result.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        single = result.get("rateLimits") or {}
        buckets = {single.get("limitId") or "codex": single}
    for key, bucket in buckets.items():
        for field in ("primary", "secondary"):
            window = bucket.get(field)
            if not window:
                continue
            minutes = window.get("windowDurationMins")
            period = "5 hours" if minutes == 300 else "7 days" if minutes == 10080 else f"{minutes} minutes" if minutes else field
            label = safe_label(bucket.get("limitName") or key, "Codex") + " / " + period
            windows.append(dict(id=safe_label(str(key) + ":" + field, field),
                                label=safe_label(label, period),
                                used_percent=number(window["usedPercent"]), resets_at=timestamp(window.get("resetsAt")),
                                window_minutes=number(minutes, True) if minutes is not None else None))
    identity = result.get("accountId") or account.get("email")
    if not windows and not identity:
        raise ValueError("no account windows")
    # An account without windows still gives its identity; its state stays unknown at the hub.
    return dict(collected_at=collected_at, source=LIMIT_SOURCES["codex"],
                plan=safe_label(account.get("planType"), "ChatGPT"), account_key=account_key("openai", identity),
                account_email=safe_email(account.get("email")), windows=windows, error=None if windows else NO_WINDOWS)


def claude_cache(home=None, signed_in=None):
    """The account record of this home and its cached usage windows. An account record and a cache can be old: they
    stay after a sign-out. So without signed_in, only a cache of this account with windows counts, with its own time
    and no email. signed_in is the email that the runtime's own sign-in status just gave ("" for none): then the
    account also counts without windows, and its email is given when it is the email of this account record."""
    home = Path(home or Path.home())
    data = read_json(home / ".claude.json")
    account = data.get("oauthAccount") or {}
    cache = data.get("cachedUsageUtilization") or {}
    identity = account.get("accountUuid")
    if not identity:
        raise ValueError("no account record")
    if cache.get("accountUuid") != identity:
        cache = {}                              # another account's cache gives no window of this account
    utilization = cache.get("utilization") or {}
    windows = []
    # Current Claude versions expose named/scoped windows here. Use the old
    # five_hour/seven_day fields only when this array is absent.
    if isinstance(utilization.get("limits"), list):
        for index, window in enumerate(utilization["limits"]):
            group = window.get("group")
            scope = window.get("scope") or {}
            model = (scope.get("model") or {}).get("display_name")
            label = "5 hours" if group == "session" else "7 days" if group == "weekly" else "Account limit"
            if model:
                label += " (" + safe_label(model, "model") + ")"
            windows.append(dict(id=safe_label(str(window.get("kind")) + ":" + str(index), str(index)),
                                label=label, used_percent=number(window["percent"]),
                                resets_at=timestamp(window.get("resets_at")),
                                window_minutes=300 if group == "session" else 10080 if group == "weekly" else None))
    else:
        for field, minutes, label in (("five_hour", 300, "5 hours"), ("seven_day", 10080, "7 days")):
            window = utilization.get(field)
            if window:
                windows.append(dict(id=field, label=label, used_percent=number(window["utilization"]),
                                    resets_at=timestamp(window.get("resets_at")), window_minutes=minutes))
    if not windows and signed_in is None:
        raise ValueError("no account windows")
    plan = {"default_claude_max_5x": "Claude Max 5x", "default_claude_max_20x": "Claude Max 20x"}.get(account.get("userRateLimitTier"))
    if not plan:
        plan = {"claude_team": "Claude Team", "claude_pro": "Claude Pro"}.get(account.get("organizationType"), "Claude")
    own = isinstance(signed_in, str) and signed_in.lower() == str(account.get("emailAddress") or "").lower()
    return dict(collected_at=number(cache["fetchedAtMs"]) / 1000 if windows else time.time(),
                source=LIMIT_SOURCES["claude"], plan=plan,
                account_key=account_key("anthropic", identity + ":" + str(account.get("organizationUuid") or "")),
                account_email=safe_email(signed_in) if own else None, windows=windows,
                error=None if windows else NO_WINDOWS)


def keep_windows(old, new):
    """A reading without windows names the account, but it never replaces the windows that an earlier reading gave
    for the same account: those stay, with their own time, as after a failed refresh."""
    if new["windows"] or not (old and old.get("windows") and old.get("account_key") == new["account_key"]):
        return new
    return dict(old, account_email=new["account_email"], error="Account limits could not be refreshed.")


def claude_signed_in(kit):
    """The runtime's own sign-in status (no model turn; it exits with 1 when signed out). Signed in: its email, "" for
    none. Signed out: SignedOut. A status that cannot be read: None, which proves neither."""
    try:
        status = run_json(["claude", "auth", "status", "--json"], kit, ok=(0, 1))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not isinstance(status, dict):
        return None
    if status.get("loggedIn") is False:
        raise SignedOut("signed out")
    return str(status.get("email") or "") if status.get("loggedIn") is True else None


def refresh_failed(reading, failure):
    """A refresh failed, so the reading is kept, with its windows and their own time. An ordinary failure keeps its
    email too. When the runtime itself just named another account, or one without an email, the reading is the earlier
    account's: it names no email, and its error says that the account changed, so the hub takes no allowance from it
    for this route. A reading that already says signed out or changed stays so until a reading succeeds; a runtime
    that is signed in again, but whose limits cannot be read, is not known to be the earlier account."""
    named = failure.email if isinstance(failure, Unrefreshed) else None
    if reading.get("error") in IDENTITY_ERRORS:
        if named is not None:
            reading["error"] = IDENTITY_CHANGED
    elif named is not None and reading.get("account_email") and named.lower() != reading["account_email"].lower():
        reading.update(account_email=None, error=IDENTITY_CHANGED)
    else:
        reading["error"] = "Account limits could not be refreshed."


def signed_out(reading):
    """After SignedOut: a reading that is kept keeps its windows and their time, but it names no account email."""
    if reading:
        reading.update(account_email=None, error=SIGN_IN_REQUIRED)


def claude_limits(kit, home=None):
    signed_in = claude_signed_in(kit)           # first: a signed-out runtime is not a failed refresh
    try:
        return claude_usage(kit, home, signed_in)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        if signed_in is not None:               # the account is known; only its limits are not
            raise Unrefreshed(signed_in) from None
        raise


def claude_usage(kit, home, signed_in):
    result = run_json(["claude", "-p", "/usage", "--output-format", "json", "--max-turns", "1",
                       "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                       "--no-session-persistence", "--permission-mode", "dontAsk"], kit)
    if result.get("num_turns") != 0 or result.get("is_error"):
        raise ValueError("local command failed")
    data = claude_cache(home, signed_in)
    if time.time() - data["collected_at"] > POLL_SECONDS:
        data["error"] = "Account limit data is not current."
    return data


def validate_usage(data, runtime, screen=False):
    """Rebuild an allowlisted payload at the hub boundary; reject all extras."""
    if not isinstance(data, dict) or not {"tokens", "limits"} <= set(data) or set(data) - {"tokens", "limits", "screen", "previous"}:
        raise ValueError("invalid usage")
    for kind, value in data.items():
        if kind == "screen":
            if screen or not isinstance(value, dict) or set(value) != {"tokens", "limits"}:
                raise ValueError("invalid screen usage")
            validate_usage(value, "codex", screen=True)
            continue
        if kind == "previous":          # owner feature runtime_switch: the runtime before a switch, tokens only
            if screen or not isinstance(value, dict) or set(value) != {"runtime", "tokens"} or \
                    value["runtime"] not in SOURCES or value["runtime"] == runtime:
                raise ValueError("invalid previous usage")
            validate_usage({"tokens": value["tokens"], "limits": None}, value["runtime"])
            continue
        if value is None:
            continue
        fields = (set(TOKEN_KEYS) | {"total", "sessions", "since", "last_activity", "collected_at", "source", "scope", "error"}
                  if kind == "tokens" else {"collected_at", "source", "plan", "account_key", "windows", "error"})
        optional = {"by_session"} if kind == "tokens" else {"account_email"}     # an older helper sends no account email
        if not isinstance(value, dict) or set(value) - optional != fields:
            raise ValueError("invalid fields")
        if "by_session" in value:
            rows = value["by_session"]
            if not isinstance(rows, list) or len(rows) > BY_SESSION:
                raise ValueError("invalid sessions")
            for row in rows:
                if not isinstance(row, dict) or set(row) != {"session_id", "total", "last_activity"} or \
                        not isinstance(row["session_id"], str) or not SESSION_ID.fullmatch(row["session_id"]):
                    raise ValueError("invalid session")
                number(row["total"], True)
                if row["last_activity"] is not None:
                    number(row["last_activity"])
        if value["error"] is not None and value["error"] not in ERRORS:
            raise ValueError("invalid error")
        if value["collected_at"] is not None and number(value["collected_at"]) > time.time() + 60:
            raise ValueError("invalid time")
        source = SCREEN_SOURCE if screen and kind == "tokens" else (SOURCES if kind == "tokens" else LIMIT_SOURCES)[runtime]
        if value["source"] != source:
            raise ValueError("invalid source")
        if kind == "tokens":
            for key in (*TOKEN_KEYS, "total", "sessions"):
                number(value[key], True)
            if value["total"] != sum(value[key] for key in TOKEN_KEYS) or value["scope"] != (SCREEN_SCOPE if screen else SCOPE) or value["collected_at"] is None:
                raise ValueError("invalid totals")
            for key in ("since", "last_activity"):
                if value[key] is not None:
                    number(value[key])
        else:
            if value["plan"] is not None and safe_label(value["plan"], None) is None:
                raise ValueError("invalid plan")
            if value["account_key"] is not None and not re.fullmatch(r"(?:anthropic|openai):[a-f0-9]{32}", value["account_key"]):
                raise ValueError("invalid account key")
            if value.get("account_email") is not None and safe_email(value["account_email"]) is None:
                raise ValueError("invalid account email")
            if not isinstance(value["windows"], list) or len(value["windows"]) > 24:
                raise ValueError("invalid windows")
            seen = set()
            for window in value["windows"]:
                if set(window) != {"id", "label", "used_percent", "resets_at", "window_minutes"}:
                    raise ValueError("invalid window")
                if not all(safe_label(window[k], None) for k in ("id", "label")):
                    raise ValueError("invalid label")
                if window["id"] in seen:
                    raise ValueError("duplicate window")
                seen.add(window["id"])
                number(window["used_percent"])
                if window["resets_at"] is not None:
                    number(window["resets_at"])
                if window["window_minutes"] is not None:
                    number(window["window_minutes"], True)
    return data


def screen_database(home=None):
    path = Path(home or Path.home()) / ".fleet/screen-usage.sqlite3"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        path.touch(mode=0o600)
    db = sqlite3.connect(path, timeout=5)
    db.execute("CREATE TABLE IF NOT EXISTS sessions(kit TEXT, agent TEXT, thread TEXT, raw_input INTEGER, output INTEGER, cache_read INTEGER, since REAL, updated REAL, cache_write INTEGER DEFAULT 0, PRIMARY KEY(kit,agent,thread))")
    return db


def record_screen_usage(kit, agent, thread, token_usage, home=None):
    """Store cumulative counters, never prompts. Repeated events cannot add tokens twice."""
    if not isinstance(thread, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", thread):
        raise ValueError("invalid thread")
    total = token_usage["total"]
    raw, out, cache = [number(total[k], True) for k in ("inputTokens", "outputTokens", "cachedInputTokens")]
    write = number(total.get("cacheWriteInputTokens", 0), True)
    if cache + write > raw or number(total["totalTokens"], True) != raw + out:
        raise ValueError("invalid token totals")
    ts = time.time()
    with screen_database(home) as db:
        db.execute("""INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(kit,agent,thread)
            DO UPDATE SET raw_input=max(raw_input,excluded.raw_input), output=max(output,excluded.output),
            cache_read=max(cache_read,excluded.cache_read), cache_write=max(cache_write,excluded.cache_write), updated=excluded.updated""",
            (str(Path(kit).resolve()), agent, thread, raw, out, cache, ts, ts, write))
    db.close()


def screen_tokens(kit, agent, home=None):
    with screen_database(home) as db:
        row = db.execute("SELECT sum(raw_input-cache_read-cache_write),sum(output),sum(cache_read),count(*),min(since),max(updated),sum(cache_write) FROM sessions WHERE kit=? AND agent=?",
                         (str(Path(kit).resolve()), agent)).fetchone()
    db.close()
    if not row[3]:
        return None
    return dict(input=row[0], output=row[1], cache_read=row[2], cache_write=row[6],
                total=sum(row[:3])+row[6], sessions=row[3], since=row[4], last_activity=row[5],
                collected_at=time.time(), source=SCREEN_SOURCE, scope=SCREEN_SCOPE, error=None)


def local_model(conf):
    """A worker that runs a model on the machine itself, by the flag its runtime has for that (lifecycle.RUNTIMES
    "local_model"): its tokens count, but no vendor account limit applies."""
    flag = lifecycle.runtime_fact(conf.get("runtime"), "local_model")
    return bool(flag) and bool(re.search(rf"(?:^|\s){re.escape(flag)}(?:\s|$)", str(conf.get("start") or "")))


class Collector:
    def __init__(self, kit, agent, runtime, publish, home=None, screen_command=None, local=False, previous=None):
        self.kit, self.agent, self.runtime, self.publish = kit, agent, runtime, publish
        self.screen_command, self.local = screen_command, local
        # owner feature runtime_switch: the runtime that this agent ran before its switch. Its retained workspace
        # sessions are read again every POLL_SECONDS, so that the last records of its closing chat count; its account
        # limits are never read again.
        self.previous = previous if previous in SOURCES and previous != runtime else None
        self.previous_poll = 0
        self.home = Path(home or Path.home())
        self.path = self.home / ".fleet" / ("usage-" + agent + ".json")
        self.data, self.last_poll = {"tokens": None, "limits": None}, 0
        try:
            cache = read_json(self.path)
            if cache["runtime"] != runtime:     # another runtime's numbers and poll time never stand for this one
                raise ValueError("another runtime")
            self.data = validate_usage(cache["data"], runtime)
            self.last_poll = number(cache["last_poll"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if local:
            self.data["limits"] = None          # a cached account window would belong to a vendor login, not this model
        if not self.previous or (self.data.get("previous") or {}).get("runtime") != self.previous:
            self.data.pop("previous", None)
        if screen_command:
            self.data.setdefault("screen", {"tokens": None, "limits": None})

    def collect(self):
        if self.screen_command:
            try:
                self.data["screen"]["tokens"] = screen_tokens(self.kit, self.agent, self.home)
            except (OSError, ValueError, sqlite3.Error):
                if self.data["screen"]["tokens"]:
                    self.data["screen"]["tokens"]["error"] = "Token records could not be read."
        if self.runtime not in SOURCES:         # Grok documents no usage: unknown, never another runtime's numbers
            self.publish(self.data)
            return
        if self.previous and time.time() - self.previous_poll >= POLL_SECONDS:
            self.previous_poll = time.time()
            try:
                self.data["previous"] = {"runtime": self.previous,
                                         "tokens": transcript_tokens(self.previous, self.kit, self.home)}
            except (OSError, ValueError, KeyError, TypeError):
                if (self.data.get("previous") or {}).get("tokens"):
                    self.data["previous"]["tokens"]["error"] = "Token records could not be read."
        try:
            self.data["tokens"] = transcript_tokens(self.runtime, self.kit, self.home)
        except (OSError, ValueError, KeyError, TypeError):
            if self.data["tokens"]:
                self.data["tokens"]["error"] = "Token records could not be read."
        if not self.local and time.time() - self.last_poll >= POLL_SECONDS:
            self.last_poll = time.time()
            if self.screen_command:
                try:
                    self.data["screen"]["limits"] = keep_windows(self.data["screen"]["limits"],
                                                                 codex_limits(self.kit, self.screen_command))
                except SignedOut:
                    signed_out(self.data["screen"]["limits"])
                except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as failure:
                    if self.data["screen"]["limits"]:
                        refresh_failed(self.data["screen"]["limits"], failure)
            try:
                self.data["limits"] = keep_windows(self.data["limits"], codex_limits(self.kit) if self.runtime == "codex"
                                                   else claude_limits(self.kit, self.home))
            except SignedOut:               # confirmed by the runtime: no old cache stands in, and no email stays
                signed_out(self.data["limits"])
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as failure:
                if self.data["limits"] is None and self.runtime == "claude":
                    try:
                        self.data["limits"] = claude_cache(self.home)
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
                if self.data["limits"]:
                    refresh_failed(self.data["limits"], failure)
        validate_usage(self.data, self.runtime)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Numbers, labels, the account key and the account email only. A failed write cannot change task handling.
        with open(self.path, "w", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            json.dump({"runtime": self.runtime, "data": self.data, "last_poll": self.last_poll}, stream)
        self.publish(self.data)

    def run(self):
        while True:
            try:
                self.collect()
            except Exception:
                pass  # Never print provider data, transcript text, or raw errors.
            time.sleep(SCAN_SECONDS)

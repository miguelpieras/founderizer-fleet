#!/usr/bin/env python3
"""Fleet place helper: the one program that a host runs for the fleet, as the forced command of one SSH key.

A key that no agent may see often has to end up on a server: in an application's .env file, or in a Kubernetes
Secret. An agent asks the hub with place_secret; the vault of the machine that the owner chose for that target reads
the key from its own store and connects here with its own SSH key, which this host's authorized_keys file allows only
this command. It sends one JSON line on standard input: the place, the variable's name, the value and a salt. This
program writes the value where the place says and answers one JSON line: what was there before (new, same or
replaced), where it wrote, and a salted fingerprint of the value as it reads it back. No answer holds the value, and
no argument or environment variable of a program that it starts does.

The places are the owner's, written in the forced command and never in a request:

    --place NAME=env:/srv/app/shared/.env     one variable of a dotenv file (systemd EnvironmentFile, docker compose,
                                               dotenv). The file must be there; its owner, group, mode and single link
                                               stay as they are, and it is replaced whole, never left half written.
    --place NAME=k8s:NAMESPACE/SECRET         one key of a Kubernetes Secret, through the cluster's API with
                                               --k8s-api https://HOST:6443 --k8s-ca FILE --k8s-token FILE
                                               [--k8s-server-name NAME]: a token that may get, create and patch only
                                               the Secrets that it needs, which stays on this host.
    --names NAME=VAR,VAR                       optional: the only variable names (or keys) that place takes.
    --then NAME=COMMAND                        optional: a command that runs after that place changed, for example a
                                               restart. It never gets the value. One that failed runs again with the
                                               next request, also when the value is then the same.
    --state FOLDER                             where a --then that has not run yet is remembered (~/.fleet-place).

One line in authorized_keys for each machine whose vault writes here, for example:

    command="/usr/bin/python3 /usr/local/lib/fleet-place/place.py --place app=env:/srv/app/shared/.env",restrict ssh-ed25519 AAAA... fleet-vault@vps

Run it as the owner of the file it writes (or as root): a file written by another user would change its owner, so
that is refused.

The request: {"v": 1, "op": "place" or "check", "place": NAME, "name": VAR, "value": "..." (place only), "salt": 32 hex}
A check with no name says only whether the place can be written.
The answer:  {"ok": true, "v": 1, "place", "sink", "where", "name", "was": new|same|replaced (place),
              "present": true|false (check with a name), "writable" (check),
              "fingerprint": {"hash": sha256(salt + value), "len"} or null, "then": {"code"} when it ran}
             {"ok": false, "v": 1, "error": "...", "unknown": true when the write had begun: the place can hold
              the new value, and placing it again says what it holds}

Standard library only; Python 3.8+.
"""
import base64
import fcntl
import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import stat
import subprocess
import sys
import urllib.parse

VERSION = 1
PLACE_NAME = re.compile(r"[a-z][a-z0-9-]{0,39}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
K8S_KEY = re.compile(r"[-._a-zA-Z0-9]{1,253}")
K8S_SECRET = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?")
K8S_NAMESPACE = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
SALT = re.compile(r"[0-9a-f]{32}")
RAW = re.compile(r"[A-Za-z0-9_./:@%+=,~-]+")      # a value that every reader of an env file reads as it stands
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
REQUEST_MAX = 65536             # bytes of one request line
VALUE_MAX = 32768               # characters of one value
FILE_MAX = 1 << 20              # bytes of an env file
THEN_LIMIT = 300                # seconds that a --then command gets
API_LIMIT = 30                  # seconds of one call to the cluster's API
ANSWER_MAX = 4 << 20            # bytes of one answer of the cluster's API
PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class Refused(Exception):
    """What the vault is told instead of an outcome. Its text never holds the value."""


class Unsure(Refused):
    """A refusal after the write began: the place can hold the new value. Placing it again says what it holds."""


class Setup(Exception):
    """The forced command is not written right: the owner's line, not the request."""


# ---------------------------------------------------------------- the owner's places, from the forced command

def setup(argv):
    """The places of this host, from the arguments of the forced command."""
    conf = {"places": {}, "names": {}, "then": {}, "k8s": {}, "state": None}
    flags = {"--place": [], "--names": [], "--then": []}
    single = {"--k8s-api": None, "--k8s-ca": None, "--k8s-token": None, "--k8s-server-name": None, "--state": None}
    args = list(argv)
    while args:
        flag = args.pop(0)
        value = None
        if "=" in flag and flag.split("=", 1)[0] in single:
            flag, value = flag.split("=", 1)
        if flag not in flags and flag not in single:
            raise Setup(f"unknown argument {flag[:40]}")
        if value is None:
            if not args:
                raise Setup(f"{flag} needs a value")
            value = args.pop(0)
        if flag in flags:
            flags[flag].append(value)
        elif single[flag] is not None:
            raise Setup(f"{flag} is given twice")
        else:
            single[flag] = value
    for spec in flags["--place"]:
        name, _, rest = spec.partition("=")
        kind, _, where = rest.partition(":")
        if not PLACE_NAME.fullmatch(name) or name in conf["places"]:
            raise Setup(f"a place needs a name of a-z 0-9 - that starts with a letter, once: {name[:40]!r}")
        if kind == "env":
            if not os.path.isabs(where) or CONTROL.search(where) or where.endswith("/"):
                raise Setup(f"place {name}: env needs the absolute path of the file")
            conf["places"][name] = {"sink": "env", "where": os.path.normpath(where)}
        elif kind == "k8s":
            namespace, _, secret = where.partition("/")
            if not (K8S_NAMESPACE.fullmatch(namespace) and K8S_SECRET.fullmatch(secret)):
                raise Setup(f"place {name}: k8s needs NAMESPACE/SECRET")
            conf["places"][name] = {"sink": "k8s", "where": f"{namespace}/{secret}", "namespace": namespace,
                                    "secret": secret}
        else:
            raise Setup(f"place {name}: the kind is env or k8s")
    if not conf["places"]:
        raise Setup("no --place")
    for spec in flags["--names"]:
        name, _, names = spec.partition("=")
        place = conf["places"].get(name)
        if place is None or name in conf["names"]:
            raise Setup(f"--names {name[:40]!r} names no place, or names it twice")
        allowed = [n for n in names.split(",") if n]
        rule = ENV_NAME if place["sink"] == "env" else K8S_KEY
        if not allowed or not all(rule.fullmatch(n) for n in allowed):
            raise Setup(f"--names {name}: a list of names that {place['sink']} takes, with commas")
        conf["names"][name] = set(allowed)
    for spec in flags["--then"]:
        name, _, command = spec.partition("=")
        if name not in conf["places"] or name in conf["then"] or not command.strip() or CONTROL.search(command):
            raise Setup(f"--then {name[:40]!r} names no place, names it twice, or has no command on one line")
        conf["then"][name] = command
    k8s = any(p["sink"] == "k8s" for p in conf["places"].values())
    api, ca, token = single["--k8s-api"], single["--k8s-ca"], single["--k8s-token"]
    if k8s:
        parts = urllib.parse.urlsplit(api or "")
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.query or parts.fragment:
            raise Setup("a k8s place needs --k8s-api, the https address of the cluster's API")
        if not (ca and os.path.isabs(ca) and token and os.path.isabs(token)):
            raise Setup("a k8s place needs --k8s-ca and --k8s-token, absolute paths on this host")
        name = single["--k8s-server-name"]
        if name is not None and not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", name):
            raise Setup("--k8s-server-name is a host name")
        conf["k8s"] = {"api": api.rstrip("/"), "ca": ca, "token": token, "server_name": name}
    elif any(single[k] is not None for k in ("--k8s-api", "--k8s-ca", "--k8s-token", "--k8s-server-name")):
        raise Setup("--k8s-* settings are for a k8s place, and there is none")
    state = single["--state"] or os.path.join(os.path.expanduser("~"), ".fleet-place")
    if not os.path.isabs(state):
        raise Setup("--state is an absolute path")
    conf["state"] = state
    return conf


# ---------------------------------------------------------------- the request

def request(conf, line):
    """The checked request: (op, place name, place, variable name or None, value or None, salt)."""
    try:
        req = json.loads(line)
    except ValueError:
        raise Refused("send one JSON request line") from None
    if not isinstance(req, dict) or req.get("v") != VERSION:
        raise Refused("unsupported protocol version")
    op, name, var, salt = req.get("op"), req.get("place"), req.get("name"), req.get("salt")
    if op not in ("place", "check"):
        raise Refused("op is place or check")
    if set(req) - {"v", "op", "place", "name", "value", "salt"}:
        raise Refused("the request has a field this helper does not take")
    place = conf["places"].get(name) if isinstance(name, str) else None
    if place is None:
        raise Refused(f"this host has no place {str(name)[:40]!r} for this key")
    if not (isinstance(salt, str) and SALT.fullmatch(salt)):
        raise Refused("the request has no salt")
    if var is None and op == "check":
        if "value" in req:
            raise Refused("a check takes no value")
        return op, name, place, None, None, salt
    rule = ENV_NAME if place["sink"] == "env" else K8S_KEY
    if not (isinstance(var, str) and rule.fullmatch(var)):
        raise Refused("the variable's name is not one that " + ("an env file" if place["sink"] == "env" else
                                                               "a Kubernetes Secret") + " takes")
    allowed = conf["names"].get(name)
    if allowed is not None and var not in allowed:
        raise Refused(f"place {name} does not take {var}")
    value = req.get("value")
    if op == "check":
        if "value" in req:
            raise Refused("a check takes no value")
        return op, name, place, var, None, salt
    if not (isinstance(value, str) and 0 < len(value) <= VALUE_MAX) or "\x00" in value:
        raise Refused(f"the value must be 1 to {VALUE_MAX} characters")
    return op, name, place, var, value, salt


def fingerprint(salt, value):
    """The salted fingerprint of a value, as the vault and the hub make it: no part of the value."""
    return {"hash": hashlib.sha256((salt + value).encode()).hexdigest(), "len": len(value)}


# ---------------------------------------------------------------- an env file

def assignment(var):
    """A line that sets this variable: NAME=, with an export in front or spaces around it."""
    return re.compile(r"[ \t]*(export[ \t]+)?" + re.escape(var) + r"[ \t]*=")


def read_value(line, match):
    """The value of one assignment line as the readers of an env file read it, in the forms that they share."""
    rest = line[match.end():].rstrip("\r\n").strip()
    if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in "'\"":
        inner = rest[1:-1]
        return re.sub(r'\\(["\\$`])', r"\1", inner) if rest[0] == '"' else inner
    return re.split(r"[ \t]+#", rest, maxsplit=1)[0].rstrip()


def written(var, value, export):
    """The line that sets var to value: as it stands when every reader reads it so, else in single quotes."""
    if RAW.fullmatch(value):
        return f"{export}{var}={value}"
    if "'" not in value and not CONTROL.search(value):
        return f"{export}{var}='{value}'"
    raise Refused("this value holds a quote or a control character beside characters that need quotes, so an env file "
                  "would not read it back the same way; nothing was written")


def opened(path, folder_fd=None):
    """(bytes, stat) of the env file: a regular file with one link, read without following a link."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        raise Refused(f"{path} is not there; the owner makes it first") from None
    except OSError:
        raise Refused(f"{path} cannot be opened: it is a link, or this helper's user may not read it") from None
    with os.fdopen(fd, "rb") as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise Refused(f"{path} is not a file")
        if info.st_nlink != 1:
            raise Refused(f"{path} has more than one link, so a new file would not be the one its readers read")
        data = f.read(FILE_MAX + 1)
    if len(data) > FILE_MAX:
        raise Refused(f"{path} is larger than {FILE_MAX} bytes")
    try:
        return data.decode("utf-8"), info
    except UnicodeDecodeError:
        raise Refused(f"{path} is not UTF-8 text") from None


def current(lines, var):
    """The values that the file's lines give var, in order."""
    pattern, out = assignment(var), []
    for line in lines:
        m = pattern.match(line)
        if m:
            out.append(read_value(line, m))
    return out


def replace_file(path, info, data):
    """Put a complete new file in the place of the old one, with its owner, group and mode: never half written."""
    folder, base = os.path.split(path)
    temp = os.path.join(folder, f".{base}.fleet-place-{os.getpid()}")
    if os.geteuid() not in (0, info.st_uid):
        raise Refused(f"this helper runs as another user than the owner of {path}, and a new file would change its "
                      f"owner; run it as that user or as root. Nothing was written")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
    except OSError:
        raise Refused(f"no new file can be made beside {path}; nothing was written") from None
    try:
        try:
            made = os.fstat(fd)
            if (made.st_uid, made.st_gid) != (info.st_uid, info.st_gid):
                os.fchown(fd, info.st_uid if os.geteuid() == 0 else -1, info.st_gid)
            os.fchmod(fd, stat.S_IMODE(info.st_mode))
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, path)
    except OSError:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise Refused(f"{path} could not be replaced with its owner, group and mode; nothing was written") from None
    try:
        dfd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass                    # the new file is in its place: this is no failure of the write


def env_place(conf, name, place, var, value, salt):
    """Write var=value into the env file, or find it there already, and read it back."""
    path = place["where"]
    try:
        folder = os.open(os.path.dirname(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        raise Refused(f"the folder of {path} cannot be opened") from None
    try:
        fcntl.flock(folder, fcntl.LOCK_EX)          # one writer of this folder's env files at a time
        text, info = opened(path)
        lines = text.splitlines(keepends=True)
        if var is None:                             # a check of the place alone
            return {"writable": os.access(path, os.W_OK) and os.access(os.path.dirname(path), os.W_OK | os.X_OK)}
        found = current(lines, var)
        if value is None:                           # a check of one variable
            return {"present": bool(found), "fingerprint": fingerprint(salt, found[-1]) if found else None}
        if found and all(v == value for v in found):
            return {"was": "same", "fingerprint": fingerprint(salt, value)}
        pattern = assignment(var)
        hits = [i for i, line in enumerate(lines) if pattern.match(line)]
        ending = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
        if hits:
            first = hits[0]
            line = lines[first]
            export = "export " if pattern.match(line).group(1) else ""
            lines[first] = written(var, value, export) + (line[len(line.rstrip("\r\n")):] or ending)
            for i in reversed(hits[1:]):        # one line sets it: a reader that takes the first and one that takes
                del lines[i]                    # the last read the same value
        else:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += ending
            lines.append(written(var, value, "") + ending)
        pending(conf, name)
        conf["_started"] = True                     # an error from here on can come after the new file is in place
        replace_file(path, info, "".join(lines).encode("utf-8"))
        try:
            again, _ = opened(path)
        except Refused as e:
            raise Unsure(f"{e}, after the new file was put in its place") from None
        back = current(again.splitlines(keepends=True), var)
        if back != [value]:
            raise Unsure(f"{path} does not read back the value that was written")
        return {"was": "replaced" if hits else "new", "fingerprint": fingerprint(salt, back[-1])}
    finally:
        os.close(folder)


# ---------------------------------------------------------------- a Kubernetes Secret

class Pinned(http.client.HTTPSConnection):
    """A connection to the cluster's API that checks its certificate against the pinned authority, by the name the
    certificate carries (--k8s-server-name) when that is not the address."""

    def __init__(self, host, port, context, server_name, timeout):
        super().__init__(host, port, context=context, timeout=timeout)
        self.pinned, self.server_name = context, server_name

    def connect(self):
        sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self.pinned.wrap_socket(sock, server_hostname=self.server_name or self.host)


def token(conf):
    """The cluster token, read from its file on this host for one request."""
    try:
        with open(conf["k8s"]["token"], encoding="utf-8") as f:
            text = f.read(65536).strip()
    except (OSError, UnicodeError):
        raise Refused("this host's cluster token cannot be read") from None
    if not text or re.search(r"\s", text):
        raise Refused("this host's cluster token file holds no token")
    return text


def api(conf, method, path, body=None, kind="application/json"):
    """(status, decoded JSON or None) of one call to the cluster's API. Its text is never passed on: an answer about a
    Secret can hold its values."""
    k8s = conf["k8s"]
    url = urllib.parse.urlsplit(k8s["api"])
    try:
        context = ssl.create_default_context(cafile=k8s["ca"])
    except (OSError, ssl.SSLError):
        raise Refused("this host's cluster authority file cannot be read") from None
    # The pinned authority is the cluster's own, which need not follow the strict profile that Python 3.13 asks of
    # every certificate by default: the pin and the name are what this connection trusts.
    context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    headers = {"Authorization": "Bearer " + token(conf), "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = kind
    conn = Pinned(url.hostname, url.port or 443, context, k8s["server_name"], API_LIMIT)
    try:
        conn.request(method, url.path.rstrip("/") + path, body=data, headers=headers)
        answer = conn.getresponse()
        raw = answer.read(ANSWER_MAX + 1)
        status = answer.status
    except ssl.SSLCertVerificationError:
        raise Refused("the cluster's API did not show a certificate of the pinned authority") from None
    except (OSError, ssl.SSLError, http.client.HTTPException) as e:
        raise Refused(f"the cluster's API could not be reached ({type(e).__name__})") from None
    finally:
        conn.close()
    try:
        got = json.loads(raw) if raw and len(raw) <= ANSWER_MAX else None
    except ValueError:
        got = None
    return status, got if isinstance(got, dict) else None


def key_of(secret, var):
    """The value of one key of a Secret, decoded, or None."""
    data = (secret or {}).get("data") or {}
    if not isinstance(data, dict) or not isinstance(data.get(var), str):
        return None
    try:
        return base64.b64decode(data[var], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def k8s_place(conf, name, place, var, value, salt):
    """Write one key of the Secret through the cluster's API, or find it there already, and read it back."""
    namespace, secret = place["namespace"], place["secret"]
    path = f"/api/v1/namespaces/{namespace}/secrets/{secret}"
    status, got = api(conf, "GET", path)
    if status in (401, 403):
        raise Refused(f"the cluster refused this host's token for Secret {namespace}/{secret} (HTTP {status})")
    if status not in (200, 404) or (status == 200 and got is None):
        raise Refused(f"the cluster answered HTTP {status} for Secret {namespace}/{secret}")
    there = got if status == 200 else None
    if var is None:
        return {"writable": True, "present": there is not None}
    old = key_of(there, var)
    if value is None:
        return {"present": old is not None, "fingerprint": fingerprint(salt, old) if old is not None else None}
    if old == value:
        return {"was": "same", "fingerprint": fingerprint(salt, value)}
    encoded = base64.b64encode(value.encode()).decode()
    pending(conf, name)
    conf["_started"] = True                         # a write that was sent can have been taken, whatever came back
    try:
        if there is None:
            status, got = api(conf, "POST", f"/api/v1/namespaces/{namespace}/secrets",
                              {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                               "metadata": {"name": secret, "namespace": namespace}, "data": {var: encoded}})
        else:
            status, got = api(conf, "PATCH", path, {"data": {var: encoded}}, "application/merge-patch+json")
    except Refused as e:
        raise Unsure(f"{e}, while it wrote Secret {namespace}/{secret}") from None
    if status in (401, 403):
        raise Refused(f"Secret {namespace}/{secret} is not there, and the cluster does not let this host's token make "
                      f"it (HTTP {status}); nothing was written" if there is None else
                      f"the cluster refused this host's token a change of Secret {namespace}/{secret} "
                      f"(HTTP {status}); nothing was written")
    if 400 <= status < 500:         # the API server checks a write before it keeps it
        raise Refused(f"the cluster refused the write of Secret {namespace}/{secret} (HTTP {status}); nothing was "
                      f"written")
    if status not in (200, 201) or got is None:
        raise Unsure(f"the cluster answered HTTP {status} to the write of Secret {namespace}/{secret}")
    back = key_of(got, var)
    if back != value:
        raise Unsure(f"Secret {namespace}/{secret} does not read back the value that was written")
    return {"was": "new" if old is None else "replaced", "fingerprint": fingerprint(salt, back)}


# ---------------------------------------------------------------- after a change

def pending(conf, name):
    """Remember, before a change, that this place's --then has to run: one that never ran runs with the next request."""
    if name not in conf["then"]:
        return
    try:
        os.makedirs(conf["state"], mode=0o700, exist_ok=True)
        with open(os.path.join(conf["state"], f"{name}.pending"), "w", encoding="utf-8") as f:
            f.write("1\n")
    except OSError:
        raise Refused("this helper cannot keep its own state folder, so it changed nothing") from None


def then(conf, name):
    """Run the place's --then when a change waits for it: {"code"}, or None when there is nothing to run."""
    mark = os.path.join(conf["state"], f"{name}.pending")
    if name not in conf["then"] or not os.path.exists(mark):
        return None
    try:
        r = subprocess.run(["/bin/sh", "-c", conf["then"][name]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=THEN_LIMIT, start_new_session=True,
                           env={"PATH": PATH, "HOME": os.path.expanduser("~"), "LANG": "C.UTF-8"})
        code = r.returncode
    except subprocess.TimeoutExpired:
        return {"code": None, "error": f"it did not end in {THEN_LIMIT} seconds"}
    except OSError:
        return {"code": None, "error": "it could not be started"}
    if code == 0:
        try:
            os.unlink(mark)
        except OSError:
            pass
    return {"code": code}


# ---------------------------------------------------------------- one request

def serve(conf, line):
    op, name, place, var, value, salt = request(conf, line)
    sink = env_place if place["sink"] == "env" else k8s_place
    done = sink(conf, name, place, var, value if op == "place" else None, salt)
    reply = dict({"ok": True, "v": VERSION, "op": op, "place": name, "sink": place["sink"], "where": place["where"],
                  "name": var}, **done)
    if op == "place":
        ran = then(conf, name)
        if ran is not None:
            reply["then"] = ran
    return reply


def read_line(stream):
    data = stream.read(REQUEST_MAX + 1)
    if len(data) > REQUEST_MAX:
        raise Refused("the request is longer than this helper reads")
    try:
        return data.decode("utf-8").split("\n", 1)[0]
    except UnicodeDecodeError:
        raise Refused("the request is not UTF-8") from None


def main(argv=None):
    try:
        conf = setup(sys.argv[1:] if argv is None else argv)
    except Setup as e:
        reply = {"ok": False, "v": VERSION, "error": f"this host's place helper is not set up right: {e}"}
    else:
        try:
            reply = serve(conf, read_line(sys.stdin.buffer))
        except Refused as e:
            reply = {"ok": False, "v": VERSION, "error": str(e)}
            if isinstance(e, Unsure):
                reply["unknown"] = True
        except Exception as e:          # never pass details on: they could hold a value
            reply = {"ok": False, "v": VERSION, "error": f"the place helper hit an error ({type(e).__name__})"}
            if conf.get("_started"):
                reply["unknown"] = True
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
    return 0 if reply.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())

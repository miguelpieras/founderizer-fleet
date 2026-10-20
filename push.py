#!/usr/bin/env python3
"""Pings to the owner's phone through Apple's push service (MOBILE-APP.md 9, owner feature push). Python standard
library only.

Apple takes a push only over HTTP/2, with a JSON web token signed with ES256, and the standard library has
neither. The hub machine has both in two programs that every Ubuntu and Debian server carries: openssl signs and
curl sends. curl gets its whole request on standard input, never in its arguments, because another user of the
machine can read the arguments of every process.

The key is the one the owner copied to the hub machine, exactly as Apple downloads it: `AuthKey_<key id>.p8`, a
file of the hub user with mode 600, alone in its folder. Its name carries its key id, so nothing else is set up.
This module hands its path to openssl and never reads, logs or returns the key.
"""
import base64
import json
import os
import re
import stat
import subprocess
import time

TEAM = "6EBZ756H9Q"                     # the team of the iPhone app: Apple takes its pushes signed for that team
TOPIC = "com.founderizer.mobile"        # the iPhone app itself
HOSTS = {"production": "https://api.push.apple.com", "development": "https://api.sandbox.push.apple.com"}
KEY_NAME = re.compile(r"AuthKey_([A-Z0-9]{10})\.p8")
DEVICE_TOKEN = re.compile(r"[0-9a-f]{64,200}")
TOKEN_LIFE = 45 * 60    # Apple refuses a token older than an hour, and one that is renewed more often than 20 minutes
TIMEOUT = 15            # seconds for one push
NOT_SENT = {6, 7, 35}   # curl could not resolve, connect or shake hands: nothing left the machine
_tokens = {}            # (key id, path, modified, team) -> (token, made)


class PushError(Exception):
    """Nothing was sent. The message is safe to show and to log: it never holds a key, a token or a payload."""


def key(folder):
    """(key id, path) of the APNs key in folder, or None while the owner has put none there."""
    try:
        names = sorted(n for n in os.listdir(folder) if KEY_NAME.fullmatch(n))
    except (FileNotFoundError, NotADirectoryError, TypeError):
        return None
    if not names:
        return None
    if len(names) > 1:
        raise PushError(f"{len(names)} APNs keys are in {folder}; keep one")
    path = os.path.join(folder, names[0])
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise PushError("the APNs key of the hub must be a regular file of the hub user with mode 600")
    return KEY_NAME.fullmatch(names[0]).group(1), path


def _part(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def raw_signature(der):
    """The 64 bytes of r and s that ES256 asks for, from the DER signature that openssl writes for a P-256 key."""
    if len(der) < 8 or der[0] != 0x30 or der[1] != len(der) - 2:
        raise ValueError("not a DER signature")
    values, at = [], 2
    for _ in range(2):
        if der[at] != 0x02:
            raise ValueError("not a DER signature")
        size = der[at + 1]
        values.append(int.from_bytes(der[at + 2:at + 2 + size], "big"))
        at += 2 + size
    if at != len(der) or any(v.bit_length() > 256 for v in values):
        raise ValueError("not a P-256 signature")
    return b"".join(v.to_bytes(32, "big") for v in values)


def provider_token(key_id, path, team=TEAM):
    """Apple's provider token for this key, signed by openssl with the key file and kept for 45 minutes."""
    mark = (key_id, path, os.stat(path).st_mtime_ns, team)
    kept = _tokens.get(mark)
    if kept and time.time() - kept[1] < TOKEN_LIFE:
        return kept[0]
    t0 = int(time.time())
    signed = _part(json.dumps({"alg": "ES256", "kid": key_id}).encode()) + "." + \
        _part(json.dumps({"iss": team, "iat": t0}).encode())
    try:
        r = subprocess.run(["openssl", "dgst", "-sha256", "-sign", path], input=signed.encode(), capture_output=True,
                           timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        r = None
    if not r or r.returncode != 0 or not r.stdout:
        raise PushError("openssl could not sign with the APNs key of the hub")
    try:
        token = signed + "." + _part(raw_signature(r.stdout))
    except (ValueError, IndexError):
        raise PushError("the APNs key of the hub is not the P-256 key that Apple gives") from None
    _tokens.clear()
    _tokens[mark] = (token, t0)
    return token


def quoted(text):
    """One value of a curl config line: in double quotes, with its backslashes and quotes escaped."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def send(device_token, payload, environment, key_id, path, collapse=None, expires=None, base=None, team=TEAM,
         topic=TOPIC):
    """One push to one device. (status, reason): Apple's HTTP status and its reason, or (0, why) when no answer came,
    so that the push may or may not have arrived. PushError when nothing left the machine."""
    if not isinstance(device_token, str) or not DEVICE_TOKEN.fullmatch(device_token):
        raise PushError("that is not a device token")
    if environment not in HOSTS:
        raise PushError("the environment is production or development")
    headers = {"authorization": "bearer " + provider_token(key_id, path, team), "apns-topic": topic,
               "apns-push-type": "alert", "apns-priority": "10", "content-type": "application/json"}
    if expires is not None:
        headers["apns-expiration"] = str(int(expires))
    if collapse:
        headers["apns-collapse-id"] = collapse
    lines = [f"url = {quoted((base or HOSTS[environment]) + '/3/device/' + device_token)}", "http2", "silent",
             "show-error", f"max-time = {TIMEOUT}", "output = -", 'write-out = "\\n%{http_code}"']
    lines += [f"header = {quoted(name + ': ' + value)}" for name, value in headers.items()]
    lines.append(f"data-binary = {quoted(json.dumps(payload, separators=(',', ':')))}")
    try:
        r = subprocess.run(["curl", "--config", "-"], input=("\n".join(lines) + "\n").encode(), capture_output=True,
                           timeout=TIMEOUT + 10)
    except FileNotFoundError:
        raise PushError("curl is not installed on the hub machine") from None
    except subprocess.TimeoutExpired:
        return 0, "Apple gave no answer in time"
    if r.returncode in NOT_SENT:
        raise PushError(f"Apple's push service cannot be reached (curl {r.returncode})")
    if r.returncode != 0:
        return 0, f"the push stopped before Apple answered (curl {r.returncode})"
    body, _, code = r.stdout.decode("utf-8", "replace").rpartition("\n")
    try:
        reason = str((json.loads(body) if body.strip() else {}).get("reason") or "")[:100]
    except (ValueError, AttributeError):
        reason = ""
    return (int(code) if code.isdigit() else 0), reason

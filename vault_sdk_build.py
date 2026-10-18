#!/usr/bin/env python3
"""Build the pinned Bitwarden SDK library for a Linux vault broker, and say exactly what was built.

A Linux broker makes a new login only in its own process (create_login). The Bitwarden command-line program takes a
new value as a command argument, which every user of the machine can read; this library takes it in memory. The
source is the revision that the Mac vault pins (app/Vault/build.py in the app repository), with the same single
addition: a function that frees an answer with the allocator that made it, and clears it first.

This program builds as an ordinary user, in a folder that it is given. It installs nothing, needs no root, and reads
no token and no secret. Its result is the library and a manifest with their checksums. The broker's installer checks
the library against the manifest (verify) before it puts a root-owned copy in place.

    python3 -B vault_sdk_build.py build --out <folder> [--source-archive <file>]
    python3 -B vault_sdk_build.py verify --manifest <folder>/vault-sdk.json [--library <file>] [--self-test]

Standard library only; Python 3.9+. A build needs cargo (Rust 1.88 or later), a C toolchain and the network (the
pinned source, and the crates that its lock file pins).
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request

# The same source as the Mac vault (app/Vault/build.py): Bitwarden SDK 2.1.0.
SDK_VERSION = "2.1.0"
REV = "0520690b9710af7a8b1e47aad776f002f369688f"
SOURCE_SHA256 = "bc605f4a9b2dd1d4dfdfcb558d2c421cf6d74a87e6bb234ffe1a8176e85497bf"
SOURCE_URL = "https://codeload.github.com/bitwarden/sdk-sm/tar.gz/" + REV
SOURCE_LIMIT = 64 * 1024 * 1024
# The same addition as the Mac vault (app/Vault/ffi-memory.rs), byte for byte.
ADDITION = """
// Fleet's only SDK addition: free a C response with the allocator that created it.
// The SDK's public run_command returns CString::into_raw but has no matching free.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn fleet_free_string(value: *mut std::os::raw::c_char) {
    if !value.is_null() {
        let mut bytes = unsafe { std::ffi::CString::from_raw(value) }.into_bytes_with_nul();
        for byte in &mut bytes {
            unsafe { std::ptr::write_volatile(byte, 0) };
        }
    }
}
"""
ADDITION_SHA256 = "43d0eb44d44fd18bdeba6ea9631a25a03bdc07ff2359430c19843aff0b8ace2f"
ADDITION_FILE = "crates/bitwarden-c/src/lib.rs"
CRATE = "bitwarden-c"
SYMBOLS = ("init", "run_command", "free_mem", "fleet_free_string")
MANIFEST = "vault-sdk.json"
# What a build may take from the caller's environment. Nothing else reaches cargo.
BUILD_ENV = ("PATH", "HOME", "CARGO_HOME", "RUSTUP_HOME", "RUSTUP_TOOLCHAIN", "TMPDIR", "LANG", "CC", "CXX", "AR")


class Stop(Exception):
    """A sentence for the person who runs the build. It never holds a value."""


def library_name(system=None):
    return "libbitwarden_c.dylib" if (system or platform.system()) == "Darwin" else "libbitwarden_c.so"


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(url=None, digest=None, opener=urllib.request.urlopen):
    """The pinned source archive. A file with another checksum is never used."""
    with opener(url or SOURCE_URL, timeout=120) as response:
        payload = response.read(SOURCE_LIMIT + 1)
    return checked(payload, digest)


def checked(payload, digest=None):
    if len(payload) > SOURCE_LIMIT:
        raise Stop("The source archive is larger than the limit.")
    if hashlib.sha256(payload).hexdigest() != (digest or SOURCE_SHA256):
        raise Stop("The source archive does not have the pinned checksum. Nothing was built.")
    return payload


def unpack(payload, folder, rev=REV):
    """Extract the archive into a new source folder. Every member must be a file, a folder or a link that stays
    inside that folder; an archive with anything else is refused whole."""
    root = "sdk-sm-" + rev
    source = Path(folder) / root
    if source.exists():
        shutil.rmtree(source)           # the pinned source again: an edit in the work folder never enters a build
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
        members = archive.getmembers()
        for member in members:
            parts = Path(member.name).parts
            if member.name == "pax_global_header" and member.type in (tarfile.XGLTYPE, tarfile.XHDTYPE):
                continue
            inside = bool(parts) and parts[0] == root and ".." not in parts and not os.path.isabs(member.name)
            if member.issym() or member.islnk():
                target = Path(member.linkname)
                base = Path(*parts[:-1]) if member.issym() else Path()
                resolved = os.path.normpath(base / target)
                inside = inside and not target.is_absolute() and (resolved == root or resolved.startswith(root + os.sep))
            elif not (member.isfile() or member.isdir()):
                inside = False
            if not inside:
                raise Stop("The source archive has an entry outside its own folder. Nothing was built.")
        wanted = [m for m in members if m.name != "pax_global_header"]
        if hasattr(tarfile, "data_filter"):
            archive.extractall(folder, members=wanted, filter="data")
        else:
            archive.extractall(folder, members=wanted)
    if not (source / "Cargo.lock").is_file():
        raise Stop("The source archive has no lock file. Nothing was built.")
    return source


def add_free_function(source):
    """The one addition to the SDK, exactly once."""
    if hashlib.sha256(ADDITION.encode()).hexdigest() != ADDITION_SHA256:
        raise Stop("The addition in this program was changed. Nothing was built.")
    target = Path(source) / ADDITION_FILE
    if not target.is_file():
        raise Stop("The source has no C binding file. Nothing was built.")
    text = target.read_text()
    if "fleet_free_string" in text:
        raise Stop("The source already has the addition. Nothing was built.")
    target.write_text(text + ADDITION)


def tool_version(program, env):
    try:
        done = subprocess.run([program, "--version"], capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip().splitlines()[0] if done.returncode == 0 and done.stdout.strip() else None


def build_environment(environ=None):
    environ = os.environ if environ is None else environ
    return {key: environ[key] for key in BUILD_ENV if environ.get(key)}


def compile_library(source, target, cargo="cargo", environ=None, timeout=3600):
    """cargo build --locked --release for the C binding crate, with the lock file of the pinned source."""
    env = dict(build_environment(environ), CARGO_TARGET_DIR=str(target))
    try:
        done = subprocess.run([cargo, "build", "--locked", "--release", "-p", CRATE], cwd=str(source), env=env, timeout=timeout)
    except FileNotFoundError:
        raise Stop("cargo is not installed. A build needs Rust 1.88 or later.") from None
    except subprocess.TimeoutExpired:
        raise Stop("The build did not end in its time.") from None
    library = Path(target) / "release" / library_name()
    if done.returncode != 0 or not library.is_file():
        raise Stop("The build failed. No library was made.")
    return library


SELF_TEST = r"""
import ctypes, json, sys
library = ctypes.CDLL(sys.argv[1])
for name in sys.argv[2].split(","):
    getattr(library, name)
library.init.restype = ctypes.c_void_p
library.init.argtypes = [ctypes.c_char_p]
library.run_command.restype = ctypes.c_void_p
library.run_command.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
library.free_mem.restype = None
library.free_mem.argtypes = [ctypes.c_void_p]
library.fleet_free_string.restype = None
library.fleet_free_string.argtypes = [ctypes.c_void_p]
settings = json.dumps({"apiUrl": "https://api.invalid", "identityUrl": "https://identity.invalid",
                       "userAgent": "Fleet Vault", "deviceType": "SDK"}).encode()
client = library.init(settings)
if not client:
    sys.exit("no client")
# A command that the SDK does not have: it answers an error from its own parser. Nothing goes to a network.
answer = library.run_command(json.dumps({"fleetSelfTest": {}}).encode(), client)
if not answer:
    sys.exit("no answer")
reply = json.loads(ctypes.string_at(answer).decode())
library.fleet_free_string(answer)
library.free_mem(client)
if reply.get("success") is not False or not isinstance(reply.get("errorMessage"), str):
    sys.exit("unexpected answer")
print("ok")
"""


def self_test(library, timeout=60):
    """Load the library in a process of its own and ask it one command that it refuses by itself: the four functions
    are there, an answer comes back, and the addition frees it. No token, no secret and no network."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise Stop("The self-test loads the library. Run it as an ordinary user, not as root.")
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "RUST_LOG": "off"}
    try:
        # The child starts in "/", so it gets the full path: a path from the caller's folder would name nothing there.
        done = subprocess.run([sys.executable, "-I", "-c", SELF_TEST, os.path.abspath(library), ",".join(SYMBOLS)],
                              capture_output=True, text=True, timeout=timeout, env=env, cwd="/")
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and done.stdout.strip() == "ok"


def manifest_for(library, versions, tested, now=None):
    return {"v": 1, "sdk": SDK_VERSION, "rev": REV, "source_sha256": SOURCE_SHA256, "addition_sha256": ADDITION_SHA256,
            "crate": CRATE, "library": Path(library).name, "library_sha256": sha256_of(library),
            "library_bytes": os.path.getsize(library), "system": platform.system(), "machine": platform.machine(),
            "libc": " ".join(platform.libc_ver()).strip(), "cargo": versions.get("cargo"), "rustc": versions.get("rustc"),
            "symbols": list(SYMBOLS), "self_test": "passed" if tested else "not run",
            "built_at": int(time.time() if now is None else now)}


def build(out, archive=None, cargo="cargo", opener=urllib.request.urlopen, environ=None, test=True):
    """The whole recipe. Returns the manifest, which is also written beside the library."""
    out = Path(out)
    if out.is_symlink() or (out.exists() and not out.is_dir()):
        raise Stop("Give a folder for the result.")
    work = out / "work"
    work.mkdir(parents=True, exist_ok=True)
    env = build_environment(environ)
    versions = {"cargo": tool_version(cargo, env), "rustc": tool_version("rustc", env)}
    if not versions["cargo"]:
        raise Stop("cargo is not installed. A build needs Rust 1.88 or later.")
    payload = checked(Path(archive).read_bytes()) if archive else fetch(opener=opener)
    source = unpack(payload, work)
    add_free_function(source)
    built = compile_library(source, work / "target", cargo=cargo, environ=environ)
    library = out / built.name
    shutil.copyfile(built, library)
    os.chmod(library, 0o644)
    tested = self_test(library) if test else False
    if test and not tested:
        library.unlink()
        raise Stop("The built library did not pass its self-test. It was removed.")
    manifest = manifest_for(library, versions, tested)
    (out / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return manifest


def verify(manifest_path, library=None, load=False):
    """Check a library against its manifest and against this recipe. Without `load` it only reads the two files, so an
    installer can run it with any rights. Returns the manifest."""
    manifest_path = Path(manifest_path)
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        raise Stop("The manifest cannot be read.") from None
    expected = {"v": 1, "sdk": SDK_VERSION, "rev": REV, "source_sha256": SOURCE_SHA256, "addition_sha256": ADDITION_SHA256,
                "crate": CRATE, "symbols": list(SYMBOLS), "self_test": "passed"}
    if not isinstance(manifest, dict) or any(manifest.get(key) != value for key, value in expected.items()):
        raise Stop("The manifest is not of this recipe: another source, another addition, or no passed self-test.")
    name = manifest.get("library")
    if name not in ("libbitwarden_c.so", "libbitwarden_c.dylib"):
        raise Stop("The manifest names no library of this recipe.")
    library = Path(library) if library else manifest_path.parent / name
    if library.is_symlink() or not library.is_file():
        raise Stop("The library is not a regular file.")
    if os.path.getsize(library) != manifest.get("library_bytes") or sha256_of(library) != manifest.get("library_sha256"):
        raise Stop("The library is not the one of the manifest.")
    if load and not self_test(library):
        raise Stop("The library did not pass its self-test.")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("build", help="Build the library and its manifest in a folder")
    p.add_argument("--out", required=True)
    p.add_argument("--source-archive", help="the pinned source archive as a file; its checksum is checked all the same")
    p.add_argument("--cargo", default="cargo")
    p = sub.add_parser("verify", help="Check a library against its manifest and this recipe")
    p.add_argument("--manifest", required=True)
    p.add_argument("--library")
    p.add_argument("--self-test", action="store_true", help="also load the library and ask it one command (not as root)")
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            manifest = build(args.out, archive=args.source_archive, cargo=args.cargo)
        else:
            manifest = verify(args.manifest, library=args.library, load=args.self_test)
    except Stop as stop:
        print(str(stop), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "library": manifest["library"], "library_sha256": manifest["library_sha256"],
                      "rev": manifest["rev"], "self_test": manifest["self_test"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

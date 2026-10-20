# Install a Founderizer worker

The same installer supports Mac Studio, Mac mini, and Linux VPS workers. It uses
a macOS user LaunchAgent or a Linux systemd user service. Run it as the worker's
normal OS user. It does not create accounts, copy credentials, change sleep or
login settings, or enable public access. For a Codex worker, it adds one table to
`~/.codex/config.toml` so that Codex trusts exactly this checkout
(`[projects."<checkout>"]`, `trust_level = "trusted"`); other settings and an
existing entry for this checkout stay as they are.

For a new VPS (Ubuntu or Debian) or a Mac, the usual way is Founderizer → Servers → Add
server: one line on the new machine joins it (UI-API.md, "Servers"), and Founderizer →
Agents → Add agent then installs an agent on it. This page is the manual way.

## Register the machine first

In the shared `team.json`, add the worker under `agents`, with a unique name,
`role: "worker"`, its `machine`, `runtime`, command and capabilities. Add that
machine's private-network address under `addresses` and add
`profiles/<worker-name>.md`. Use the existing Studio or VPS entry as the example.
Commit and push these changes and reload the hub before installing the worker.
This step is explicit: an installer cannot grant a new machine hub access.

Each worker identity must belong to one machine. Do not install `studio` on a
second Mac. The installer checks that the outgoing private-network address is the
registered one, that the hub knows the worker, and that it has no active tasks.
It refuses to replace a service for another checkout.

## Requirements

- All workers: Python 3.10 or later, Git, tmux, a connection to the hub's private
  network, access to the shared Git repository, and the configured Codex or
  Claude CLI. Runtime login is checked without an AI turn. A signed-out runtime is
  reported and needs sign-in before tasks; it does not prevent service installation.
- macOS screen workers: macOS 14 or later, the ChatGPT app with Codex Computer Use
  configured, Xcode Command Line Tools, and the desktop user signed in and unlocked.
  Use the installed Codex binary path as `screen_command` in `team.json`.
- Linux VPS: systemd with user services. Enable linger for the worker user so it
  runs after SSH logout: `loginctl enable-linger "$USER"`. An administrator may be
  required. Linux installation does not provide a macOS desktop or Computer Use.

Install missing requirements with the OS package manager. The installer stops
with the missing command's name; it does not run downloaded code or use sudo.

## macOS

Clone the shared repository, then double-click **Install Founderizer Worker.command**.
Enter the registered worker name. The installer builds and signs **Founderizer Worker**,
installs the LaunchAgent, and starts it. It opens the macOS screen permission flow
for a screen worker. In System Settings → Privacy & Security → Screen & System
Audio Recording, allow **Founderizer Worker**. No audio is captured. Rebuilding the app
can require this permission again.

From Terminal, the same installation is:

```sh
python3 worker_install.py --agent studio --start --authorize-screen
```

The Mac service starts after the user signs in. It does not bypass FileVault or
the lock screen. Leave a dedicated screen worker unlocked while it works.

## Linux VPS or a new checkout

Copy `install-worker.sh` to the machine, then run:

```sh
sh install-worker.sh --repo '<private-repo-url>' --agent vps --directory "$HOME/fleet-pilot"
```

Use the real registered name and repository URL. This clones the repository,
checks requirements and identity, installs the service, and starts the helper.
It refuses an existing target directory. For an existing checkout on either OS:

```sh
python3 worker_install.py --agent <worker-name> --check
python3 worker_install.py --agent <worker-name> --start
```

Open Founderizer → Team to check the worker's fresh helper reading. A running helper
does not prove a browser or desktop task; check one real task after onboarding.

## Update and remove

Finish the worker's tasks, run `git pull --rebase` in its existing checkout, and
run the installer there again. Do not create a second checkout for the same worker.

To stop automatic startup, unload the service and remove only its definition:

```sh
# macOS: replace studio with the worker name.
launchctl bootout "gui/$(id -u)/com.agentfleet.studio"
rm "$HOME/Library/LaunchAgents/com.agentfleet.studio.plist"

# Linux: replace vps with the worker name.
systemctl --user disable --now fleet-vps.service
rm "$HOME/.config/systemd/user/fleet-vps.service"
systemctl --user daemon-reload
```

These commands keep the repository, outputs, usage records and runtime login.

## Vault broker (Linux, V3)

The `vps` worker has a running broker; a worker without one is set up as follows.
It needs root and the owner's approval. It moves the Bitwarden
token and the handover key from the worker's user to a separate user, so that
agents cannot read them, and no broker answer gives a login (UI-API.md, "Vault
isolation (V3)"). `<worker>` is the worker's OS user (`claude`
on the VPS, `fleet` on onboarded workers), `<group>` its primary group, and
`<agent>` its Founderizer name. Run as root on the worker:

```bash
useradd --system --home-dir /var/lib/fleet-vault --shell /usr/sbin/nologin fleet-vault
install -d -o root -g root -m 755 /usr/local/lib/fleet-vault /etc/fleet-vault
install -o root -g root -m 644 ~<worker>/fleet-pilot/vault.py /usr/local/lib/fleet-vault/vault.py
install -o root -g root -m 644 ~<worker>/fleet-pilot/bws.toml /etc/fleet-vault/bws.toml
apt-get install -y age      # /usr/bin/age, owned by root
```

Install `/usr/local/bin/bws` (owner root, mode 755) from the official Bitwarden
Secrets Manager CLI release: check the downloaded zip against the release's
published SHA-256 list, then unzip it. Do not copy the worker's own `bws` (for
example `~/.local/bin/bws`): the worker's user, and so its agents, could have
changed it, and the broker gives this program the token.

Write `/etc/fleet-vault/vault.json` (owner root, mode 644, no secret in it):

```json
{"agent": "<agent>", "hub": "http://100.80.1.2:8765", "worker_uid": <id -u of the worker>,
 "region": "eu", "token_file": "/var/lib/fleet-vault/bws-token",
 "age_key": "/var/lib/fleet-vault/age.key", "socket": "/run/fleet-vault/vault.sock",
 "bws": "/usr/local/bin/bws", "bws_config": "/etc/fleet-vault/bws.toml", "age": "/usr/bin/age"}
```

Then start the broker and move the token and the handover key (they are moved,
never shown or copied; the key keeps its public half, so handovers already made
for this machine stay readable):

```bash
python3 /usr/local/lib/fleet-vault/vault.py unit --config /etc/fleet-vault/vault.json --group <group> > /etc/systemd/system/fleet-vault.service
systemctl daemon-reload && systemctl enable --now fleet-vault
mv ~<worker>/.fleet/bws-token /var/lib/fleet-vault/bws-token
chown fleet-vault:fleet-vault /var/lib/fleet-vault/bws-token && chmod 600 /var/lib/fleet-vault/bws-token
mv ~<worker>/.fleet/age.key /var/lib/fleet-vault/age.key
chown fleet-vault:fleet-vault /var/lib/fleet-vault/age.key && chmod 600 /var/lib/fleet-vault/age.key
grep '^# public key:' /var/lib/fleet-vault/age.key     # the public half: vault_recipient
```

Set `"vault": "broker"` and `"vault_recipient": "<the public half>"` for `<agent>` in
`team.json`, commit, reload the team and update the worker. Check: `sudo -u <worker> sudo -n true` must fail ("a password is required"): a
worker user with passwordless sudo can read the vault as root, so the broker is no
boundary for it. `sudo -u <worker> cat /var/lib/fleet-vault/bws-token` and
`sudo -u <worker> cat /var/lib/fleet-vault/age.key` must fail with "Permission denied"; `python3 fleet.py check <agent>` as the worker
must show "vault broker answers (kept by fleet-vault)", "vault broker key is the
one pinned in team.json" and "vault broker runs the vault.py of this checkout"; and the machine row must show `security.vault`
`{"mode": "broker", "isolated": true}`. To undo: set `"vault": "file"`, move both
files back to `~<worker>/.fleet/` (owner `<worker>`, mode 600), and
`systemctl disable --now fleet-vault`.

### When a release changes vault.py

**A release never reaches the broker.** It updates each agent's own checkout and
its own user service; the broker runs the root-owned copy at
`/usr/local/lib/fleet-vault/vault.py` as a system service, and nothing in the
fleet may touch that without root. `lifecycle.OWNER_ONLY` also lists `vault.py`
among the files a helper never loads, so a commit that changes only that file
advances the revision every machine reports: the Updates page says the fleet is
current and each broker goes on running the copy that was installed by hand.

The fleet says so instead of hiding it. The broker's `status` answers `source`,
the SHA-256 of the program it runs, the helper sends it to the hub as
`security.vault.broker_source`, and `/ui/health` raises one `vault_behind`
warning for each machine whose broker differs from the hub's own `vault.py`. The
checksum is read once, when the broker starts, so a copy installed under a broker
that was not restarted does not clear the warning — the running code is still the
old one.

So after a release that changed `vault.py`, run this as root on each worker that
has a broker, which is the warning's own `fix` line:

```bash
install -o root -g root -m 644 ~<worker>/fleet-pilot/vault.py /usr/local/lib/fleet-vault/vault.py && systemctl restart fleet-vault
```

Then `python3 fleet.py check <agent>` as the worker shows "vault broker runs the
vault.py of this checkout", and the warning goes at that machine's next status
report (within ten minutes). A broker that says nothing about its program — one
older than this, or a Mac's signed helper, which the app updates itself — raises
no warning, because unknown is not behind.

### New logins on a Linux broker (`create_login`): the write setup

A broker only reads until it has this setup. With it, the broker makes a new login
in its own process with the pinned Bitwarden library, so that no value is a program
argument (UI-API.md, "`create_login` on a Linux worker"). It needs root and the
owner's approval. It reads and changes no token: the token in
`/var/lib/fleet-vault/bws-token` must already have write access to the project, which
the owner gives in Bitwarden. Linux on x86_64 with glibc 2.31 or later.

Build the library with the pinned recipe, or take a build that main gave you with its
manifest (`python3 -B vault_sdk_build.py build --out <folder>`, as an ordinary user).
Then, with absolute paths:

```bash
# As an ordinary user: the build is the pinned one, and it loads.
python3 -B vault_sdk_build.py verify --manifest <folder>/vault-sdk.json --self-test
# As root: put the current broker program in place, then do the one setup operation.
install -o root -g root -m 644 ~<worker>/fleet-pilot/vault.py /usr/local/lib/fleet-vault/vault.py
python3 /usr/local/lib/fleet-vault/vault.py sdk-install --config /etc/fleet-vault/vault.json \
    --manifest <folder>/vault-sdk.json --project <UUID of the Bitwarden project for new logins>
systemctl restart fleet-vault
```

`sdk-install` checks the library against its manifest and the pinned recipe, checks
the processor and the C library, makes a root-owned copy
`/usr/local/lib/fleet-vault/libbitwarden_c.so`, tests as the `fleet-vault` user
(never as root, and with no token) that the copy loads, and only then puts the copy
in place and adds `sdk`, `sdk_sha256` and `write_project` to the configuration. A
setup that fails leaves the installed library and the configuration as they were.

Check: after the restart, `journalctl -u fleet-vault` has no line "no new logins",
and the machine row shows `security.vault.creates: true`. The owner then allows the
agent in Founderizer (`vault_create`), and a project with its own permission. To undo:
remove the three keys from `/etc/fleet-vault/vault.json` and restart the broker; it
reads as before. Its record of saves, `/var/lib/fleet-vault/creates.json`, stays.


### API requests on a Linux broker (`call_api`): the curl program

A broker sends no API request until its configuration names a curl program. This
step adds no token and no grant: a request uses a login that the agent already
has (UI-API.md, "`call_api`", part 7). It needs root. An update of the broker
program alone leaves requests off.

Add one key to `/etc/fleet-vault/vault.json` (owner root, mode 644), with the path of
a curl program that root owns and that the worker cannot change, and restart:

```bash
ls -l /usr/bin/curl                                  # owner root, not writable by others
# add:  "curl": "/usr/bin/curl"   to /etc/fleet-vault/vault.json
systemctl show fleet-vault -p KillMode               # must be control-group
systemctl restart fleet-vault
```

Check: the machine row shows `security.vault.requests: true`, and the broker made
`/var/lib/fleet-vault/requests.json` (its record of started requests; it has no
answer and no key). `KillMode=control-group` is the default of the unit that
`vault.py unit` prints: with it, a restart of the broker also ends a curl child that
runs, so no request goes on without its broker. To undo: remove the key and
restart; the broker then sends no requests, and `requests.json` stays.

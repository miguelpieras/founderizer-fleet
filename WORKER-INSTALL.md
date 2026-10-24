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

**The join line installs it.** `join.sh` runs `python3 hubsetup.py vault --hub <hub>`
as root on a Linux machine, beside the root helpers and the agent users, so a machine
that joins the fleet ends with a working broker and the owner runs nothing by hand.
It is safe to run again: every step reads first, and the service is restarted only
when something changed. Run the join line again to repair a machine.

The join line also puts `/usr/local/sbin/fleet-vault-program` in place, the one root
command that brings this broker onto the `vault.py` of a release; the machine's own
server service runs it. See "When a release changes vault.py".

The broker holds the key of the store and the key that opens sealed handovers, as
its own user `fleet-vault`, so that no agent of the machine can read either and no
broker answer gives a login (UI-API.md, "Vault isolation (V3)"). What the install
puts in place, all of it mechanical and none of it a credential of the owner's: the
user; `/usr/local/lib/fleet-vault/vault.py` and `/etc/fleet-vault`, owned by root;
`age`; the handover key in `/var/lib/fleet-vault/age.key`;
`/etc/fleet-vault/vault.json`; the store; and the service.

**It belongs to the machine, not to one agent.** A machine joins before it has its
first agent, so the configuration it gets has no `agent` and every request to such a
broker names its caller, which every current helper does. Who may ask it is
`worker_group`, the group of this machine's agent users (`fleet-agents`): an agent
the owner adds later, or one whose OS user changed, reaches the broker with no change
to its configuration. That is the one thing to get right, because a broker that
admits one user ID was left unreachable by the move to one OS user for each agent --
running, current, and serving nobody.

**A machine with no broker gets the fleet's own store**: one `age` file on that
machine (`store`, `store_key`), made by `vault.py store-init`, with no Bitwarden
account, no organization and no token. So installing a machine needs nothing of the
owner's, and the one thing that cannot be automatic does not arise. A machine whose
broker opens a **Bitwarden project** keeps it, token and write setup included: the
install changes only who may ask it. A broker opens one store or the other, never
both, so a machine with the fleet's own store cannot open a secret of a Bitwarden
project, and the owner's existing brokers are left as they are.

**The owner's keys reach that store by delivery.** A key or a login that the owner
types into Founderizer is saved in the vault of his Mac, which no worker reads. So
the app also seals the value to each machine's pinned vault key, the hub carries the
sealed copy, which it cannot open, and the broker opens it, has the hub compare the
value's fingerprints with the ones the owner's vault registered, and saves it in its
own store under the same `bws://` reference (owner feature `secret_deliveries`). It
takes a broker whose `vault.py` has the `deposit` operation (its status says
`"deposits": true`) and a helper that reports the feature `secret_deposits`; the
Secrets page says, for each machine, whether it can take one and what is missing. A
broker that opens a Bitwarden project takes none.

**The configuration is root with mode 644 and holds no secret.** Do not make it
`root:fleet-vault` 640: the service sets `Group=`, so the vault user's own group is
not among the service's groups, the broker fails on its own configuration, and the
unit restarts for ever.

**Then the fleet is told, by the owner's own click.** The hub records a machine's
store key when the owner first gives an agent of that machine its vault, on the
Secrets page or in the agent's Knowledge & access step; `hub.offered_store` takes the
key the machine's own broker reports when Fleet has recorded none, because the only
writer of `vault_stores` is a grant and the first grant would otherwise be refused
for ever (`no_vault_store`). After that, a reported key that differs is refused
(`vault_store_mismatch`). Until that click the agent holds its secrets and cannot use
them, and the Secrets page says so on the secret itself.

Check, as the worker: `sudo -u <worker> sudo -n true` must fail ("a password is
required"), because a worker user with passwordless sudo can read the vault as root
and the broker is then no boundary for it; `sudo -u <worker> cat
/var/lib/fleet-vault/age.key` must fail with "Permission denied"; `python3 fleet.py
check <agent>` must show "vault broker answers (kept by fleet-vault)", which store it
opens, "vault broker key is the one pinned in team.json" and "vault broker runs the
vault.py of this checkout"; and the machine row must show `security.vault`
`{"mode": "broker", "isolated": true}`.

To undo on one machine: `systemctl disable --now fleet-vault`, and set `"vault":
"none"` for its agents in the app. The store and both keys stay in
`/var/lib/fleet-vault`, so the same machine's broker can be started again; a new key
would leave every value it holds unreadable.

A Mac has no `vault.py` broker. Its vault is the signed `Fleet Vault.app`, started by
the LaunchAgent `com.agentfleet.vault` in the owner's login session, and the app
installs and updates it. It holds the fleet's own store too; see the next section.

## Vault broker (macOS)

The Mac helper opens **one of two stores**, which its configuration chooses, exactly
as a Linux broker does: the fleet's own store on that Mac, or a Bitwarden project.
A fleet of Macs therefore needs no Bitwarden account either. The configuration at
`/Library/Application Support/Fleet Vault/config.json`, root with mode 644, names
one and never both — `"store": "fleet"` or `"region": "eu"|"us"` — and `status`
answers that word, so `python3 fleet.py check <agent>` prints "vault broker opens
the fleet's own store".

The store is `~/Library/Application Support/Fleet Vault/store.age`, mode 600, with
the same format as a Linux store (`{"v": 1, "items": {...}}`, one item for each
`bws://<uuid>`), encrypted to the public half of a store key of its own. That key is
one item of the helper's Keychain group, not a file: on a Mac the agents of the
machine run as the vault's own user, so only the helper's signature keeps the store
shut, and the ciphertext alone opens nothing. The handover key is a separate key, so
rotating one does not orphan the other's values.

Install it from the app repository (`miguelpieras/agents`), with the signed build:

```sh
sudo python3 app/Vault/install.py --app "<build>/Fleet Vault.app" --user <mac user> --agent <agent> --hub http://100.80.1.2:8765 --store
```

Nothing else is needed: the helper makes the store key, the handover key and an empty
store at its first request, and `create_login` works on that store with no project and
no setup. `--switch-store` changes a Mac that opens the other store, and **moves no
value**: a broker opens one store or the other, so the items of the first one are then
unreachable and the owner adds each secret again.

The owner saves one item into a Mac's store with the helper's own commands, the Mac
equivalents of `store-put` and `store-list`. The value is on standard input, so it
stands in no argument, and no answer repeats it:

```sh
"/Applications/Fleet Vault.app/Contents/MacOS/FleetVault" store-put --title "<name>" < item.json
"/Applications/Fleet Vault.app/Contents/MacOS/FleetVault" store-list
```

Then register the reference it gives with the hub and grant it, as for any secret.
Because the agents of a Mac run as the vault's own user, an agent of that machine can
run those two commands: it can add an item and read the names in the store, and no
value and no other machine's store.

### When a release changes vault.py

**A release reaches no checkout's copy of the broker.** It updates each agent's
own checkout and its own user service; the broker runs the root-owned copy at
`/usr/local/lib/fleet-vault/vault.py` as a system service, and nothing in the
fleet may touch that without root. `lifecycle.OWNER_ONLY` also lists `vault.py`
among the files a helper never loads, so a commit that changes only that file
advances the revision every machine reports.

**So the machine brings its own broker along.** The server service of a joined
machine reads the installed program, which is root-owned and world-readable, and
where it differs from the `vault.py` of its own checkout it runs the second root
command the installers put in place:

```bash
/usr/local/sbin/fleet-vault-program
```

It takes **no argument**. It fetches `vault.py` from the fleet's configuration ref
itself, as root, into a bare repository under `/usr/local/lib/fleet-vault` that only
root can write, installs it `root:root` 644 and restarts `fleet-vault`; a broker
that does not come up gets back the program that was serving. So the service user
passes no bytes and names no path, source, destination or service: what it may ask
for is this one act and nothing else. That is narrower than the command this
replaced, which installed from a checkout a user can write.

Nothing else of the broker is touched. Its user, its store, its store key and its
handover key are never read, and the handover key is never remade — a new one would
leave every value the store holds unreadable and turn every agent's access into
`vault_store_mismatch`.

**What root's own clone proves, and what it does not.** It proves that nobody
below root on that machine wrote those bytes. It does not prove they are the
bytes the owner meant: whoever can push the configuration ref can put a program
there, and this one runs as the vault's own user, which holds the store key. The
agents' code already comes from that ref, so the ref could always run code as
every agent — but never as the vault user, and that is the step this adds. What
closes it is a signature over the released revision, verified on the machine
before the blob becomes the program, with the public half pinned root-owned at
install. `release-key.pub` already carries the release key and its private half
is on the website host and never on a hub, so the key exists; what is missing is
a signature this program can check with a stock tool. Until then the trust in
this command is the trust in that ref.

Each machine does this within ten minutes of taking a release, so after a release
that changed `vault.py` the owner runs nothing.

**A machine installed before this needs one installer step run once.** The root
command is written by the installers, and nothing in the fleet can put a new
root-owned file in place by itself. So on a machine whose broker was installed
earlier, as root in that machine's checkout:

```bash
python3 hubsetup.py agent-helper --user <the service user> --repo <the hub's config ref>
```

`--check` first says what it would write. That is the step both installers already
run; it writes nothing when the three files are already the ones that version writes,
and the machine's own server service brings the broker across within ten minutes of
it. Every release after that is automatic.

**Not the join line.** A machine already in the fleet can get no fresh one: the hub
refuses a join code for a name it already has, and the script would redo apt, the
firewall and `POST /join`, with `set -eu` stopping it before the two steps that
matter.

**The warning is now about the machine that could not.** The broker's `status`
answers `source`, the SHA-256 of the program it runs, the helper sends it to the hub
as `security.vault.broker_source`, and `/ui/health` raises one `vault_behind`
warning for each machine whose broker differs from the hub's own `vault.py`. A
warning that stays is one of four things: the machine has a broker and no server
service, its installers are older than that root command, it cannot reach the hub's
configuration ref, or the broker did not come up on the new program and kept the one
that was serving. The `fix` names the command above and, after it, the `agent-helper`
step for a machine that does not have it; `journalctl -u fleet-vault -n 30` says
which of the four it is.

The checksum is read once, when the broker starts, so a copy installed under a
broker that was not restarted does not clear the warning — the running code is
still the old one. Then `python3 fleet.py check <agent>` as the worker shows "vault
broker runs the vault.py of this checkout", and the warning goes at that machine's
next status report (within ten minutes). A broker that says nothing about its
program — one older than this, or a Mac's signed helper, which the app updates
itself — raises no warning, because unknown is not behind.

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

A broker sends no API request until its configuration names a curl program. **The
install names it**: `hubsetup.py vault` adds `"curl": "/usr/bin/curl"` to
`/etc/fleet-vault/vault.json`, where the base packages of `join.sh` put curl, so a
machine that joins can send requests and nobody runs a step by hand. A curl path
that is there already is kept. This adds no token and no grant: a request uses a
login that the agent already has (UI-API.md, "`call_api`", part 7).

**A broker set up by an older installer has no curl key**, and no release adds one: a
release changes the broker program and never its configuration, so an update of the
program alone leaves requests off. Add the one key once, as root, with the path of a
curl program that root owns and that the worker cannot change, and restart:

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
restart; the broker then sends no requests, and `requests.json` stays. A later run
of `hubsetup.py vault` adds the key again.

### Keys into a server or a cluster (`place_secret`): targets

A key in a machine's vault often has to reach the place where an application reads
it: one variable of a server's `.env` file, or one key of a Kubernetes Secret. The
owner defines each such place once, on the Targets page, and an agent that holds the
target and the key calls `place_secret` (owner feature `secret_targets`). The vault
of the machine that the owner chose reads the key from its own store and sends it on
one SSH connection, with an SSH key of its own, to `place.py` on the target's host,
which writes it, reads it back and answers a salted fingerprint. No value passes the
hub, a helper or an agent, and the host's line in `authorized_keys` lets that key run
`place.py` with the owner's places and nothing else.

**On the vault's machine, nothing to set.** A Linux broker takes the stock
`/usr/bin/ssh` and makes its own key, `/var/lib/fleet-vault/place_ed25519`, once at
its start, with the `ssh-keygen` beside it. Nothing names that `ssh` anywhere; only a
machine whose one is somewhere else writes it in `/etc/fleet-vault/vault.json`, where
it stays an override and nothing else.

**But the broker has to be running a `vault.py` with the `place` operation**, and that
is not the same thing as the machine being up to date. The broker runs a root-owned
copy outside every checkout, which a release does not touch: only
`/usr/local/sbin/fleet-vault-program` brings it across, and only an installer run puts
that command there (see "When a release changes vault.py"). **A machine installed
before that command exists has no way to get a new `vault.py` at all**, so its vault
reaches no target however current the machine looks. Its own server service says so
every ten minutes — "The server needs /usr/local/sbin/fleet-vault-program from the
Fleet installer; run the join line again on <name>" — and the `agent-helper` step
above is the whole fix: it puts the command in place, the machine brings its broker
onto the release's `vault.py` within ten minutes, and every release after that
catches the broker up by itself. Check: the
machine row shows `security.vault.places: true` and its `place_key`, and
`journalctl -u fleet-vault` has no line "no targets".

**In Founderizer, the Targets page.** A target is its kind (`env` or `k8s`), where it
is (the absolute path of the `.env` file, or `NAMESPACE/SECRET`), the machine whose
vault reaches the host, and the host's SSH address, port and user. Optionally the only
names it takes, a note that every agent that places a key there reads, and a command
that the host runs after a change (a restart of the application). No host key is
typed: Add server pins it.

**On the target's host: one line.** Press **Add server** on the host's row and run the
line it gives as root on that host (owner feature `target_servers`):

```bash
curl -fsS http://<hub>:8765/place | FLEET_PLACE=<code> sh
```

The code works once and for 30 minutes. The line installs `place.py` as
`/usr/local/lib/fleet-place/place.py`, checked against the SHA-256 the hub serves with
it; makes sure the user is there with a login shell; writes the one line of that user's
`authorized_keys` file that lets the vault's key run the helper and nothing else; and
reports `/etc/ssh/ssh_host_ed25519_key.pub`, which the hub pins on every target of that
host. Then the host's row goes green and **Test** checks the place end to end.

- Run the line again when you add a target on that host: the row says "Its line is
  older than the targets on it", and the script replaces its own line instead of adding
  a second one. A changed host key or address needs no new line.
- The host must reach the hub, because it takes the helper from there and reports its
  own key. A host that cannot is set up by hand, from "Set it up by hand" in that
  form: install `place.py` as above and put the line it shows in the user's
  `authorized_keys` file, then paste the host's key into the target.
- The user is the owner of the file that it writes, or root: a file written by
  another user would change its owner, so `place.py` refuses that. It needs a login
  shell (sshd runs a forced command through it, and `/usr/sbin/nologin` runs nothing)
  and no password: the line's `restrict` gives the vault's key no shell, no terminal
  and no forwarding.
- The `.env` file is there already: `place.py` changes one variable and keeps the
  rest of the file, its owner, its group, its mode and its single link, and it puts a
  complete new file in its place, never a half-written one.
- The host's sshd is the one at the target's port: on a host whose port 22 belongs to
  Tailscale SSH, the plain sshd at its own port (2222, for example).

**A Kubernetes Secret.** When the cluster's API is reached only from inside (a node,
or a host that its firewall admits), the target's host is that host: `place.py` calls
the API there with a token of a ServiceAccount that may change only the Secret it
needs, and the token stays on that host. For example, for `content/api-keys`:

```bash
kubectl -n content create serviceaccount fleet-place
kubectl -n content create role fleet-place --verb=get,patch --resource=secrets --resource-name=api-keys
kubectl -n content create rolebinding fleet-place --role=fleet-place --serviceaccount=content:fleet-place
kubectl -n content apply -f - <<'YAML'
apiVersion: v1
kind: Secret
metadata: {name: fleet-place-token, annotations: {kubernetes.io/service-account.name: fleet-place}}
type: kubernetes.io/service-account-token
YAML
kubectl -n content get secret fleet-place-token -o jsonpath='{.data.token}' | base64 -d > token
kubectl -n content get secret fleet-place-token -o jsonpath='{.data.ca\.crt}' | base64 -d > ca.crt
```

Put the two files on the target's host (for example `/etc/fleet-place/token`, owned by
the target's user with mode 600, and `/etc/fleet-place/ca.crt`), and delete the copies
that the commands left. Make the Secret first (`kubectl -n content create secret
generic api-keys`): Kubernetes cannot limit `create` to one name, so this role has
none. The target's `k8s` settings are the API's https address as that host reaches
it, the two files, and `server_name` when the API's certificate does not name that
address.

Check: the Test button on the Targets page connects with the vault's key and says
whether the place can be written, and writes nothing. To undo: take the line out of
`authorized_keys` on that host, which is the one thing that lets a vault in. A vault's
own key opens nothing by itself, so a machine needs nothing undone.

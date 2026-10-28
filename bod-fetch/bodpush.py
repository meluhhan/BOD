"""bodpush - copy one day's fetched BOD files to the UAT server, safely.

    python bodfetch.py push --dry-run          # what would change on the server; writes nothing
    python bodfetch.py push                    # stage -> verify -> back up -> swap -> verify
    python bodfetch.py push --local /tmp/uat   # same flow into a local folder (no server needed)

Flow (the Greeksoft UAT procedure's "staging folder first, then replace", done over SFTP straight from
this machine to the UAT server - no RDP/.73 hop):
  1. collect   every selected file must be in downloads/<date>/ with the sha256 recorded at fetch time;
               anything missing stops the push (unless --allow-partial)
  2. plan      per file: NEW / REPLACE / SAME (same sha256 already in the target -> skipped)
  3. stage     upload to <staging>/<date>/ as .part, rename, check sha256 on the server
  4. back up   copy the target's current version of every file we replace to <backup>/<date>/
  5. swap      rename staged file -> target (atomic per file; staging and target share a filesystem)
  6. verify    sha256 of every file in the target equals the local one

The target is never touched until every file is staged and verified. A lock file stops two pushes
running at once. Only files we push are written; nothing else in the target is changed or deleted.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import pathlib
import posixpath
import shlex
import shutil
import socket
from typing import Callable, Protocol

LIVE_FOLDER = "contract"  # Greeksoft's real folder; writing there needs --live


class PushError(Exception):
    pass


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- 1. collect
@dataclasses.dataclass
class Item:
    id: str
    name: str
    local: pathlib.Path
    size: int
    sha256: str
    action: str = ""  # NEW / REPLACE / SAME, set by plan()


def collect(out_dir: pathlib.Path, entries: list[dict], render_name: Callable[[dict], str]
            ) -> tuple[list[Item], list[tuple[str, str]]]:
    """Files fetched for this day (from state.json). Returns (items, missing[(id, expected name)]).
    A file whose bytes no longer match the sha256 recorded at fetch time counts as missing."""
    state_path = out_dir / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    items, missing = [], []
    for e in entries:
        key = str(e["id"])
        st = state.get(key)
        path = out_dir / st["name"] if st else None
        if not st or not path.exists() or path.stat().st_size != st["size"] or sha256_file(path) != st["sha256"]:
            missing.append((key, st["name"] if st else render_name(e)))
            continue
        items.append(Item(key, st["name"], path, st["size"], st["sha256"]))
    return items, missing


# --------------------------------------------------------------------------- remote side
class Remote(Protocol):
    def exists(self, path: str) -> bool: ...
    def makedirs(self, path: str) -> None: ...
    def put(self, local: pathlib.Path, path: str) -> None: ...
    def rename(self, src: str, dst: str) -> None: ...     # must overwrite dst
    def copy(self, src: str, dst: str) -> None: ...       # server-side copy, keeps mtime
    def sha256(self, path: str) -> str: ...
    def remove(self, path: str) -> None: ...
    def create_exclusive(self, path: str, text: str) -> bool: ...  # False if it already exists
    def close(self) -> None: ...


class LocalRemote:
    """A folder on this machine standing in for the server: for tests and `push --local`."""

    def exists(self, path):
        return os.path.exists(path)

    def makedirs(self, path):
        os.makedirs(path, exist_ok=True)

    def put(self, local, path):
        shutil.copyfile(local, path)

    def rename(self, src, dst):
        os.replace(src, dst)

    def copy(self, src, dst):
        shutil.copy2(src, dst)

    def sha256(self, path):
        return sha256_file(pathlib.Path(path))

    def remove(self, path):
        os.remove(path)

    def create_exclusive(self, path, text):
        try:
            with open(path, "x") as f:
                f.write(text)
            return True
        except FileExistsError:
            return False

    def close(self):
        pass


class SshRemote:
    """The UAT server over SSH/SFTP (paramiko). Host key must already be known (~/.ssh/known_hosts):
    an unknown or changed key is refused, never auto-accepted.
    Auth, in order: keys held by ssh-agent (macOS keeps your unlocked key there), `key_file` (+ passphrase),
    then a password - the last two only if configured. Encrypted key files are never read without a passphrase."""

    def __init__(self, host: str, user: str, port: int = 22, password: str | None = None,
                 key_file: str | None = None, passphrase: str | None = None, timeout: float = 20):
        try:
            import paramiko
        except ImportError:
            raise PushError("paramiko is needed for push:  pip install paramiko")
        self._paramiko = paramiko
        try:  # a clear "not reachable" beats a confusing auth error
            socket.create_connection((host, port), timeout=timeout).close()
        except OSError as ex:
            raise PushError(f"cannot reach {host}:{port} ({ex.strerror or ex}) - on the office network/VPN?") from None
        self.ssh = paramiko.SSHClient()
        self.ssh.load_system_host_keys()
        self.ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            self.ssh.connect(host, port=port, username=user, password=password or None,
                             key_filename=os.path.expanduser(key_file) if key_file else None,
                             passphrase=passphrase or None, timeout=timeout,
                             allow_agent=True, look_for_keys=False)
        except paramiko.AuthenticationException:
            raise PushError(f"SSH login refused for {user}@{host}. Set up a key once with "
                            f"`ssh-copy-id {user}@{host}` and `ssh-add --apple-use-keychain ~/.ssh/id_ed25519`, "
                            f"or set the password in .env") from None
        except paramiko.SSHException as ex:
            if "No authentication methods" in str(ex):
                raise PushError(f"no SSH key in ssh-agent and no password in .env for {user}@{host}. One-time setup: "
                                f"`ssh-add --apple-use-keychain ~/.ssh/id_ed25519` then `ssh-copy-id {user}@{host}`") from None
            raise PushError(f"SSH to {user}@{host}:{port} failed: {ex}") from None
        except OSError as ex:
            raise PushError(f"cannot reach {host}:{port} ({ex.strerror or ex}) - on the office network/VPN?") from None
        self.sftp = self.ssh.open_sftp()

    def _run(self, cmd: str) -> str:
        _, out, err = self.ssh.exec_command(cmd, timeout=300)
        rc = out.channel.recv_exit_status()
        if rc != 0:
            raise PushError(f"server command failed ({rc}): {cmd}\n{err.read().decode(errors='replace')[:300]}")
        return out.read().decode(errors="replace")

    def exists(self, path):
        try:
            self.sftp.stat(path)
            return True
        except FileNotFoundError:
            return False

    def makedirs(self, path):
        self._run(f"mkdir -p -- {shlex.quote(path)}")

    def put(self, local, path):
        self.sftp.put(str(local), path, confirm=True)

    def rename(self, src, dst):
        self.sftp.posix_rename(src, dst)  # overwrites dst atomically (OpenSSH extension)

    def copy(self, src, dst):
        self._run(f"cp -p -- {shlex.quote(src)} {shlex.quote(dst)}")

    def sha256(self, path):
        return self._run(f"sha256sum -- {shlex.quote(path)}").split()[0]

    def remove(self, path):
        self.sftp.remove(path)

    def create_exclusive(self, path, text):
        try:
            f = self.sftp.open(path, "wx")  # "x" alone is not writable in paramiko
        except OSError:
            return False  # already exists
        try:
            with f:
                f.write(text)
        except OSError:
            self.sftp.remove(path)  # never leave a lock behind that we failed to fill
            raise
        return True

    def close(self):
        try:
            self.sftp.close()
        finally:
            self.ssh.close()


# --------------------------------------------------------------------------- 2-6. plan and push
@dataclasses.dataclass
class Paths:
    target: str
    staging: str  # day folder
    backup: str   # day folder
    lock: str


def day_paths(target: str, staging_root: str, backup_root: str, day: dt.date) -> Paths:
    tag = day.isoformat()
    return Paths(target=target, staging=posixpath.join(staging_root, tag),
                 backup=posixpath.join(backup_root, tag), lock=posixpath.join(staging_root, ".push.lock"))


def check_target(target: str, live: bool):
    if posixpath.basename(target.rstrip("/")) == LIVE_FOLDER and not live:
        raise PushError(f"target {target} is Greeksoft's live '{LIVE_FOLDER}' folder - pass --live to write there")


def plan(items: list[Item], remote: Remote, p: Paths) -> list[Item]:
    for it in items:
        dst = posixpath.join(p.target, it.name)
        if not remote.exists(dst):
            it.action = "NEW"
        else:
            it.action = "SAME" if remote.sha256(dst) == it.sha256 else "REPLACE"
    return items


def push(items: list[Item], remote: Remote, p: Paths, log: Callable[[str], None] = print) -> dict:
    """Stage, verify, back up, swap, verify. The target is untouched until every file is staged and verified."""
    todo = [it for it in items if it.action in ("NEW", "REPLACE")]
    report = {"target": p.target, "started": dt.datetime.now().isoformat(timespec="seconds"),
              "files": {it.name: it.action for it in items}}
    if not todo:
        log("Nothing to do: every file is already in the target.")
        return {**report, "result": "NOTHING_TO_DO"}

    remote.makedirs(posixpath.dirname(p.lock))
    if not remote.create_exclusive(p.lock, f"{os.getlogin() if hasattr(os, 'getlogin') else '?'} "
                                           f"{dt.datetime.now().isoformat(timespec='seconds')}\n"):
        raise PushError(f"another push is running (lock {p.lock}); remove it only if you are sure it is stale")
    try:
        # 3. stage
        remote.makedirs(p.staging)
        for i, it in enumerate(todo, 1):
            staged = posixpath.join(p.staging, it.name)
            remote.put(it.local, staged + ".part")
            remote.rename(staged + ".part", staged)
            if remote.sha256(staged) != it.sha256:
                raise PushError(f"checksum mismatch after upload: {it.name} - target untouched")
            log(f"  staged {i}/{len(todo)}  {it.name}  ({it.size:,} bytes)")
        # 4. back up what we are about to overwrite
        replace = [it for it in todo if it.action == "REPLACE"]
        if replace:
            remote.makedirs(p.backup)
            for it in replace:
                remote.copy(posixpath.join(p.target, it.name), posixpath.join(p.backup, it.name))
            log(f"  backed up {len(replace)} current file(s) to {p.backup}")
        # 5. swap
        remote.makedirs(p.target)
        for it in todo:
            remote.rename(posixpath.join(p.staging, it.name), posixpath.join(p.target, it.name))
        # 6. verify
        bad = [it.name for it in todo if remote.sha256(posixpath.join(p.target, it.name)) != it.sha256]
        if bad:
            raise PushError(f"verification failed in target for {bad}; previous versions are in {p.backup}")
        log(f"  swapped {len(todo)} file(s) into {p.target} and verified checksums")
        return {**report, "result": "OK", "backup": p.backup if replace else None,
                "finished": dt.datetime.now().isoformat(timespec="seconds")}
    finally:
        try:
            remote.remove(p.lock)
        except Exception:
            pass

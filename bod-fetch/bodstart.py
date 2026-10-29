"""bodstart - start the 15 Greeksoft apps in order and wait until each one is really up.

    python bodstart.py plan                         # print the start order from apps.yaml; starts nothing
    python bodstart.py run --dry-run                # print the exact commands; starts nothing
    python bodstart.py run --fake                   # Mac test with fake apps (tests/fake_startapp.py), times x0.01
    python bodstart.py run --fake --fake-plan RMS=crash
    python bodstart.py run --confirm                # the real apps (on .76) - only with the user's OK

Replaces AUTO/BOD_F's fixed sleeps (CLAUDE.md 3.2):
  1. serial    the first 5 apps, one at a time; the next starts only when this one is READY.
               A failure stops here: the apps after it are NOT_STARTED.
  2. parallel  the other 10, `stagger` seconds apart, then wait for all of them.
  3. report    per app READY / FAILED / TIMEOUT / NOT_STARTED, written to <log-dir>/report.json.

Each app runs in its own pseudo-terminal (the apps show interactive menus and need one); everything it
prints goes to <log-dir>/<id>.log, and readiness is read from that output. The pty belongs to this
process, so after a real run it stays in the foreground holding the apps; Ctrl-C stops them all.
ASSUMPTION: on .76 the apps may have to stay in visible gnome-terminal tabs instead (as BOD_F does) -
decide after reading BOD_F and startapp.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import pty
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from typing import Callable

import yaml

HERE = pathlib.Path(__file__).resolve().parent
FAKE_APP = HERE / "tests" / "fake_startapp.py"


def load_config(path: pathlib.Path) -> dict:
    cfg = yaml.safe_load(path.read_text())
    apps = cfg["apps"]
    ids = [a["id"] for a in apps]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate app id in " + path.name)
    groups = [a["group"] for a in apps]
    if set(groups) - {"serial", "parallel"}:
        raise ValueError("group must be serial or parallel")
    if "parallel" in groups and "serial" in groups[groups.index("parallel"):]:
        raise ValueError("all serial apps must come before the parallel ones")
    for a in apps:
        if len(set(a["ready"]) & {"output", "after"}) != 1:
            raise ValueError(f"{a['id']}: ready needs exactly one of output / after")
    return cfg


# --------------------------------------------------------------------------- one app
class App:
    """One app in its own pty. Output is copied to a log file and kept for readiness checks."""

    def __init__(self, spec: dict, argv: list[str], cwd: str, log_path: pathlib.Path,
                 timeout: float, env: dict | None = None):
        self.spec, self.argv, self.cwd, self.log_path, self.timeout, self.env = spec, argv, cwd, log_path, timeout, env
        self.proc: subprocess.Popen | None = None
        self.started = 0.0
        self.detail = ""
        self._out = ""
        self._lock = threading.Lock()

    def start(self) -> None:
        master, slave = pty.openpty()
        self.proc = subprocess.Popen(self.argv, cwd=self.cwd, stdin=slave, stdout=slave, stderr=slave,
                                     start_new_session=True, env=self.env)
        os.close(slave)
        self.master = master
        self.started = time.monotonic()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        with open(self.log_path, "ab") as log:
            while True:
                try:
                    data = os.read(self.master, 4096)
                except OSError:  # the app closed its terminal (exited)
                    break
                if not data:
                    break
                log.write(data)
                log.flush()
                with self._lock:
                    self._out += data.decode(errors="replace")

    def output(self) -> str:
        with self._lock:
            return self._out

    def check(self, now: float) -> str | None:
        """READY / FAILED / TIMEOUT, or None while still waiting."""
        ready = self.spec["ready"]
        if "output" in ready and re.search(ready["output"], self.output()):
            return "READY"
        code = self.proc.poll()
        if code is not None:
            self.detail = f"exited with code {code} before ready"
            return "FAILED"
        if "after" in ready and now - self.started >= ready["after_s"]:
            return "READY"
        if now - self.started >= self.timeout:
            self.detail = f"not ready after {self.timeout:g} s"
            return "TIMEOUT"
        return None

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()
        try:
            os.close(self.master)
        except (OSError, AttributeError):
            pass


# --------------------------------------------------------------------------- the run
def run(cfg: dict, *, start_cmd: str, workdir: str, log_dir: pathlib.Path, scale: float = 1.0,
        env: dict | None = None, log: Callable[[str], None] = print, poll: float = 0.05
        ) -> tuple[dict, list[App]]:
    """Start every app in apps.yaml order. Returns (report, started apps). Never stops apps itself."""
    s = cfg["settings"]
    log_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    rows = {a["id"]: {"id": a["id"], "app": a["args"], "group": a["group"], "status": "NOT_STARTED"}
            for a in cfg["apps"]}
    started: list[App] = []

    def launch(spec: dict) -> App:
        spec = {**spec, "ready": dict(spec["ready"])}
        if "after" in spec["ready"]:
            spec["ready"]["after_s"] = spec["ready"]["after"] * scale
        timeout = spec.get("timeout", s["default_timeout"]) * scale
        app = App(spec, shlex.split(start_cmd.format(args=spec["args"])), workdir,
                  log_dir / f"{spec['id']}.log", timeout, env)
        app.start()
        started.append(app)
        rows[spec["id"]]["started_s"] = round(app.started - t0, 2)
        log(f"  start  {spec['id']:<5} {spec['args']}")
        return app

    def wait(apps: list[App]) -> None:
        pending = list(apps)
        while pending:
            now = time.monotonic()
            for app in list(pending):
                status = app.check(now)
                if status:
                    pending.remove(app)
                    row = rows[app.spec["id"]]
                    row.update(status=status, ready_s=round(now - t0, 2), detail=app.detail)
                    log(f"  {status:<6} {app.spec['id']:<5} after {now - app.started:.1f} s {app.detail}")
            time.sleep(poll)

    serial = [a for a in cfg["apps"] if a["group"] == "serial"]
    parallel = [a for a in cfg["apps"] if a["group"] == "parallel"]
    ok = True
    for spec in serial:
        wait([launch(spec)])
        if rows[spec["id"]]["status"] != "READY":
            log(f"  {spec['id']} did not come up - the apps after it are not started")
            ok = False
            break
    if ok:
        batch = []
        for i, spec in enumerate(parallel):
            if i:
                time.sleep(s["stagger"] * scale)
            batch.append(launch(spec))
        wait(batch)

    report = {
        "result": "OK" if all(r["status"] == "READY" for r in rows.values()) else "FAILED",
        "time": dt.datetime.now().isoformat(timespec="seconds"),
        "seconds": round(time.monotonic() - t0, 1),
        "apps": list(rows.values()),
    }
    (log_dir / "report.json").write_text(json.dumps(report, indent=2))
    return report, started


# --------------------------------------------------------------------------- command line
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["plan", "run"])
    ap.add_argument("--config", type=pathlib.Path, default=HERE / "apps.yaml")
    ap.add_argument("--dry-run", action="store_true", help="print what would be started; start nothing")
    ap.add_argument("--fake", action="store_true", help="use tests/fake_startapp.py instead of the real apps")
    ap.add_argument("--fake-plan", default="", help='e.g. "RMS=crash,N1=hang" (see tests/fake_startapp.py)')
    ap.add_argument("--scale", type=float, help="multiply every wait/timeout (default 1, or 0.01 with --fake)")
    ap.add_argument("--log-dir", type=pathlib.Path)
    ap.add_argument("--confirm", action="store_true", help="required to start the REAL apps")
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    s = cfg["settings"]
    start_cmd, workdir, env = s["start_cmd"], s["workdir"], None
    if a.fake:
        start_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE_APP))} {{args}}"
        workdir = str(HERE)
        env = {**os.environ, "FAKE_PLAN": a.fake_plan}
    scale = a.scale if a.scale is not None else (0.01 if a.fake else 1.0)

    if a.command == "plan" or a.dry_run:
        print(f"workdir: {workdir}")
        for spec in cfg["apps"]:
            ready = spec["ready"]
            how = f"output /{ready['output']}/" if "output" in ready else f"running after {ready['after'] * scale:g} s"
            timeout = spec.get("timeout", s["default_timeout"]) * scale
            print(f"  {spec['group']:<8} {spec['id']:<5} {start_cmd.format(args=spec['args']):<40} ready: {how}, timeout {timeout:g} s")
        return 0

    if not a.fake and not a.confirm:
        print("Refusing to start the REAL apps without --confirm (UAT is live; see CLAUDE.md 3.3).")
        return 2

    log_dir = a.log_dir or HERE / "start-logs" / dt.date.today().isoformat() / dt.datetime.now().strftime("%H%M%S")
    print(f"logs: {log_dir}")
    report, apps = run(cfg, start_cmd=start_cmd, workdir=workdir, log_dir=log_dir, scale=scale, env=env)
    n_ready = sum(r["status"] == "READY" for r in report["apps"])
    print(f"{report['result']}: {n_ready}/{len(report['apps'])} apps ready in {report['seconds']} s")

    if a.fake:
        for app in apps:
            app.stop()
    else:
        print("Apps are running in this process's terminals. Ctrl-C stops ALL of them.")
        try:
            while any(app.proc.poll() is None for app in apps):
                time.sleep(5)
        except KeyboardInterrupt:
            for app in apps:
                app.stop()
    return 0 if report["result"] == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())

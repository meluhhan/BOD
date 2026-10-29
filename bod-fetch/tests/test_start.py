"""Tests for bodstart (no server needed: tests/fake_startapp.py stands in for the apps). Run:  python tests/test_start.py"""
import os
import pathlib
import shlex
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import bodstart as bs  # noqa: E402

CFG = bs.load_config(ROOT / "apps.yaml")
SCALE = 0.01  # 300 s timeout -> 3 s, SMM's 120 s -> 1.2 s


def _run(fake_plan="", cfg=CFG):
    log_dir = pathlib.Path(tempfile.mkdtemp()) / "logs"
    cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(bs.FAKE_APP))} {{args}}"
    env = {**os.environ, "FAKE_PLAN": fake_plan, "FAKE_DELAY": "0.05"}
    report, apps = bs.run(cfg, start_cmd=cmd, workdir=str(ROOT), log_dir=log_dir, scale=SCALE, env=env, log=lambda m: None)
    for app in apps:
        app.stop()
    return report, {r["id"]: r for r in report["apps"]}, log_dir


def test_config_matches_bod_f():
    apps = CFG["apps"]
    assert len(apps) == 15
    assert [a["id"] for a in apps if a["group"] == "serial"] == ["M", "S", "C", "R", "G"]
    assert sum(a["group"] == "parallel" for a in apps) == 10


def test_all_ready_in_order():
    report, rows, log_dir = _run()
    assert report["result"] == "OK", report
    assert all(r["status"] == "READY" for r in rows.values())
    serial = ["M", "S", "C", "R", "G"]
    for prev, nxt in zip(serial, serial[1:]):                        # each serial app waits for the one before
        assert rows[nxt]["started_s"] >= rows[prev]["ready_s"]
    last_serial_ready = rows["G"]["ready_s"]
    assert all(r["started_s"] >= last_serial_ready for r in rows.values() if r["group"] == "parallel")
    assert "Enter Your Selection" in (log_dir / "M.log").read_text()  # app output is logged
    assert (log_dir / "report.json").exists()


def test_serial_crash_stops_the_rest():
    report, rows, _ = _run("RMS=crash")
    assert report["result"] == "FAILED"
    assert rows["R"]["status"] == "FAILED" and "exited with code 1" in rows["R"]["detail"]
    assert [rows[i]["status"] for i in ("M", "S", "C")] == ["READY"] * 3
    assert all(rows[i]["status"] == "NOT_STARTED" for i in rows if i not in ("M", "S", "C", "R"))


def test_serial_hang_times_out():
    report, rows, _ = _run("ClientCommunicator=hang")
    assert rows["C"]["status"] == "TIMEOUT"
    assert rows["R"]["status"] == "NOT_STARTED"


def test_parallel_failure_does_not_block_others():
    # FAKE_PLAN matches app names, so use one that has a single tab (MC). Its 15 s "after" scales to 0.15 s, close to
    # when the fake crashes; give it 1 s so the crash lands inside the window.
    # (Same limitation for real: an `after:` app that dies after the window counts as READY.)
    cfg = {**CFG, "apps": [{**a, "ready": {"after": 100}} if a["id"] == "MC" else a for a in CFG["apps"]]}
    report, rows, _ = _run("MarginCalculator=crash", cfg)
    assert report["result"] == "FAILED"
    assert rows["MC"]["status"] == "FAILED"
    assert all(r["status"] == "READY" for i, r in rows.items() if i != "MC")


def test_real_run_needs_confirm():
    assert bs.main(["run"]) == 2  # refuses before starting anything


def test_dry_run_starts_nothing():
    assert bs.main(["run", "--dry-run"]) == 0


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")

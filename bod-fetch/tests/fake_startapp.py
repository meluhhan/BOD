"""fake_startapp - stands in for Greeksoft's `./startapp <App>` so bodstart.py can be tested on the Mac.

    python tests/fake_startapp.py RMS

Behaves like the real apps as far as we know (CLAUDE.md 3.2): prints startup lines, then the
"MAIN MENU ... Enter Your Selection" prompt, then waits for input forever. Needs a terminal (pty),
like the real ones.

FAKE_PLAN picks a behaviour per app, e.g. FAKE_PLAN="RMS=crash,GATS=hang,SMM=slow":
  ok     (default) ready after FAKE_DELAY seconds
  slow   ready after 3 x FAKE_DELAY
  crash  exits with code 1 before ready
  hang   keeps running, never shows the menu
FAKE_DELAY defaults to 0.3 s.
"""
import os
import sys
import time

app = sys.argv[1] if len(sys.argv) > 1 else "?"
plan = dict(p.split("=", 1) for p in os.environ.get("FAKE_PLAN", "").split(",") if "=" in p)
mode = plan.get(app, "ok")
delay = float(os.environ.get("FAKE_DELAY", "0.3"))

if not sys.stdin.isatty():
    print(f"{app}: needs a terminal", flush=True)
    sys.exit(2)

print(f"Starting {app} ...", flush=True)
print(f"{app}: connecting to database", flush=True)
time.sleep(delay * (3 if mode == "slow" else 1))

if mode == "crash":
    print(f"{app}: FATAL could not connect", flush=True)
    sys.exit(1)
if mode == "hang":
    while True:
        time.sleep(60)

print(f"\n===== {app} MAIN MENU =====\n1. Status\n0. Exit\nEnter Your Selection: ", end="", flush=True)
for line in sys.stdin:
    if line.strip() == "0":
        break
    print("Enter Your Selection: ", end="", flush=True)

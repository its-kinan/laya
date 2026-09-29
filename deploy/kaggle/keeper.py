#!/usr/bin/env python3
"""Keeper for the Kaggle CPU Laya serve.

Kaggle sessions die at ~12h, so this rotates the serve notebook *before* that:
every run it health-checks the current backend; if it is unhealthy or older
than MAX_AGE_H it pushes a fresh kernel version with a new per-run tunnel id,
waits until the new backend answers /health, then flips the stable public
alias (kaggle-laya-cpu) to the new backend. The old session coasts to its
12h death with zero traffic -- overlapping runs, no downtime.

Public URL never changes:
  https://tunnel-relay.ediprnm-keen.workers.dev/t/kaggle-laya-cpu/v1/systemone

Run from cron every 15 minutes. State in state.json next to this script.
"""
import json, os, subprocess, sys, time
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, "workspace", "tunnel", "kaggle-serve")
STATE_PATH = os.path.join(BASE, "state.json")
LOG_PATH = os.path.join(BASE, "keeper.log")
KERNEL_DIR = os.path.join(BASE, "kernel")  # generated notebook goes here

ALIAS = "kaggle-laya-cpu"
RELAY = "https://tunnel-relay.ediprnm-keen.workers.dev"
MAX_AGE_H = 10.5
BOOT_WAIT_S = 25 * 60

def log(msg):
    line = "%s %s" % (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), msg)
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

def secret():
    for line in open(os.path.join(HOME, "workspace", "tunnel", ".env.local")):
        if line.startswith("SECRET="):
            return line.strip().split("=", 1)[1]
    raise RuntimeError("SECRET not found in .env.local")

def curl_get(path, timeout=30):
    """GET via curl (urllib is 403'd by the sandbox egress proxy; curl is not)."""
    r = subprocess.run(
        ["curl", "-s", "-m", str(timeout), "-o", "/dev/null",
         "-w", "%{http_code}", RELAY + path],
        capture_output=True, text=True)
    code = r.stdout.strip()
    return code == "200"

def backend_healthy(backend_id):
    return curl_get("/t/%s/health" % backend_id, timeout=30)

def load_state():
    try:
        return json.load(open(STATE_PATH))
    except Exception:
        return {}

def save_state(s):
    json.dump(s, open(STATE_PATH, "w"), indent=1)

def run(cmd, **kw):
    log("+ " + " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        log("FAILED rc=%d\n%s\n%s" % (r.returncode, r.stdout[-2000:], r.stderr[-2000:]))
    return r

def rotate(state):
    new_id = "kaggle-laya-cpu-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    log("rotating: %s -> %s" % (state.get("backend_id"), new_id))

    # 1. build the notebook with the per-run backend id
    env = dict(os.environ, SERVE_TUNNEL_ID=new_id, SERVE_KERNEL_DIR=KERNEL_DIR)
    r = run([sys.executable, os.path.join(BASE, "build-serve-nb.py")], env=env)
    if r.returncode != 0:
        log("build failed, will retry next tick"); return False

    # 2. push -> new kernel version starts running
    r = run(["kaggle", "kernels", "push", "-p", KERNEL_DIR], timeout=300)
    if r.returncode != 0 or "successfully pushed" not in (r.stdout + r.stderr).lower():
        log("push failed, will retry next tick"); return False

    # 3. wait for the new backend to answer health through the relay
    t0 = time.time()
    while time.time() - t0 < BOOT_WAIT_S:
        if backend_healthy(new_id):
            log("new backend %s healthy after %.0fs" % (new_id, time.time() - t0))
            break
        time.sleep(60)
    else:
        log("new backend never became healthy; keeping old alias, will retry")
        return False

    # 4. flip the public alias to the new backend
    s = secret()
    r = subprocess.run(
        ["curl", "-s", "-m", "30", "-X", "POST", RELAY + "/admin/alias",
         "-H", "Authorization: Bearer " + s,
         "-H", "Content-Type: application/json",
         "-d", json.dumps({"alias": ALIAS, "backend": new_id})],
        capture_output=True, text=True)
    if '"ok":true' in r.stdout:
        log("alias flip: %s" % r.stdout.strip()[:200])
    else:
        log("alias flip FAILED: %s %s" % (r.stdout[:200], r.stderr[:200])); return False

    # 5. sanity: public URL serves through the new backend
    time.sleep(3)
    if backend_healthy(ALIAS):
        log("public alias healthy")
    else:
        log("WARNING: public alias not healthy after flip")

    save_state({"backend_id": new_id,
                "started_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "consec_failures": 0})
    log("rotation complete")
    return True

def main():
    state = load_state()
    backend = state.get("backend_id")
    if not backend:
        log("no state: initial rotation")
        rotate(state)
        return
    try:
        started = datetime.strptime(state["started_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        started = None
    age_h = (datetime.now(timezone.utc) - started).total_seconds() / 3600 if started else 999

    healthy = backend_healthy(backend)
    fails = 0 if healthy else state.get("consec_failures", 0) + 1
    state["consec_failures"] = fails
    save_state(state)

    log("backend=%s age=%.1fh healthy=%s fails=%d" % (backend, age_h, healthy, fails))

    if age_h >= MAX_AGE_H:
        log("age %.1fh >= %.1fh: scheduled rotation" % (age_h, MAX_AGE_H))
        rotate(state)
    elif fails >= 2:
        log("backend unhealthy twice in a row: early rotation")
        rotate(state)

if __name__ == "__main__":
    main()

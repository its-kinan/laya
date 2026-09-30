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
import fcntl
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, "workspace", "tunnel", "kaggle-serve")
STATE_PATH = os.path.join(BASE, "state.json")
LOG_PATH = os.path.join(BASE, "keeper.log")
KERNEL_DIR = os.path.join(BASE, "kernel")  # generated notebook goes here
KAGGLE = os.path.join(HOME, "workspace", ".venvs", "laya", "bin", "kaggle")

ALIAS = "kaggle-laya-cpu"
RELAYS_PATH = os.path.join(HOME, "workspace", "tunnel", "relays.json")
# Per-kernel shutdown tokens, written by build-serve-nb.py: {backend_id: token}.
# Lets the keeper tell a running kernel to end its session via
# POST /t/<backend-id>/admin/shutdown — no Kaggle API needed.
TOKEN_PATH = os.path.join(BASE, "shutdown_tokens.json")
# Fallback if relays.json is missing: relay A only.
RELAY_A = "https://tunnel-relay.ediprnm-keen.workers.dev"
MAX_AGE_H = 10.5
BOOT_WAIT_S = 25 * 60

def log(msg):
    line = "%s %s" % (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), msg)
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

# Mutual exclusion: never run two keeper passes (tick + manual rotate, or two
# overlapping ticks) concurrently. A second rotation mid-flight double-pushes
# kernels and the later one silently supersedes the earlier one's alias flip.
# Reentrant within this process so rotate() can be called via main() or directly.
LOCK_PATH = os.path.join(BASE, "keeper.lock")
_lock_fd = None

def acquire_lock():
    global _lock_fd
    if _lock_fd is not None:
        return True
    try:
        f = open(LOCK_PATH, "w")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        return False
    _lock_fd = f  # held for the life of the process; never closed
    return True

def secret(name="SECRET"):
    for line in open(os.path.join(HOME, "workspace", "tunnel", ".env.local")):
        if line.startswith(name + "="):
            return line.strip().split("=", 1)[1]
    raise RuntimeError("%s not found in .env.local" % name)

def load_relays():
    """[(base_url, secret)] from relays.json; falls back to relay A only."""
    try:
        cfg = json.load(open(RELAYS_PATH))
        out = []
        for r in cfg.get("relays", []):
            out.append((r["host"], secret(r.get("secret_name", "SECRET"))))
        if out:
            return out
        log("relays.json has no relays; using relay A only")
    except Exception as e:
        log("relays.json unreadable (%s); using relay A only" % e)
    return [(RELAY_A, secret("SECRET"))]

def curl_get(base, path, timeout=60):
    """GET via curl (urllib is 403'd by the sandbox egress proxy; curl is not)."""
    r = subprocess.run(
        ["curl", "-s", "-m", str(timeout), "-o", "/dev/null",
         "-w", "%{http_code}", base + path],
        capture_output=True, text=True)
    code = r.stdout.strip()
    return code == "200"

def relay_healthy(base, backend_id):
    return curl_get(base, "/t/%s/health" % backend_id, timeout=60)

def backend_healthy(backend_id, relays):
    """Healthy if ANY relay serves the backend (laya-live fails over)."""
    return any(relay_healthy(base, backend_id) for base, _ in relays)

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

def alias_target(base, sec):
    """Current backend the public alias points at on this relay (None if unreadable)."""
    r = subprocess.run(
        ["curl", "-s", "-m", "30", base + "/admin/alias?name=" + ALIAS,
         "-H", "Authorization: Bearer " + sec],
        capture_output=True, text=True)
    try:
        return json.loads(r.stdout).get("backend")
    except Exception:
        return None

def heal_aliases(relays, per_relay, backend):
    """Self-heal: on any relay where the backend is verified healthy but the
    public alias points elsewhere (e.g. relay missed the flip during rotation),
    re-flip the alias. Never points an alias at an unverified backend."""
    for (base, sec), (_, healthy) in zip(relays, per_relay):
        if not healthy:
            continue
        target = alias_target(base, sec)
        if target is None:
            log("WARNING: cannot read alias on %s; skipping heal" % base)
            continue
        if target != backend:
            log("alias on %s points at %s, expected %s: re-flipping" % (base, target, backend))
            flip_alias(base, sec, backend)

def load_tokens():
    try:
        return json.load(open(TOKEN_PATH))
    except Exception:
        return {}

def shutdown_backend(backend_id, relays):
    """Tell a running serving kernel to end its Kaggle session (frees the
    slot) via its /admin/shutdown endpoint, reached through the relay tunnel
    at the kernel's own tunnel id. Best-effort across relays: True if any
    relay reports ok. Never raises."""
    tokens = load_tokens()
    token = tokens.get(backend_id, "")
    if not token:
        log("no shutdown token for %s; skipping remote shutdown" % backend_id)
        return False
    for base, _sec in relays:
        try:
            r = subprocess.run(
                ["curl", "-s", "-m", "30", "-X", "POST",
                 base + "/t/%s/admin/shutdown" % backend_id,
                 "-H", "Authorization: Bearer " + token],
                capture_output=True, text=True, timeout=40)
            if '"ok":true' in r.stdout:
                log("remote shutdown accepted for %s via %s" % (backend_id, base))
                return True
            log("remote shutdown via %s: %s %s"
                % (base, r.stdout[:200], r.stderr[:200]))
        except Exception as e:
            log("remote shutdown via %s failed: %s" % (base, e))
    return False

def flip_alias(base, sec, backend_id):
    r = subprocess.run(
        ["curl", "-s", "-m", "30", "-X", "POST", base + "/admin/alias",
         "-H", "Authorization: Bearer " + sec,
         "-H", "Content-Type: application/json",
         "-d", json.dumps({"alias": ALIAS, "backend": backend_id})],
        capture_output=True, text=True)
    if '"ok":true' in r.stdout:
        log("alias flip on %s: %s" % (base, r.stdout.strip()[:200]))
        return True
    log("alias flip FAILED on %s: %s %s" % (base, r.stdout[:200], r.stderr[:200]))
    return False

def rotate(state, relays):
    if not acquire_lock():
        log("rotation already in progress elsewhere; aborting this rotate()")
        return False
    new_id = "kaggle-laya-cpu-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    log("rotating: %s -> %s" % (state.get("backend_id"), new_id))

    # 1. build the notebook with the per-run backend id
    #    (build-serve-nb.py reads relays.json: one agent per relay)
    env = dict(os.environ, SERVE_TUNNEL_ID=new_id, SERVE_KERNEL_DIR=KERNEL_DIR)
    r = run([sys.executable, os.path.join(BASE, "build-serve-nb.py")], env=env)
    if r.returncode != 0:
        log("build failed, will retry next tick"); return False

    # 2. push -> new kernel version starts running
    r = run([KAGGLE, "kernels", "push", "-p", KERNEL_DIR], timeout=300)
    if r.returncode != 0 or "successfully pushed" not in (r.stdout + r.stderr).lower():
        log("push failed, will retry next tick"); return False

    # 3. wait for the new backend to answer health through each relay.
    #    Relays where it never appears keep the old alias (loud warning);
    #    never point an alias at a backend unverified on that relay.
    ready = []
    t0 = time.time()
    pending = list(relays)
    while pending and time.time() - t0 < BOOT_WAIT_S:
        for base, sec in list(pending):
            if relay_healthy(base, new_id):
                log("new backend %s healthy on %s after %.0fs"
                    % (new_id, base, time.time() - t0))
                ready.append((base, sec))
                pending.remove((base, sec))
        if pending:
            time.sleep(60)
    for base, _ in pending:
        log("WARNING: new backend never healthy on %s; alias there keeps old backend" % base)
    if not ready:
        log("new backend never became healthy anywhere; keeping old alias, will retry")
        return False

    # 4. flip the public alias to the new backend on every ready relay
    ok = True
    for base, sec in ready:
        if not flip_alias(base, sec, new_id):
            ok = False

    # 5. sanity: public URL serves through the new backend on flipped relays
    time.sleep(3)
    for base, _ in ready:
        if relay_healthy(base, ALIAS):
            log("public alias healthy on %s" % base)
        else:
            log("WARNING: public alias not healthy on %s after flip" % base)
            ok = False

    if not ok:
        log("rotation partially failed; will retry next tick")
        return False

    # 6. The old backend is fully redundant only if every relay flipped.
    #    Then tell it to end its session now (frees the Kaggle slot within
    #    ~60s) instead of lingering to its 12h cap. Best-effort: the kernel's
    #    own alias polling is the backstop, so a failure here is only logged.
    old_id = state.get("backend_id")
    if old_id and old_id != new_id and len(ready) == len(relays):
        if shutdown_backend(old_id, ready):
            log("old backend %s shutting down; slot frees shortly" % old_id)
        else:
            log("WARNING: remote shutdown of %s failed; its alias polling "
                "will stop it within minutes" % old_id)
    elif old_id and old_id != new_id:
        log("not all relays flipped; old backend %s stays for unflipped relays"
            % old_id)

    save_state({"backend_id": new_id,
                "started_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "consec_failures": 0})
    log("rotation complete")
    return True

def main():
    if not acquire_lock():
        log("another keeper run in progress; skipping tick")
        return
    relays = load_relays()
    log("relays: %s" % [b for b, _ in relays])
    state = load_state()
    backend = state.get("backend_id")
    if not backend:
        log("no state: initial rotation")
        rotate(state, relays)
        return
    try:
        started = datetime.strptime(state["started_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        started = None
    age_h = (datetime.now(timezone.utc) - started).total_seconds() / 3600 if started else 999

    # Per-relay health: a backend is unhealthy only if NO relay serves it.
    # One relay down must not trigger a kernel rotation (rotation can't fix
    # a relay outage, and laya-live fails over to the healthy one).
    per_relay = [(b, relay_healthy(b, backend)) for b, _ in relays]
    healthy = any(h for _, h in per_relay)
    for b, h in per_relay:
        if not h:
            log("WARNING: backend %s not reachable via %s" % (backend, b))
    fails = 0 if healthy else state.get("consec_failures", 0) + 1
    state["consec_failures"] = fails
    save_state(state)

    log("backend=%s age=%.1fh healthy=%s fails=%d" % (backend, age_h, healthy, fails))

    # Converge public aliases: a relay that missed the flip (outage during
    # rotation, late agent connect) gets its alias repaired once verified.
    heal_aliases(relays, per_relay, backend)

    if age_h >= MAX_AGE_H:
        log("age %.1fh >= %.1fh: scheduled rotation" % (age_h, MAX_AGE_H))
        rotate(state, relays)
    elif fails >= 2:
        log("backend unhealthy on all relays twice in a row: early rotation")
        rotate(state, relays)

if __name__ == "__main__":
    # Manual ops: python3 keeper.py shutdown <backend-id>
    # Tells a running serving kernel to end its Kaggle session via the relay
    # tunnel (no Kaggle API/UI). Useful to free a session slot by hand.
    if len(sys.argv) == 3 and sys.argv[1] == "shutdown":
        _relays = load_relays()
        _ok = shutdown_backend(sys.argv[2], _relays)
        print("shutdown %s: %s" % (sys.argv[2], "accepted" if _ok else "FAILED"))
        sys.exit(0 if _ok else 1)
    main()

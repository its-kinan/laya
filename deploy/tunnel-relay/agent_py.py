#!/usr/bin/env python3
"""Tunnel agent (Python port of agent.mjs): dials OUT to the Cloudflare Worker
relay via WebSocket and forwards bridged HTTP requests to a local target.

Usage: agent_py.py --tunnel ID --secret SECRET --worker-host HOST --local-port PORT
Env fallback: TUNNEL_ID / TUNNEL_SECRET / WORKER_HOST / LOCAL_PORT.
"""
import argparse, asyncio, base64, json, os, sys, time
import http.client
import websockets

def cfg(name, default=None):
    return os.environ.get(name, default)

async def forward_to_local(msg, ws, local_port, timeout):
    body = base64.b64decode(msg["body"]) if msg.get("body") else None
    headers = dict(msg.get("headers") or {})
    headers.pop("content-length", None)
    if body:
        headers["content-length"] = str(len(body))
    headers["host"] = f"127.0.0.1:{local_port}"
    headers["connection"] = "close"
    try:
        conn = http.client.HTTPConnection("127.0.0.1", local_port, timeout=timeout)
        conn.request(msg.get("method", "GET"), msg.get("path", "/"), body=body, headers=headers)
        res = conn.getresponse()
        data = res.read()
        out = {}
        for k, v in res.getheaders():
            lk = k.lower()
            if lk in ("transfer-encoding", "connection"):
                continue
            out[lk] = v
        frame = {"t": "res", "id": msg["id"], "status": res.status,
                 "headers": out, "body": base64.b64encode(data).decode()}
    except Exception as e:
        frame = {"t": "res", "id": msg["id"], "status": 502,
                 "headers": {"content-type": "text/plain"},
                 "body": base64.b64encode(f"local target unreachable: {e}\n".encode()).decode()}
    if True:
        try:
            await ws.send(json.dumps(frame))
        except Exception:
            pass

async def handle(ws, local_port, timeout):
    async def pinger():
        while True:
            await asyncio.sleep(20)
            try:
                await ws.send(json.dumps({"t": "ping"}))
            except Exception:
                return
    pt = asyncio.create_task(pinger())
    try:
        async for raw in ws:
            if isinstance(raw, bytes):
                continue  # TCP streams not used by the HTTP agent
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("t") == "req":
                asyncio.create_task(forward_to_local(msg, ws, local_port, timeout))
    finally:
        pt.cancel()

async def run(tunnel, secret, worker_host, local_port, timeout):
    # worker_host may arrive as a full URL (https://host) from relays.json;
    # the agent needs the bare host for the wss:// URL.
    worker_host = worker_host.split("://", 1)[-1].rstrip("/")
    url = f"wss://{worker_host}/agent/connect?tunnel={tunnel}"
    backoff = 1
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    while True:
        try:
            print(f"[agent] connecting to {worker_host} ...", flush=True)
            kw = {"additional_headers": {"Authorization": f"Bearer {secret}"}}
            if proxy:
                kw["proxy"] = proxy
            async with websockets.connect(url, **kw) as ws:
                print("[agent] connected", flush=True)
                backoff = 1
                await handle(ws, local_port, timeout)
        except Exception as e:
            print(f"[agent] disconnected ({e}), retry in {backoff}s", flush=True)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tunnel", default=cfg("TUNNEL_ID"))
    ap.add_argument("--secret", default=cfg("TUNNEL_SECRET"))
    ap.add_argument("--worker-host", default=cfg("WORKER_HOST", "tunnel-relay.ediprnm-keen.workers.dev"))
    ap.add_argument("--local-port", type=int, default=int(cfg("LOCAL_PORT", "8000")))
    ap.add_argument("--timeout", type=int, default=110)
    a = ap.parse_args()
    if not a.tunnel or not a.secret:
        sys.exit("need --tunnel and --secret")
    asyncio.run(run(a.tunnel, a.secret, a.worker_host, a.local_port, a.timeout))

if __name__ == "__main__":
    main()

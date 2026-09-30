// tunnel-relay: public relay for the VM tunnel.
// The VM agent dials OUT via WebSocket to /agent/connect?tunnel=<id> (auth: Bearer TUNNEL_SECRET).
// Public traffic hits /t/<id>/* and is bridged through that socket by the Durable Object.
// Single-file module: cf-api uploads one script part.

function b64encode(buf) {
  const bytes = new Uint8Array(buf);
  let s = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    s += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return btoa(s);
}

export class TunnelRelay {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.agent = null;          // agent WebSocket
    this.pending = new Map();   // reqId -> {resolve, reject}
    this.seq = 0;
    this.lastSeen = 0;          // ms epoch of last agent frame
    this.sshClients = new Map(); // streamId -> client WebSocket (raw TCP bridge)
    this.streamSeq = 0;
  }

  async fetch(req) {
    const url = new URL(req.url);
    if (url.pathname === "/agent/connect") return this.handleAgentConnect(req);
    if (url.pathname === "/agent/status") {
      const online = !!(this.agent && this.agent.readyState === 1);
      return Response.json({ online, lastSeen: this.lastSeen || null });
    }
    if (url.pathname === "/ssh") return this.handleSsh(req);
    return this.handlePublic(req);
  }

  async handleAgentConnect(req) {
    if (req.headers.get("Upgrade") !== "websocket") {
      return new Response("expected websocket\n", { status: 426 });
    }
    const auth = req.headers.get("Authorization") || "";
    const secret = this.env.TUNNEL_SECRET || "";
    if (!secret || auth !== `Bearer ${secret}`) {
      return new Response("unauthorized\n", { status: 401 });
    }
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    if (this.agent) {
      try { this.agent.close(4000, "replaced by new agent"); } catch (_) {}
    }
    this.agent = server;
    this.lastSeen = Date.now();
    server.accept();
    server.addEventListener("message", (ev) => this.onAgentMessage(ev.data));
    const down = () => this.onAgentDown();
    server.addEventListener("close", down);
    server.addEventListener("error", down);
    return new Response(null, { status: 101, webSocket: client });
  }

  async handlePublic(req) {
    if (!this.agent || this.agent.readyState !== 1) {
      return new Response("tunnel offline: no agent connected\n", { status: 502 });
    }
    const id = String(++this.seq);
    const url = new URL(req.url);
    const headers = {};
    req.headers.forEach((v, k) => {
      const lk = k.toLowerCase();
      if (lk === "host" || lk.startsWith("cf-") || lk.startsWith("x-forwarded")) return;
      headers[lk] = v;
    });
    let bodyB64 = null;
    if (req.body && req.method !== "GET" && req.method !== "HEAD") {
      bodyB64 = b64encode(await req.arrayBuffer());
    }
    const frame = JSON.stringify({
      t: "req", id, method: req.method,
      path: url.pathname + url.search, headers, body: bodyB64,
    });
    const responsePromise = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Response("tunnel timeout\n", { status: 504 }));
      }, 120000);  // 120s: CPU inference (e.g. Laya on Kaggle) can take ~30-60s
      this.pending.set(id, {
        resolve: (res) => { clearTimeout(timer); resolve(res); },
        reject: (err) => { clearTimeout(timer); reject(err); },
      });
    });
    try {
      this.agent.send(frame);
    } catch (_) {
      this.pending.delete(id);
      return new Response("tunnel offline: send failed\n", { status: 502 });
    }
    try {
      return await responsePromise;
    } catch (e) {
      return e instanceof Response ? e : new Response("tunnel error\n", { status: 502 });
    }
  }

  // Raw TCP bridge for SSH: client opens a WebSocket here, the agent
  // dials 127.0.0.1:22 on the VM and pipes bytes both ways.
  // Framing on the agent socket: text frames are JSON control
  // ({t:"tcp-open"|"tcp-close", id, ...}), binary frames are
  // 4-byte big-endian stream id + payload.
  async handleSsh(req) {
    if (req.headers.get("Upgrade") !== "websocket") {
      return new Response("expected websocket\n", { status: 426 });
    }
    if (!this.agent || this.agent.readyState !== 1) {
      return new Response("tunnel offline: no agent connected\n", { status: 502 });
    }
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    const streamId = (this.streamSeq = (this.streamSeq + 1) >>> 0) || 1;
    this.sshClients.set(streamId, server);
    server.accept();
    try {
      this.agent.send(JSON.stringify({ t: "tcp-open", id: streamId, host: "127.0.0.1", port: 22 }));
    } catch (_) {
      this.sshClients.delete(streamId);
      return new Response("tunnel offline: send failed\n", { status: 502 });
    }
    server.addEventListener("message", (ev) => {
      let payload;
      if (typeof ev.data === "string") {
        payload = new TextEncoder().encode(ev.data);
      } else {
        payload = new Uint8Array(ev.data);
      }
      const frame = new Uint8Array(4 + payload.length);
      new DataView(frame.buffer).setUint32(0, streamId);
      frame.set(payload, 4);
      try {
        this.agent.send(frame);
      } catch (_) {
        try { server.close(1011, "agent send failed"); } catch (_) {}
      }
    });
    const cleanup = () => {
      if (!this.sshClients.has(streamId)) return;
      this.sshClients.delete(streamId);
      try { this.agent.send(JSON.stringify({ t: "tcp-close", id: streamId })); } catch (_) {}
    };
    server.addEventListener("close", cleanup);
    server.addEventListener("error", cleanup);
    return new Response(null, { status: 101, webSocket: client });
  }

  onAgentMessage(data) {
    this.lastSeen = Date.now();
    if (typeof data !== "string") {
      // binary frame: 4-byte stream id + payload -> SSH client
      const buf = new Uint8Array(data);
      if (buf.length < 4) return;
      const id = new DataView(buf.buffer, buf.byteOffset, 4).getUint32(0);
      const client = this.sshClients.get(id);
      if (client && client.readyState === 1) {
        try { client.send(buf.slice(4)); } catch (_) {}
      }
      return;
    }
    let msg;
    try { msg = JSON.parse(data); } catch (_) { return; }
    if (msg.t === "ping") return; // heartbeat
    if (msg.t === "tcp-close") {
      const client = this.sshClients.get(msg.id);
      if (client) {
        this.sshClients.delete(msg.id);
        try { client.close(1000, "tcp closed"); } catch (_) {}
      }
      return;
    }
    if (msg.t !== "res") return;
    const p = this.pending.get(msg.id);
    if (!p) return;
    this.pending.delete(msg.id);
    const headers = new Headers(msg.headers || {});
    let body = null;
    if (msg.body) {
      const bin = atob(msg.body);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      body = bytes;
    }
    p.resolve(new Response(body, { status: msg.status || 200, headers }));
  }

  onAgentDown() {
    this.agent = null;
    for (const [, p] of this.pending) {
      p.reject(new Response("tunnel offline\n", { status: 502 }));
    }
    this.pending.clear();
    // SSH sessions can't survive an agent reconnect (stream ids are
    // per-connection), so drop them loudly instead of hanging.
    for (const [, ws] of this.sshClients) {
      try { ws.close(1012, "tunnel agent reconnected"); } catch (_) {}
    }
    this.sshClients.clear();
  }
}

export default {
  async fetch(req, env) {
    return route(req, env);
  },
};

const ID_RE = /^[a-zA-Z0-9_-]{8,64}$/;

async function resolveAlias(env, id) {
  try {
    const b = await env.ALIASES.get("alias:" + id);
    if (b && ID_RE.test(b)) return b;
  } catch (_) {}
  return id;
}

async function route(req, env) {
    const url = new URL(req.url);
    if (url.pathname === "/") {
      return new Response(
        "tunnel-relay: VM tunnel relay\nusage: /t/<tunnel-id>/<path>  (HTTP)\n       /t/<tunnel-id>/ssh      (WebSocket raw TCP bridge -> VM 127.0.0.1:22)\n       /t/<alias>/<path>       (alias -> current backend, see /admin/alias)\n",
        { headers: { "content-type": "text/plain" } }
      );
    }
    if (url.pathname === "/admin/alias") {
      return handleAdminAlias(req, env);
    }
    if (url.pathname === "/agent/connect") {
      const id = url.searchParams.get("tunnel");
      if (!id || !ID_RE.test(id)) {
        return new Response("bad tunnel id\n", { status: 400 });
      }
      const stub = env.TUNNEL.get(env.TUNNEL.idFromName(id));
      return stub.fetch(req);
    }
    const m = url.pathname.match(/^\/t\/([a-zA-Z0-9_-]{8,64})(\/.*)?$/);
    if (m) {
      const backend = await resolveAlias(env, m[1]);
      const stub = env.TUNNEL.get(env.TUNNEL.idFromName(backend));
      const newUrl = new URL(req.url);
      newUrl.pathname = m[2] || "/";
      return stub.fetch(new Request(newUrl, req));
    }
    return new Response("not found\n", { status: 404 });
}

async function handleAdminAlias(req, env) {
  const auth = req.headers.get("Authorization") || "";
  const secret = env.TUNNEL_SECRET || "";
  if (!secret || auth !== `Bearer ${secret}`) {
    return new Response("unauthorized\n", { status: 401 });
  }
  const url = new URL(req.url);
  if (req.method === "GET") {
    const alias = url.searchParams.get("name") || "";
    if (!ID_RE.test(alias)) return new Response("bad alias\n", { status: 400 });
    const backend = await resolveAlias(env, alias);
    return Response.json({ alias, backend, is_alias: backend !== alias });
  }
  if (req.method === "POST") {
    let body;
    try { body = await req.json(); } catch (_) { return new Response("bad json\n", { status: 400 }); }
    const alias = String(body.alias || ""), backend = String(body.backend || "");
    if (!ID_RE.test(alias) || !ID_RE.test(backend)) {
      return new Response("bad alias or backend id\n", { status: 400 });
    }
    await env.ALIASES.put("alias:" + alias, backend);
    return Response.json({ ok: true, alias, backend });
  }
  return new Response("method not allowed\n", { status: 405 });
}

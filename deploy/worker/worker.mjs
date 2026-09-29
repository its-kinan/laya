// laya worker — dashboard + breakdown API + ingest + routing + rotation keeper.
// Bindings: LAYA_KV (kv_namespace). Plain env: RELAY_BASE, ALIASES, ROTATE_AFTER_H.
// Secrets (env.NAME): KAGGLE_API_TOKEN, RELAY_ADMIN_SECRET, INGEST_SECRET, ROUTE_SECRET.
// Dependency-free.

const ID_RE = /^[a-zA-Z0-9_-]{8,64}$/;

function aliasList(env) {
  return String(env.ALIASES || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

// All relay traffic goes through the RELAY service binding (tunnel-relay worker).
// A Worker cannot fetch() a sibling Worker's public *.workers.dev URL (error 1042),
// so we never use the global fetch for the relay.
function relayFetch(env, path, init) {
  return env.RELAY.fetch("https://relay.internal" + path, init);
}

async function getJson(kv, key, fallback) {
  try {
    const v = await kv.get(key);
    if (v == null) return fallback;
    return JSON.parse(v);
  } catch (_) {
    return fallback;
  }
}

async function logEvent(env, alias, kind, msg) {
  try {
    const key = "laya:events";
    let ev = await getJson(env.LAYA_KV, key, []);
    if (!Array.isArray(ev)) ev = [];
    ev.push({ ts: new Date().toISOString(), alias: String(alias || ""), kind: String(kind), msg: String(msg || "") });
    if (ev.length > 200) ev = ev.slice(ev.length - 200);
    await env.LAYA_KV.put(key, JSON.stringify(ev));
  } catch (_) {
    // never fail the caller on logging
  }
}

function authed(req, secret) {
  const auth = req.headers.get("Authorization") || "";
  return !!secret && auth === "Bearer " + secret;
}

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function ageH(startedUtc) {
  if (!startedUtc) return null;
  const ms = Date.parse(startedUtc);
  if (!isFinite(ms)) return null;
  return Math.round(((Date.now() - ms) / 3600000) * 10) / 10;
}

function healthLabel(state) {
  if (!state) return ["no state", "#888"];
  const fails = Number(state.consec_fail) || 0;
  if (fails >= 2) return ["down", "#f66"];
  if (fails === 1) return ["degraded", "#fc6"];
  return ["healthy", "#6f6"];
}

function statsSummary(doc) {
  if (!doc || !doc.stats) return null;
  const s = doc.stats;
  const reqs = Number(s.requests) || 0;
  const errs = Number(s.errors) || 0;
  // Notebook /stats reports avg_ms directly; older shape used total_ms/requests.
  const direct = Number(s.avg_ms);
  const total = Number(s.total_ms) || 0;
  const avg = Number.isFinite(direct) ? Math.round(direct * 10) / 10
    : reqs > 0 ? Math.round((total / reqs) * 10) / 10 : null;
  return { requests: reqs, errors: errs, avg_ms: avg, ts: doc.ts || null, model: doc.model || null };
}

// ---------- routes ----------

async function dashboard(env) {
  const aliases = aliasList(env);
  const cards = [];
  for (const alias of aliases) {
    const state = await getJson(env.LAYA_KV, "laya:state:" + alias, null);
    let doc = null;
    if (state && state.backend_id) {
      doc = await getJson(env.LAYA_KV, "laya:stats:" + state.backend_id, null);
    }
    const sum = statsSummary(doc);
    const [label, color] = healthLabel(state);
    const age = state ? ageH(state.started_utc) : null;
    cards.push(
      "<div class='card'>" +
        "<div class='cardhead'><span class='alias'>" + esc(alias) + "</span>" +
        "<span class='pill' style='color:" + color + ";border-color:" + color + "'>" + esc(label) + "</span></div>" +
        "<div class='row'><span>backend</span><code>" + esc(state ? state.backend_id : "-") + "</code></div>" +
        "<div class='row'><span>age</span><b>" + (age == null ? "-" : esc(age) + " h") + "</b></div>" +
        "<div class='row'><span>started</span><b>" + esc(state && state.started_utc ? state.started_utc : "-") + "</b></div>" +
        "<div class='row'><span>last healthy</span><b>" + esc(state && state.last_healthy_utc ? state.last_healthy_utc : "-") + "</b></div>" +
        "<div class='row'><span>requests</span><b>" + (sum ? esc(sum.requests) : "-") + "</b></div>" +
        "<div class='row'><span>errors</span><b>" + (sum ? esc(sum.errors) : "-") + "</b></div>" +
        "<div class='row'><span>avg latency</span><b>" + (sum && sum.avg_ms != null ? esc(sum.avg_ms) + " ms" : "-") + "</b></div>" +
        "</div>"
    );
  }
  const ev = await getJson(env.LAYA_KV, "laya:events", []);
  const rows = (Array.isArray(ev) ? ev.slice().reverse().slice(0, 60) : [])
    .map(
      (e) =>
        "<tr><td class='ts'>" + esc(e.ts) + "</td><td>" + esc(e.alias) + "</td><td><code>" +
        esc(e.kind) + "</code></td><td>" + esc(e.msg) + "</td></tr>"
    )
    .join("");
  const html =
    "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>" +
    "<title>laya dashboard</title><style>" +
    "body{background:#0d1117;color:#c9d1d9;font-family:system-ui,sans-serif;margin:0;padding:24px}" +
    "h1{font-size:20px;margin:0 0 16px}h2{font-size:14px;margin:28px 0 10px;color:#8b949e}" +
    ".cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}" +
    ".card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px 16px}" +
    ".cardhead{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}" +
    ".alias{font-size:16px;font-weight:600}" +
    ".pill{font-size:11px;border:1px solid;border-radius:20px;padding:2px 10px}" +
    ".row{display:flex;justify-content:space-between;font-size:13px;padding:3px 0;color:#8b949e}" +
    ".row b,.row code{color:#c9d1d9;font-weight:500}" +
    "code{font-family:ui-monospace,monospace;font-size:12px}" +
    "table{width:100%;border-collapse:collapse;font-size:12px}" +
    "th{text-align:left;color:#8b949e;font-weight:500;padding:6px 8px;border-bottom:1px solid #30363d}" +
    "td{padding:6px 8px;border-bottom:1px solid #21262d;vertical-align:top}" +
    ".ts{white-space:nowrap;color:#8b949e}" +
    "a{color:#58a6ff;font-size:13px}" +
    "</style></head><body>" +
    "<h1>laya dashboard</h1>" +
    "<div class='cards'>" + cards.join("") + "</div>" +
    "<h2>events <a href='/api/breakdown'>(json)</a></h2>" +
    "<table><tr><th>time</th><th>alias</th><th>kind</th><th>detail</th></tr>" + rows + "</table>" +
    "</body></html>";
  return new Response(html, { headers: { "Content-Type": "text/html; charset=utf-8" } });
}

async function breakdown(env) {
  const out = { aliases: {}, events: [] };
  for (const alias of aliasList(env)) {
    const state = await getJson(env.LAYA_KV, "laya:state:" + alias, null);
    let stats = null;
    if (state && state.backend_id) {
      stats = await getJson(env.LAYA_KV, "laya:stats:" + state.backend_id, null);
    }
    out.aliases[alias] = {
      backend_id: state ? state.backend_id || null : null,
      started_utc: state ? state.started_utc || null : null,
      age_h: state ? ageH(state.started_utc) : null,
      consec_fail: state ? Number(state.consec_fail) || 0 : null,
      last_healthy_utc: state ? state.last_healthy_utc || null : null,
      stats: stats,
    };
  }
  const ev = await getJson(env.LAYA_KV, "laya:events", []);
  out.events = Array.isArray(ev) ? ev.slice().reverse() : [];
  return Response.json(out);
}

async function ingest(req, env) {
  if (!authed(req, env.INGEST_SECRET)) {
    return new Response("unauthorized\n", { status: 401 });
  }
  let body;
  try {
    body = await req.json();
  } catch (_) {
    return new Response("bad json\n", { status: 400 });
  }
  const alias = String(body.alias || "");
  if (!aliasList(env).includes(alias)) {
    return Response.json({ error: "unknown alias" }, { status: 400 });
  }
  const backendId = String(body.backend_id || "");
  if (!ID_RE.test(backendId)) {
    return Response.json({ error: "bad backend_id" }, { status: 400 });
  }
  const doc = {
    ts: body.ts || Date.now(),
    alias: alias,
    model: String(body.model || alias),
    stats: body.stats || null,
  };
  await env.LAYA_KV.put("laya:stats:" + backendId, JSON.stringify(doc));
  const event = String(body.event || "stats");
  if (event !== "stats") {
    const s = doc.stats || {};
    const msg =
      s.requests != null ? "requests=" + s.requests + " errors=" + (s.errors || 0) : "";
    await logEvent(env, alias, event, msg);
  }
  return Response.json({ ok: true });
}

async function routeSystemone(req, env, alias) {
  if (!authed(req, env.ROUTE_SECRET)) {
    return new Response("unauthorized\n", { status: 401 });
  }
  if (!aliasList(env).includes(alias)) {
    return Response.json({ error: "unknown alias" }, { status: 400 });
  }
  let raw;
  try {
    raw = await req.arrayBuffer();
  } catch (_) {
    return Response.json({ error: "bad body" }, { status: 400 });
  }
  try {
    const r = await relayFetch(env, "/t/" + alias + "/v1/systemone", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: raw,
      signal: AbortSignal.timeout(120000),
    });
    const out = await r.arrayBuffer();
    return new Response(out, {
      status: r.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch (_) {
    return Response.json({ error: "backend unreachable" }, { status: 502 });
  }
}

// ---------- rotation keeper ----------

async function backendHealthy(env, backendId) {
  try {
    const r = await relayFetch(env, "/t/" + backendId + "/health", {
      signal: AbortSignal.timeout(25000),
    });
    return r.status === 200;
  } catch (_) {
    return false;
  }
}

async function pushKernel(env, alias, newId) {
  const template = await getJson(env.LAYA_KV, "laya:template:" + alias, null);
  if (!template || !template.text || !template.slug || !template.title) {
    await logEvent(env, alias, "push_failed", "no template in KV");
    return;
  }
  const text = String(template.text).split("__BACKEND_ID__").join(newId);
  const body = {
    id: 0,
    slug: template.slug,
    newTitle: template.title,
    text: text,
    language: "python",
    kernelType: "notebook",
    isPrivate: true,
    enableGpu: false,
    enableTpu: false,
    enableInternet: true,
    datasetDataSources: template.dataset_sources || [],
    kernelDataSources: template.kernel_sources || [],
  };
  try {
    const r = await fetch("https://api.kaggle.com/v1/kernels.KernelsApiService/SaveKernel", {
      method: "POST",
      headers: {
        Authorization: "Bearer " + env.KAGGLE_API_TOKEN,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(120000),
    });
    let j = null;
    try {
      j = await r.json();
    } catch (_) {
      j = null;
    }
    if (r.ok && j && j.versionNumber && !j.error) {
      const nowIso = new Date().toISOString();
      await env.LAYA_KV.put(
        "laya:pending:" + alias,
        JSON.stringify({ backend_id: newId, pushed_utc: nowIso })
      );
      await logEvent(env, alias, "pushed", "version " + j.versionNumber + " backend " + newId);
    } else {
      const msg = (j && (j.error || j.message)) || ("http " + r.status);
      await logEvent(env, alias, "push_failed", String(msg).slice(0, 300));
    }
  } catch (e) {
    await logEvent(env, alias, "push_failed", String((e && e.message) || e).slice(0, 300));
  }
}

async function tickAlias(env, alias) {
  const kv = env.LAYA_KV;
  const stateKey = "laya:state:" + alias;
  const pendingKey = "laya:pending:" + alias;
  const nowIso = new Date().toISOString();

  const state = await getJson(kv, stateKey, null);
  if (!state) {
    await logEvent(env, alias, "no_state", "no rotation state; bootstrap required");
    return;
  }
  const backendId = state.backend_id;
  if (!backendId) {
    await logEvent(env, alias, "bad_state", "state.backend_id missing");
    return;
  }

  const pending = await getJson(kv, pendingKey, null);
  if (pending && pending.backend_id) {
    if (await backendHealthy(env, pending.backend_id)) {
      // flip the stable alias to the new backend
      try {
        const r = await relayFetch(env, "/admin/alias", {
          method: "POST",
          headers: {
            Authorization: "Bearer " + env.RELAY_ADMIN_SECRET,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ alias: alias, backend: pending.backend_id }),
          signal: AbortSignal.timeout(25000),
        });
        if (r.ok) {
          await kv.put(
            stateKey,
            JSON.stringify({
              backend_id: pending.backend_id,
              started_utc: nowIso,
              consec_fail: 0,
              last_healthy_utc: nowIso,
            })
          );
          await kv.delete(pendingKey);
          await logEvent(env, alias, "flip", "alias -> " + pending.backend_id);
        } else {
          await logEvent(env, alias, "flip_failed", "admin status " + r.status);
        }
      } catch (e) {
        await logEvent(env, alias, "flip_failed", String((e && e.message) || e).slice(0, 200));
      }
    } else {
      const pushedMs = Date.parse(pending.pushed_utc || "") || 0;
      if (Date.now() - pushedMs > 45 * 60 * 1000) {
        await kv.delete(pendingKey);
        await logEvent(env, alias, "pending_expired", "pending " + pending.backend_id + " never healthy");
      }
      // else: still booting, wait for next tick
    }
    return;
  }

  const ok = await backendHealthy(env, backendId);
  const consecFail = ok ? 0 : (Number(state.consec_fail) || 0) + 1;
  await kv.put(
    stateKey,
    JSON.stringify({
      backend_id: backendId,
      started_utc: state.started_utc || null,
      consec_fail: consecFail,
      last_healthy_utc: ok ? nowIso : state.last_healthy_utc || null,
    })
  );

  const rotateAfterH = parseFloat(env.ROTATE_AFTER_H || "10.5") || 10.5;
  const age = ageH(state.started_utc);
  if ((age != null && age > rotateAfterH) || consecFail >= 2) {
    const d = new Date();
    const p2 = (n) => String(n).padStart(2, "0");
    const newId =
      alias +
      "-" +
      d.getUTCFullYear() +
      p2(d.getUTCMonth() + 1) +
      p2(d.getUTCDate()) +
      "-" +
      p2(d.getUTCHours()) +
      p2(d.getUTCMinutes());
    if (!ID_RE.test(newId)) {
      await logEvent(env, alias, "push_failed", "bad new backend id " + newId);
      return;
    }
    await pushKernel(env, alias, newId);
  }
}

// ---------- entrypoints ----------

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    const path = url.pathname;

    if (req.method === "GET" && (path === "/" || path === "")) {
      return dashboard(env);
    }
    if (req.method === "GET" && path === "/api/breakdown") {
      return breakdown(env);
    }
    if (req.method === "POST" && path === "/ingest") {
      return ingest(req, env);
    }
    const m = path.match(/^\/r\/([a-zA-Z0-9_-]{1,64})\/v1\/systemone$/);
    if (req.method === "POST" && m) {
      return routeSystemone(req, env, m[1]);
    }
    return new Response("not found\n", { status: 404 });
  },

  async scheduled(event, env, ctx) {
    for (const alias of aliasList(env)) {
      try {
        await tickAlias(env, alias);
      } catch (e) {
        await logEvent(env, alias, "keeper_error", String((e && e.stack) || e).slice(0, 300));
      }
    }
  },
};

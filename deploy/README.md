# Laya deployment

Serving infrastructure for the Laya decision engine.

## `worker/` — Cloudflare Worker

Dedicated `laya` Worker (not shared with other projects):

- `worker.mjs` — dashboard (`GET /`), per-backend breakdown (`GET /api/breakdown`),
  authenticated telemetry ingest (`POST /ingest`), structured inference route
  (`POST /r/<alias>/v1/systemone`), and the scheduled rotation/health handler.
- `meta.json` — Worker metadata (KV binding, secrets, service bindings, cron).

Live at `https://laya.ediprnm-keen.workers.dev/`. KV namespace `laya`.
Aliases: `laya-orig`, `laya-finetuned`.

The worker is also the **rotation watcher**: its cron checks backend age every
15 minutes. At 11h it pushes fresh Kaggle kernels; when the replacements are
healthy it flips the aliases. Old kernels self-stop at 11.5h, so steady state
is 2 instances, 4 during the ~30-minute handover.

## `kaggle/` — Kaggle serving

- `build-serve.py` — builds the `laya-serve` notebooks (orig + finetuned) from
  templates: model snapshot, relay wiring, telemetry with browser UA.
- `keeper.py` — VM-side keeper (transitional; kept until worker-native rotation
  is proven end-to-end, then retired).
- `template-orig.json`, `template-ft.json` — kernel templates the worker uses
  when pushing fresh backends during rotation.

Notebook slugs: `kinan21/laya-serve-orig`, `kinan21/laya-serve-finetuned`.
Kaggle CPU session cap is 5; steady state uses 2, rotation briefly uses 4.

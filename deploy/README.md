# Laya deployment

Serving infrastructure for the Laya decision engine.

## Primary model

**`laya-multilingual` (fine-tuned)** — selected 2026-09-29 from A/B testing
three variants on 606 Hyperliquid mainnet cases. Wins on accuracy (52.5%
overall), calibration (Brier 0.562), and speed (73ms vs 139ms for base —
~1.9x faster). See `../finetune/README.md` for full results.

Checkpoint: Kaggle kernel `kinan21/laya-hl-finetune-multilingual-v1` output
(`laya_hl_small/`). Serving kernels pull it via `kernel_sources`.

## `worker/` — Cloudflare Worker

Dedicated `laya` Worker (not shared with other projects):

- `worker.mjs` — dashboard (`GET /`), per-backend breakdown (`GET /api/breakdown`),
  authenticated telemetry ingest (`POST /ingest`), structured inference route
  (`POST /r/<alias>/v1/systemone`), and the scheduled rotation/health handler.
- `meta.json` — Worker metadata (KV binding, secrets, service bindings, cron).

Live at `https://laya.ediprnm-keen.workers.dev/`. KV namespace `laya`.

**Serving aliases:**
- `kaggle-laya-cpu` — primary public endpoint, serves the fine-tuned
  multilingual model. Public URL:
  `https://tunnel-relay.ediprnm-keen.workers.dev/t/kaggle-laya-cpu/v1/systemone`
- `laya-orig`, `laya-finetuned` — legacy aliases (pre-A/B test naming).

The worker is also the **rotation watcher**: its cron checks backend age every
15 minutes. At 11h it pushes fresh Kaggle kernels; when the replacements are
healthy it flips the aliases. Old kernels self-stop at 11.5h, so steady state
is 2 instances, 4 during the ~30-minute handover.

**Rotation design (autonomous, no VM dependency):**
- Worker holds the Kaggle API token and pushes kernels itself.
- At 11h: launch replacements → 4 instances temporarily.
- After replacements healthy: flip aliases, verify traffic.
- At 11.5h: old kernels stop naturally (never killed).

## `kaggle/` — Kaggle serving

- `build-serve.py` / `build-serve-nb.py` — builds the `laya-serve` notebooks
  from templates: model snapshot, relay wiring, telemetry with browser UA.
  The `ft` (finetuned) config pulls the multilingual checkpoint via
  `kernel_sources: ["kinan21/laya-hl-finetune-multilingual-v1"]`.
- `keeper.py` — VM-side keeper (transitional; kept until worker-native rotation
  is proven end-to-end, then retired).
- `template-orig.json`, `template-ft.json` — kernel templates the worker uses
  when pushing fresh backends during rotation.

Notebook slugs: `kinan21/laya-serve-orig`, `kinan21/laya-serve-finetuned`.
Kaggle CPU session cap is 5; steady state uses 2, rotation briefly uses 4.

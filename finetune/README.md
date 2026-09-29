# Laya fine-tuning — Hyperliquid perps microstructure

Trial run: **2026-09-29** (Kaggle 2xT4).

## A/B test: base vs multilingual vs typed-decisions

Three Laya variants fine-tuned on the same 606-case dataset, same training
loop, same validation. Goal: find the best speed/accuracy tradeoff for serving.

| Variant | Params | Base checkpoint |
|---|---|---|
| `laya` (base) | 421M | `convaiinnovations/laya` |
| `laya-multilingual` | 322M | `convaiinnovations/laya-multilingual` |
| `laya-typed-decisions` | 421M | `convaiinnovations/laya-typed-decisions` |

### Results (122 val cases, chronological last 20%)

**Fine-tuned models compared:**

| Metric | Base laya FT | Multilingual FT | Typed-decisions FT |
|---|---|---|---|
| Bias acc | 50.82% | **54.92%** | 47.54% |
| Intent acc | 48.36% | **59.84%** | 48.36% |
| Leverage acc | 40.16% | **42.62%** | 41.80% |
| Overall acc | 46.45% | **52.46%** | 45.90% |
| Overall Brier | 0.5692 | **0.5619** | 0.5694 |
| Inference latency | 139.2 ms | **73.1 ms** | 135.6 ms |

**Multilingual wins on every metric.** ~1.9x faster than base (73ms vs 139ms),
+6.0pp overall accuracy, better calibration.

**Fine-tuning effect per variant (zero-shot → fine-tuned):**

| Variant | Overall acc Δ | Brier Δ | Verdict |
|---|---|---|---|
| Base laya | 48.09% → 46.45% (-1.64) | 0.6312 → 0.5692 | Accuracy down, calibration up |
| Multilingual | 47.54% → 52.46% (+4.92) | 0.6429 → 0.5619 | Clear win on both |
| Typed-decisions | 45.90% → 45.90% (+0.00) | 0.5617 → 0.5694 | No gain, Brier slightly worse |

### Primary model: `laya-multilingual` (fine-tuned)

Selected 2026-09-29 based on the A/B results above. Currently serving via
the Kaggle CPU rotation (see `../deploy/`).

Kaggle kernels:
- `kinan21/laya-hl-finetune-multilingual-v1` (v7) — training + eval
- `kinan21/laya-hl-finetune-typed-decisions-v1` (v8) — training + eval

## Dataset

606 labeled cases from Hyperliquid **mainnet** recorder states (testnet books are
fake — useless for training). Subsampled ~1/min, rendered with the exact live
serving template/questions (flat position only).

- `train.jsonl` — 484 cases (chronological first 80%)
- `val.jsonl` — 122 cases (chronological last 20%)

Each case: `{state, questions, gold}` — state is decision-time text (book,
momentum, RSI); 3 typed-decision questions (`bias` {long, short},
`intent` {open, hold}, `leverage` {1x, 2x, 3x}); gold is soft probability
distributions. Labels from 5-min forward mid move vs hurdle
(spread + 2× taker fee), soft-ramped over 20bps.

Dataset: Kaggle `kinan21/laya-hl-trial-v1` (also in consumer repo
jev-trade `data/laya-hl-trial/`); labeling script `label-laya-data.py` there.

## Training

Built on the upstream `notebooks/laya_finetune_typed_decisions_2xT4_kaggle.ipynb`
pattern. 1,307 train items + 145 calibration holdout, 4 epochs, 2xT4 DDP.

## Evaluation honesty

Mixed/null hard-label accuracy with improved calibration — **not evidence of
trading edge**. Label imbalance warning: the sample day trended (40/42 long,
39/42 open). Needs multi-day/multi-regime data + class balancing before the
model learns anything beyond "always long".

Per-question accuracy, Brier/calibration, and inference latency are the
selection criteria. Paper PnL and smoke tests do not prove edge.

## Weights

Too large for Git. Checkpoints live on Kaggle kernel output; the serving
notebooks in `../deploy/kaggle/` pull them via `kernel_sources` at boot.

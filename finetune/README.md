# Laya fine-tuning — Hyperliquid perps microstructure

Trial run: **2026-09-29** (Kaggle 2xT4).

## Base model

`convaiinnovations/laya` (ModernBERT-large, 421M params, English).

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

Dataset lives in the consumer repo (jev-trade `data/laya-hl-trial/`); the
labeling script is `label-laya-data.py` there.

## Training

`laya-hl-small-trial-v5.ipynb` — the exact Kaggle notebook that ran (v5).
Built on the upstream `notebooks/laya_finetune_typed_decisions_2xT4_kaggle.ipynb`
pattern. 1,307 train items + 145 calibration holdout.

## Validation (122 cases, chronological)

| | Bias acc | Intent acc | Leverage acc | Overall acc | Overall Brier |
|---|---|---|---|---|---|
| Base | 54.92% | 44.26% | 45.08% | 48.09% | 0.6312 |
| Fine-tuned | 50.82% | 48.36% | 40.16% | 46.45% | 0.5692 |

Mixed/null hard-label accuracy with improved calibration — **not evidence of
trading edge**. Label imbalance warning: the sample day trended (40/42 long,
39/42 open). Needs multi-day/multi-regime data + class balancing before the
model learns anything beyond "always long".

## Weights

Too large for Git (421M). Checkpoints live on Kaggle/HF; the serving
notebooks in `../deploy/kaggle/` pull them at boot.

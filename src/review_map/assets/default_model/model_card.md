# Review Map default risk model

The model that ships with Review Map and is used when a repo has no model of its own
(`review-map train` makes one in `.reviewmap/model/`).

## What it is

- **Type:** LightGBM, 23 trees, 38 features (`features.json`). Review Map runs it in
  plain Python (`risk/lgb_text.py`), so LightGBM doesn't need to be installed to use it.
- **Predicts:** the chance that a changed function will later be changed by a bug-fix
  commit (function-level SZZ labels, which are noisy).
- **Trained on:** the history of psf/requests, pallets/flask and pallets/click: 13,166
  changed functions (1,039 later fixed). Validated on the next 2,881 rows, tested on the
  newest 3,067. It is exactly the model evaluated in `reports/eval.md` (not refitted
  afterwards; reports/DECISIONS.md D24).
- **Inputs:** change size, function size and kind, files in the change, whether tests
  changed, whether the change is itself a fix, the Phase 3 rule flags, and the file's
  history before the change (age, earlier changes, earlier fixes, authors).

## How Review Map uses it

- **Levels, not percentages.** "High" is the top 2% of the model's validation scores
  (probability ≥ 0.121), "medium" the top 10% (≥ 0.079). The probabilities themselves are
  never shown because calibration failed (below).
- **Reading order:** model risk per changed line (chosen on validation). "High" sections
  are always read first, whatever their size (D23, D25).
- **Rule reasons stay.** Every flagged section also shows the rule reasons, so you can see
  what in the code looks risky. The model only adds "changes like this were often fixed".

## How well it works (test data, newest 15% of each repo)

| Ranking | Later-fixed changes read at 20% of lines |
|---|---:|
| model ÷ lines (used) | 53.7% |
| smallest change first | 53.2% |
| random | 21.0% |

- It **ties** the simple "smallest change first" rule. Per repo it's better on requests,
  worse on flask and about equal on click.
- Per function: PR-AUC 0.082 and ROC-AUC 0.639, close to "largest change first" (0.080,
  0.600).
- **Calibration: not passed** (ECE 0.023 vs a target of 0.02; Brier skill +2.3%). So
  Review Map shows no "risk coverage" estimate with this model.
- The reading-plan backtest is in `reports/backtest.md`.

## Limits

- Labels miss bugs fixed only by adding code, and may blame refactors bundled with fixes.
- Trained on three mature Python libraries. Other languages and kinds of project may
  behave differently.
- History is per path, so renames reset it.
- **It is not a bug detector.** A low level never means a change is correct.

Files: `model.txt` (LightGBM text format), `features.json`, `calibration.json` (isotonic),
`metrics.json` (every evaluation number, as in `reports/metrics.json`). SHA-256 of
`model.txt`: `31837905086818abdada05824f70016b06d8040efdd004c9a823bf1e594b2bda`.

## Summary in plain language

**The question.** If a reviewer reads only the parts of a change that Review Map ranks highest, using a fixed share of the changed lines, how many of the changes that later needed a bug fix do they read? We measured this on the history of three Python projects (requests, flask, click), training on older changes and testing on the newest 15%, which the model never saw.

**What works.**
- Ranking by **risk per line** (the model's probability ÷ lines changed) reads **53.7%** of the later-fixed changes while reading only **20%** of the changed lines. Picking at random reads 21.0%. Validation data (never test) chose this ranking.
- The model picks up real signals: how many files the commit touched, how old the file is, how big the change is, the file's past changes and authors.

**What doesn't.**
- That 53.7% is **no better than a much simpler rule**: "read the smallest changes first" gets **53.2%**. The difference is within noise (the test set has 155 later-fixed changes, so one change is about 0.6 points). Per project it's mixed: better on requests (83.6% vs 67.2%), worse on flask (34.5% vs 41.4%), about equal on click (42.9% vs 41.1%).
- Ranked **per function** (most likely to need a fix first), the model's PR-AUC is 0.082, the same as "largest change first" (0.080). Its ROC-AUC is 0.639 vs 0.600. Trained on requests + flask and tested on click, "largest change first" beats it (PR-AUC 0.171 vs 0.153).
- The Phase 3 hand-written rules are only slightly better than random at ranking functions (ROC-AUC 0.551).
- **Calibration did not pass.** The predicted chances are off by 2.3 points on average (target ≤ 2), and the model is only 2.3% better than always predicting the base rate (Brier skill). So Phase 11 will **not** show the "risk coverage" estimate with this model.
- The hybrid ("high-risk first, whatever the size"), with "high" meaning the top 10% of scores, does about as badly as the per-function ranking on this metric (11.2% at 20% of lines). High-risk changes tend to be large and use up the budget. On average 185 of the 306 high-risk changes didn't fit the 20% budget.

**Why small changes win this metric.** Bigger changes are more likely to need a fix, but not in proportion to their size. A 100-line change is not 10 times as likely to be buggy as a 10-line one. The metric counts *changes caught per line read*, so many small changes beat a few big ones. This is a known effect in defect-prediction research. It also means this metric undervalues one big risky function, which is exactly the case the hybrid rule protects in the product.

**What a user should expect.**
- Review Map's reading plan is a reasonable way to spend limited review time. But today its *measured* advantage comes mostly from ordering by size and risk-per-line, not from deep insight into which code is buggy.
- The model is **not** a bug detector, and a low score never means a change is correct.
- The scope check (description vs. diff) is separate from this model and isn't measured here.

**Changes made after the first results.** These are listed below and in `reports/DECISIONS.md`, with before/after numbers. They are:
- a 180-day gap before the test period, so training labels have time to mature
- the reading plan's own "skip what doesn't fit" budget rule for every method
- three new rankings: model ÷ lines, rules ÷ lines, and the hybrid

None of them made the model beat "smallest change first".

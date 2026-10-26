# Methodology decisions

A dated log of every methodology decision behind Review Map's dataset and model, from
Phase 9 onward. Each entry says when it was made **relative to the test results**:

- **Before results**: decided before any test number existed.
- **After seeing data**: decided after looking at the mined data or validation numbers, but
  before any test number existed.
- **After test results**: decided after test numbers were seen. These can flatter the final
  numbers, so each one gives its evidence and before/after effect. `review-map evaluate`
  re-runs the "before" configurations on every run (see "Changes after first results" in
  `reports/eval.md`).

All numbers are from `reports/metrics.json` unless stated otherwise. "Test" is the newest
15% of each repo (3,067 rows, 155 risky). "R@20%" is recall at 20% of changed lines.

---

## Phase 9: mining the dataset

### D1. Training libraries as an optional extra
- **Date / phase:** 2026-10-24, Phase 9. **Timing:** before results.
- **Decision:** `pandas`, `pyarrow` and `pydriller` (later also `lightgbm` and `scikit-learn`)
  are an optional `train` extra. The `dev` group includes it, so the tests always have it.
- **Why:** CLI and Action users don't need them, and pyarrow alone is about 40 MB.
- **Options:** required dependencies; optional extra (chosen).
- **Effect:** none on the numbers.

### D2. Bug-fix detection by subject-line keywords
- **Date / phase:** 2026-10-24, Phase 9. **Timing:** before results.
- **Decision:** a commit is a fix if its *subject line* matches fix / fixes / fixed / bug /
  bugfix / regression / revert / hotfix, and none of typo / docs / readme / changelog /
  style / formatting / lint / flake8 / pep8 / ci. Commit bodies are ignored.
- **Why:** this is the standard SZZ approach. Bodies often say "fixes" about other things.
- **Options:** subject + body; subject only (chosen); issue-tracker labels (not available offline).
- **Effect:** 521 / 446 / 279 fix commits in requests / flask / click.

### D3. Only non-test source code can be labelled risky
- **Date / phase:** 2026-10-24, Phase 9. **Timing:** before results.
- **Decision:** rows and blame targets are Python/JS/TS source outside test paths.
  "Did tests change" is kept as a feature instead.
- **Why:** a fix that edits a test usually means the test changed, not that it caused the bug.
- **Options:** all files; source only (chosen).

### D4. Docs folders excluded too
- **Date / phase:** 2026-10-24, Phase 9. **Timing:** after seeing data (before any test results).
- **Decision:** `docs/` and `doc/` are not labelable.
- **Why:** a spot check of 6 random risky rows found `docs/_static/native.js`, bundled ad
  code, labelled risky. D3's intent was "not docs" from the start.
- **Effect:** requests 8,172 → 8,113 rows, 632 → 619 risky.

### D5. SZZ noise filters
- **Date / phase:** 2026-10-24, Phase 9. **Timing:** before results.
- **Decision:**
  - Skip merge commits, root commits, and commits over 50 files.
  - Don't blame blank, punctuation-only, comment-only or merely re-spaced lines.
  - Run `git blame -w -M`, ignoring the over-50-file commits (`--ignore-revs-file`).
- **Why:** these are the common SZZ refinements. Big commits are mostly reformats or vendoring.
- **Options considered:** `-C` (cross-file moves); rejected for speed.

### D6. Recent rows flagged, not dropped
- **Date / phase:** 2026-10-24, Phase 9. **Timing:** before results.
- **Decision:** rows within 180 days of the repo's newest commit get `too_recent = true`
  and stay in the dataset. Phase 10 decides how to use them (see D11).
- **Why:** a recent change's "0" means "unknown", not "safe".

### D7. Data corrections found by running on real history
- **Date / phase:** 2026-10-24, Phase 9. **Timing:** after seeing data (before any test results).
  These were bug fixes, not choices:
  1. Git output is decoded from bytes without newline translation. A lone `\r` in an
     Illustrator file broke diff parsing, and it would have broken `analyze` too.
  2. The ignore-revs path is absolute. The relative path made every blame fail
     silently: 521 fixes and 0 risky rows. Blame failures are now counted and warned about.
- **Effect:** the dataset went from wrong (0 risky) to 632 risky rows in requests.

## Phase 10: features, training, evaluation

### D8. Re-mine to add the Phase 3 rule features
- **Date / phase:** 2026-10-24, Phase 10. **Timing:** before results.
- **Decision:** compute the rule score and rule flags during mining (checkpoint version 2),
  plus a commit table of every file each commit touched, for the history features.
- **Why:** the rules need the changed code text, which the dataset didn't keep.
- **Effect:** requests and flask rows and labels came out identical to before.

### D9. A third repo for the cross-repo test: pallets/click
- **Date / phase:** 2026-10-24, Phase 10. **Timing:** before results.
- **Decision:** add pallets/click and use it as the cross-repo holdout (train on requests + flask).
- **Why:** it's a different domain (CLI) from requests and flask (HTTP / web), so it's a
  harder test than encode/httpx would be.
- **Options:** pallets/click (chosen); encode/httpx; test requests ↔ flask both ways.
- **Effect:** +5,593 rows (455 risky, 8.1%).

### D10. Realistic labels
- **Date / phase:** 2026-10-24, Phase 10. **Timing:** before results.
- **Decision:** training and validation rows count a fix only if it happened before the
  test period starts. Test rows use every known fix.
- **Why:** that's what a user training on that day would know.

### D11. Rows from each repo's last 180 days are left out
- **Date / phase:** 2026-10-24, Phase 10. **Timing:** before results.
- **Decision:** `too_recent` rows (940) are used in neither training nor testing.
- **Why:** their outcome isn't known yet (D6).

### D12. 70 / 15 / 15 time split per repo, whole commits
- **Date / phase:** 2026-10-24, Phase 10. **Timing:** before results.
- **Decision:** oldest 70% train, next 15% validation, newest 15% test, split by date per
  repo. A commit is never split. (D17 later added a gap before the test period.)
- **Why:** no shuffling means no seeing the future. Splitting per repo keeps every repo
  in every part.

### D13. Calibration pass rule
- **Date / phase:** 2026-10-24, Phase 10. **Timing:** before results.
- **Decision:** passed = ECE ≤ 0.02 **and** Brier skill > 0 on test. If it isn't reached,
  the phase does not fail: the real numbers are reported and explained. Phase 11's
  risk-coverage estimate is only shown for a model that passed.

### D14. Where `review-map train` saves a model
- **Date / phase:** 2026-10-24, Phase 10. **Timing:** before results.
- **Decision:** `REPO/.reviewmap/model/`, so a team can commit it and the Action can use it.
  The research model goes to `data/models/general/`.

### D15. Fixed hyperparameters; choices made on validation only
- **Date / phase:** 2026-10-24, Phase 10. **Timing:** before results.
- **Decision:**
  - LightGBM parameters are fixed in advance (15 leaves, learning rate 0.05, min 40 rows
    per leaf, single-threaded, deterministic).
  - Positive-class weighting (none vs. negatives ÷ positives), the number of trees
    (early stopping) and the calibrator (none / Platt / isotonic, by 5-fold CV Brier score
    *within* validation) are chosen on validation.
- **Why:** with this little data, tuning invites overfitting the validation set too.

### D16. Baselines and metric details
- **Date / phase:** 2026-10-24, Phase 10. **Timing:** before results.
- **Decision:**
  - Baselines: random (20 seeds), largest change first, smallest change first, and the
    Phase 3 rule score.
  - Recall @10/20/30% of lines with random tie-breaking, averaged over 10 seeds.
  - ECE over 10 equal-count bins.
- **Why:** "smallest change first" was included from the start because it's known to be
  strong on effort-aware metrics.

### D17. 180-day embargo before the test period
- **Date / phase:** 2026-10-24, Phase 10. **Timing:** after test results.
- **Decision:** training and validation only use rows at least 180 days older than the
  test period. The rows in between (882) are used nowhere.
- **Why (evidence from the first run):**
  - The calibration table under-predicted throughout: predicted 1.1–7.3%, observed 0.7–12.7%.
  - Early stopping kept only 7 trees.
  - Cause: with realistic labels (D10), validation rows just before the test period hadn't
    had time for their fixes to arrive (in requests, median days to fix about 61, 90th percentile about 940).
- **Options:** keep as is and report; embargo (chosen, by the user).
- **Effect** (model per function, test):

  | Metric | Before | After |
  |---|---:|---:|
  | Trees | 7 | 23 |
  | PR-AUC | 0.113 | 0.082 |
  | ROC-AUC | 0.688 | 0.639 |
  | R@20% | 11.0% | 11.2% |
  | ECE | 0.0234 | 0.0231 |
  | Brier skill | +2.32% | +2.27% |

  **It did not solve the problem.** Validation's risky rate is still 3.9% vs 7.9% in
  training, and calibration still fails. Per-function ranking got *worse*: either the
  embargo removed the rows most like the test period (drift), or this is noise.

### D18. One budget rule for every method: the reading plan's "skip what doesn't fit"
- **Date / phase:** 2026-10-24, Phase 10. **Timing:** after test results.
- **Decision:** recall @X% picks rows in ranked order and skips any row too big for the
  remaining budget, exactly like the reading plan (Phase 6). Before, it stopped at the
  first row that didn't fit.
- **Why:** the hybrid (D20) must not be blocked by one big high-risk row, and the metric
  should measure the product's real behaviour. Applied to every method alike.
- **Effect** (test R@20%):

  | Method | Before | After |
  |---|---:|---:|
  | model per function | 11.2% | 11.2% |
  | rules (Phase 3) | 9.4% | 17.7% |
  | random | 20.3% | 21.0% |
  | largest change first | 0.0% | 0.1% |
  | smallest change first | 53.2% | 53.2% |

  The "before" values for rules, random and largest change are from the first run's
  printed table. The test rows are the same in every configuration.

### D19. New rankings: model ÷ lines and rules ÷ lines
- **Date / phase:** 2026-10-24, Phase 10. **Timing:** after test results.
- **Decision:** add "model ÷ lines" (probability per changed line) and "rules ÷ lines"
  ((score + 1) ÷ lines, so zero-score rows still order by size). Validation chooses which
  model ranking is used (D21).
- **Why:** the reading-plan budget is in lines, and ranking by risk per line is the
  standard for line budgets (effort-aware defect prediction). The first run showed the
  per-function model reading big changes first and losing to "smallest change first"
  (11.0% vs 53.2%).
- **Options:** keep per function only; add per line, chosen on validation (chosen).
- **Effect (test R@20%):** model ÷ lines **53.7%** vs smallest change first 53.2%, a tie
  within noise. Rules ÷ lines: 41.9%.

### D20. Hybrid ranking: high-risk first whatever the size
- **Date / phase:** 2026-10-24, Phase 10. **Timing:** after test results (the user's design).
- **Decision:**
  - Rows whose probability reaches a "high" cut come first (highest first), whatever
    their size. The rest follow by probability per line.
  - The cut is chosen on validation, from the 80 / 90 / 95 / 98 / 99% quantiles of
    validation scores.
  - High rows that don't fit are counted and reported, never hidden.
- **Why:** per-line ranking can push out one big, genuinely risky function. A reviewer
  would want to know about it.
- **Effect:**
  - Validation chose the 90% quantile (probability ≥ 0.079). Validation R@20% was flat at
    17.7–17.8% across every cut.
  - Test R@20%: **11.2%**, the same as per function. At this cut 10% of rows count as
    "high", and they're mostly large, so they use up the budget. On average 185 of the
    306 high rows didn't fit at 20%.
  - Lesson for Phase 11: "high" has to be rare, and the product also caps the number of
    sections at 5.

### D21. Ranking for Phase 11 chosen on validation
- **Date / phase:** 2026-10-24, Phase 10. **Timing:** after test results (the rule itself
  came with D19 and D20). The choice uses validation numbers only.
- **Decision:** use the model ranking with the best validation R@20%.
- **Effect:** **model ÷ lines** (validation R@20% 48.2%, vs 17.8% per function and 17.8% hybrid).

### D22. Audit trail and reproducibility
- **Date / phase:** 2026-10-24, Phase 10. **Timing:** after test results.
- **Decision:**
  - This log.
  - `reports/eval.md`: plain-language summary; data and per-split label balance; every
    method on validation *and* test; cross-repo; calibration; feature importances;
    "changes after first results"; limitations; reproducibility.
  - `reports/metrics.json`: every raw number.
  - `evaluate` re-runs the "before" configurations each time.
  - Library versions, seeds, repo HEADs, dataset hashes and exact commands are recorded.
- **Effect:** two runs give byte-identical `metrics.json`.

### D23. Reading-plan behaviour for model scores (planned for Phase 11)
- **Date / phase:** 2026-10-24, decided in Phase 10 and built in Phase 11. **Timing:**
  after test results.
- **Decision:** when the model is used, any "high" unit is always included in "Read
  carefully" whatever its size. The validation-chosen ranking only orders and fills the
  rest of the budget. If high units don't all fit, the plan says so ("N more high-risk
  sections didn't fit the budget").
- **Why:** see D20.
- **Effect (measured in Phase 11, reports/backtest.md):** with "high" made rare (D25), only
  1 high section in 666 held-out commits didn't fit. The floor makes the plan read more
  lines: 37.4% vs 23.2% for model ÷ lines without it, for about the same recall (60.6% vs
  60.0%). In the 106 commits where the budget binds, it's 26.5% vs 25.3%.

## Phase 11: shipping the model and the backtest

### D24. Ship the evaluated model as is, not refitted on all data
- **Date / phase:** 2026-10-26, Phase 11. **Timing:** after test results.
- **Decision:** `assets/default_model/` is byte-for-byte the model evaluated in
  `reports/eval.md` (`data/models/general/`, trained on the oldest 70% of each repo).
- **Why:** every number in eval.md and backtest.md then describes exactly the model users
  get. A refit on all data would probably be a little better on new code, but it would be
  unmeasured.
- **Options:** refit on all rows with the same settings; ship as evaluated (chosen).
- **Effect:** none on the numbers.

### D25. Model levels: "high" = top 2%, "medium" = top 10% of validation scores
- **Date / phase:** 2026-10-26, Phase 11. **Timing:** after test results (Phase 10 showed
  that "high" = top 10% fills the budget), but defined before the backtest ran.
- **Decision:** the cuts are the 98% and 90% quantiles of the model's validation scores,
  read from the model's own `metrics.json`, so a repo-trained model gets its own cuts. For
  the shipped model: high ≥ 0.121, medium ≥ 0.079 (calibrated). Probabilities are never
  shown, only levels, because calibration failed (D13).
- **Why:** the plan requires "high" to be rare. The calibrated scores fall on only 8
  isotonic steps, so a step cut is needed. On test, 0.121 marks 2.7% of rows; the 99%
  quantile is the same step, and the next step up (0.364) marks 0.03% (1 row).
- **Effect (backtest):** 84 of 3,067 functions were high (2.7%), and 17.9% of those were
  later fixed vs 5.1% overall. One high section in 666 commits didn't fit 5 sections.

### D26. With the model, every model-scored section can fill the budget
- **Date / phase:** 2026-10-26, Phase 11. **Timing:** after test results (the backtest);
  chosen by the user. Validation agrees.
- **Decision:** after the must-read sections, every section the model scores (low ones
  too) is eligible, ordered by model risk per line. Sections the model doesn't apply to
  (tests, docs, deleted code, the description file) keep Phase 6's gate (priority ≥ 30).
  Out-of-scope violations join "high" in the must-read group (the scope check is the main
  feature; D23 only named high-risk units). Rules-only mode is unchanged.
- **Why (evidence):** with Phase 6's gate (medium or higher only), the plan read almost
  nothing in most commits (0.5 sections per commit):

  | Configuration | Validation: later-fixed read | Test: later-fixed read | Test: lines read |
  |---|---:|---:|---:|
  | medium-or-higher gate (before) | 34.8% | 23.9% | 29.2% |
  | every section eligible (after) | 53.6% | 60.6% | 37.4% |

- **Options:** keep the gate; every section (chosen); gate plus a second "if you have time"
  list.
- **Caveat:** it ties random at the same budget (61.5% at 27.6% of lines). The "before" is
  still measured in every backtest run (row "same, medium-or-higher gate (before D26)").

### D27. No risk-coverage estimate in v0.1
- **Date / phase:** 2026-10-26, Phase 11. **Timing:** after test results.
- **Decision:** the optional per-PR "risk-coverage estimate" is not built. The plan,
  Markdown, HTML and JSON say the model isn't calibrated, and JSON has
  `risk_scorer.calibrated`.
- **Why:** the plan only allows it for a model that passed calibration, and the shipped
  model didn't (D13). A repo-trained model might pass, but nothing in this project has,
  so the code would ship unexercised. Phase 10b can add it.

### D28. Backtest design
- **Date / phase:** 2026-10-26, Phase 11. **Timing:** before the backtest ran.
- **Decision:**
  - Replay each commit of the held-out test period (the same rows as eval.md's test set)
    as one PR.
  - Use the default budget (15% of lines, at least 40, at most 5 sections) and the same
    `select_sections` code as `analyze`.
  - Use the dataset's leak-free features and full labels.
  - Compare at the same budget: rules-only plan, model ÷ lines without the floor,
    smallest first, random (20 seeds).
  - Report "commits where the budget forces a choice" separately, because the 40-line
    floor reads small commits in full.
  - Examples are chosen by a fixed rule (spread over repos, big commits first, high level
    first, soonest fix), not by hand.
- **Why:** to measure the product's real behaviour, not a pooled ranking.
- **Limits:** no descriptions, so no scope check; one commit ≠ one PR; and the test
  period isn't fresh for decisions made after the Phase 10 results.

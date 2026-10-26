## Summary in plain language

**The question.** If each change from the held-out period had been a PR, and the reviewer
read only what Review Map's plan said to "read carefully", how many of the functions that
later needed a bug fix would they have read?

**What we found.**
- The plan (rules + shipped model) read **60.6%** of the later-fixed functions while reading
  **37.4%** of the changed lines. The rules-only plan from Phase 6 read 16.8%.
- That is **no better than simple alternatives at the same budget**: random picking read
  61.5% (27.6% of lines), and "smallest change first" 58.7% (22.3% of lines). Per commit,
  the 40-line floor means a small change is read in full whatever the order, so the
  ranking matters only in big commits.
- In the 106 big commits where the budget forces a choice, the plan read 26.5% of the
  later-fixed functions (26.6% of lines). Random: 28.0% (15.0% of lines). Model ÷ lines
  without the high-risk floor: 25.3% (9.9% of lines). The **high-risk floor costs lines**:
  it adds big sections whatever their size, which is its purpose (D20, D23), but on this
  data it didn't catch more.
- **"High" is rare and meaningful:** 2.7% of functions, and 17.9% of those later needed a
  fix, 3.5× the 5.1% base rate. Only one high section in 666 commits didn't fit the
  5-section budget.
- A change made **after these results** (D26): before it, only medium-or-higher sections
  could fill the budget, and the plan read just 23.9%. It's kept as a row below.

**The examples are real but noisy.** One "bug fix" is a revert of the same day's commit,
and another is a type-checker cleanup. That's SZZ: "later changed by a commit whose
subject says fix" is only a proxy for "had a bug".

**What this means for users.** The reading plan is a sensible way to split limited
review time, and its "high" flag points at genuinely riskier code. But the backtest
doesn't show that its order beats random or size-based reading at catching later-fixed
code. The main differentiator, the scope check against the description, can't be
backtested here, because history has no PR descriptions.

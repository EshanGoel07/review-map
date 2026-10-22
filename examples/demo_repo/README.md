# Review Map demo repo

A tiny notes service with one "AI-made" change to review.

- `base/` is the code on `main`.
- `head/` is the code on the branch `feature/paginate-notes`.
- `changes.md` is the change description. It claims the branch only paginates the notes
  list, and says permissions are unchanged.

The branch also quietly changes `can_delete` so that anyone can delete a shared note.
Review Map should put that change first.

```bash
python examples/demo_repo/build.py /tmp/review-map-demo   # builds a real git repo
review-map analyze --repo /tmp/review-map-demo --base main
review-map analyze --repo /tmp/review-map-demo --base main --format html   # report.html
```

A git repo can't be committed inside another one, so the demo is kept as plain files and
`build.py` creates the repo on demand (standard library and `git` only).

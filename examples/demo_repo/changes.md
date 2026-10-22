# Paginate the notes list

The notes list could return thousands of notes at once. It now returns one page at a time.

- `NoteStore.list_notes` in `notes/store.py` takes `page` and `per_page` (default 20).
- The `list_notes` handler in `notes/api.py` reads `?page=` from the query string.
- New tests for pagination in `tests/test_store.py`.

## Out of scope
- Permissions (`notes/permissions.py`) are unchanged.
- No changes to how notes are stored.

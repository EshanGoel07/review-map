"""Request handlers (framework-free for the demo)."""

from notes.permissions import can_delete
from notes.store import NoteStore


def list_notes(store: NoteStore, user, query: dict) -> dict:
    notes = store.list_notes(user.id)
    return {"notes": [n.text for n in notes]}


def delete_note(store: NoteStore, user, note_id: int) -> dict:
    note = store.notes.get(note_id)
    if note is None:
        return {"status": 404}
    if not can_delete(user, note):
        return {"status": 403}
    store.delete(note_id)
    return {"status": 204}

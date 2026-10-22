from notes.store import Note, NoteStore


def test_list_shows_own_and_shared_notes():
    store = NoteStore()
    store.add(Note(1, owner_id=1, text="mine"))
    store.add(Note(2, owner_id=2, text="shared", shared=True))
    store.add(Note(3, owner_id=2, text="private"))
    assert [n.text for n in store.list_notes(1)] == ["mine", "shared"]

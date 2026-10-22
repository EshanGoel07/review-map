"""In-memory note storage."""

from dataclasses import dataclass, field


@dataclass
class Note:
    id: int
    owner_id: int
    text: str
    shared: bool = False


@dataclass
class NoteStore:
    notes: dict[int, Note] = field(default_factory=dict)

    def add(self, note: Note) -> None:
        self.notes[note.id] = note

    def list_notes(self, user_id: int, page: int = 1, per_page: int = 20) -> list[Note]:
        visible = [n for n in self.notes.values() if n.owner_id == user_id or n.shared]
        start = (page - 1) * per_page
        return visible[start : start + per_page]

    def delete(self, note_id: int) -> None:
        del self.notes[note_id]

"""Who may do what with a note."""


def can_view(user, note) -> bool:
    return note.shared or note.owner_id == user.id or user.is_admin


def can_delete(user, note) -> bool:
    if user.is_admin or note.shared:
        return True
    return note.owner_id == user.id

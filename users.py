"""
Shared user name cache — populated at bot startup from config/users.yaml
or the Users sheet. Import get_name() anywhere to resolve a user_id → display name.
"""

_NAMES: dict[int, str] = {}


def set_names(names: dict[int, str]) -> None:
    _NAMES.clear()
    _NAMES.update(names)


def update_names(names: dict[int, str]) -> None:
    _NAMES.update(names)


def get_name(user_id, fallback: str = "") -> str:
    try:
        return _NAMES.get(int(user_id), "") or fallback
    except (ValueError, TypeError):
        return fallback


def get_all() -> dict[int, str]:
    return dict(_NAMES)

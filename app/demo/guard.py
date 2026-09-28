"""Защита витрины: гость не может сломать демо-входы для следующего гостя."""

from __future__ import annotations

from typing import Any

from app.demo.accounts import demo_enabled, protected_emails
from app.models.user import User

# Объясняем только в момент, когда ограничение сработало, и без слова «демо».
PROTECTED_NOTICE = "Пароль, роль и доступ этого сотрудника изменить нельзя. Остальное сохранено"


def is_protected(user: User | None) -> bool:
    return bool(demo_enabled() and user is not None and user.email in protected_emails())


def sanitize_user_edit(edited_user: User, values: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Для демо-сотрудников оставляем прежние пароль, роль и активность. Возвращает (форма, было_ли_ограничение)."""
    if not is_protected(edited_user):
        return values, False
    wanted_active = str(values.get("is_active") or "").lower() in {"1", "true", "on", "yes"}
    blocked = bool(
        str(values.get("password") or "").strip()
        or str(values.get("role") or edited_user.role.value) != edited_user.role.value
        or wanted_active != edited_user.is_active
    )
    safe = dict(values)
    safe["password"] = ""
    safe["role"] = edited_user.role.value
    safe["is_active"] = "on" if edited_user.is_active else ""
    return safe, blocked

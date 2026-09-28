"""Версия демо-мира и открытые вкладки после пересборки.

Мир пересобирается сам (06:00, тихая пересборка, потолок 6 часов) и по кнопке «Начать заново». После пересборки
номера заявок и id строк начинаются заново, поэтому вкладка, открытая в прежнем мире, не должна ни падать с
ошибкой, ни менять чужую заявку с тем же номером. Сессия помнит версию мира, которую видел посетитель; если мир
с тех пор сменился:
- переход по странице ведёт на стартовый экран роли с подписью «Данные обновлены»;
- изменение (форма) не применяется и ведёт туда же; запрос из скрипта получает 409, и скрипт отправляет форму
  обычным путём — тоже на стартовый экран;
- чтения из скриптов (поиск, подсказки) проходят как есть;
- мир пересобрали, пока запрос уже шёл (заявка ссылалась на клиента прежнего мира — база отказала): вместо
  страницы ошибки — тот же стартовый экран с «Данные обновлены».

Здесь же — время последнего действия посетителя: потолок пересборки ждёт 5 минут тишины (app/demo/reset.py).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.demo.accounts import demo_enabled, landing_path
from app.ui.flash import add_flash
from app.ui.prefetch import is_prefetch

logger = logging.getLogger("app.demo")

SESSION_KEY = "world"
DATA_REFRESHED = "Данные обновлены"
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Служебные адреса: не страницы посетителя и не его действия.
SKIP_PREFIXES = ("/static/", "/demo/", "/login", "/logout", "/health", "/robots.txt", "/favicon.ico", "/manifest",
                 "/sw.js", "/offline")


class _World:
    """Версия мира (время сборки демо-владельца) и последнее действие посетителя — в памяти процесса."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.version = ""
        self.last_activity: datetime | None = None

    def set_version(self, value: str) -> None:
        with self._lock:
            self.version = value or ""

    def touch(self, moment: datetime) -> None:
        with self._lock:
            if self.last_activity is None or moment > self.last_activity:
                self.last_activity = moment


world = _World()


def remember_world(session: dict) -> None:
    """Посетитель видит текущий мир: вход, «Начать заново»."""
    if world.version:
        session[SESSION_KEY] = world.version


class FreshWorldMiddleware:
    """Внутри SessionMiddleware: у запроса уже есть сессия."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not demo_enabled() or scope["path"].startswith(SKIP_PREFIXES):
            await self.app(scope, receive, send)
            return
        session = scope.get("session")
        if session is None or not session.get("user_id") or is_prefetch(scope):
            # Предзагрузка ссылок — не переход посетителя: ни отметки, ни подписи.
            await self.app(scope, receive, send)
            return

        from app.utils.dates import CRM_TIMEZONE

        world.touch(datetime.now(CRM_TIMEZONE))
        current = world.version
        seen = session.get(SESSION_KEY)
        if not current or seen == current:
            await self._guarded(scope, receive, send, session, current)
            return
        if seen is None:
            # Сессия старше этой отметки (или вход без неё): запоминаем мир и ничего не меняем.
            session[SESSION_KEY] = current
            await self._guarded(scope, receive, send, session, current)
            return

        method = scope["method"]
        is_fetch = _header(scope, b"x-requested-with") == "fetch"
        if method in MUTATING and is_fetch:
            # Скрипт отправит форму обычным путём и попадёт на стартовый экран (ветка ниже).
            await _json(send, 409, {"ok": False, "message": DATA_REFRESHED, "redirect": "/"})
            return
        if method not in MUTATING and (is_fetch or "text/html" not in (_header(scope, b"accept") or "")):
            await self.app(scope, receive, send)
            return

        target = _landing_for(scope, session)
        session[SESSION_KEY] = current
        add_flash(session, DATA_REFRESHED, "info")
        if method not in MUTATING and scope["path"] == target:
            await self.app(scope, receive, send)
            return
        await _redirect(send, target)

    async def _guarded(self, scope: Scope, receive: Receive, send: Send, session: dict, version: str) -> None:
        """Запрос, во время которого мир пересобрали: ошибку базы превращаем в мягкий возврат на старт."""
        started = False

        async def tracked(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracked)
        except Exception as error:
            if started or not _rebuilt_meanwhile(scope, version, error):
                raise
            logger.info("Запрос %s %s пришёлся на пересборку мира — посетитель на стартовом экране",
                        scope["method"], scope["path"])
            session[SESSION_KEY] = world.version
            if _header(scope, b"x-requested-with") == "fetch":
                await _json(send, 409, {"ok": False, "message": DATA_REFRESHED, "redirect": "/"})
                return
            target = _landing_for(scope, session)
            add_flash(session, DATA_REFRESHED, "info")
            await _redirect(send, target)


def _rebuilt_meanwhile(scope: Scope, version: str, error: Exception) -> bool:
    """Мир сменился, пока шёл запрос: в этом процессе — по версии, в другом — по базе (для ошибок целостности)."""
    if world.version and world.version != version:
        return True
    from sqlalchemy.exc import IntegrityError

    if not isinstance(error, IntegrityError):
        return False
    from app.database import get_db
    from app.demo.reset import demo_epoch

    provider = scope["app"].dependency_overrides.get(get_db, get_db)
    sessions = provider()
    try:
        current = demo_epoch(next(sessions))
    except Exception:
        return False
    finally:
        sessions.close()
    if current and current != version:
        world.set_version(current)
        return True
    return False


def _landing_for(scope: Scope, session: dict) -> str:
    """Стартовый экран роли; посетителя без учётки в новом мире — на вход."""
    from app.database import get_db
    from app.models.user import User

    provider = scope["app"].dependency_overrides.get(get_db, get_db)
    sessions = provider()
    try:
        db = next(sessions)
        user = db.get(User, session.get("user_id"))
        if user is None or not user.is_active:
            session.pop("user_id", None)
            return "/login"
        return landing_path(user.role)
    finally:
        sessions.close()


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return None


async def _redirect(send: Send, location: str) -> None:
    await send({
        "type": "http.response.start",
        "status": 303,
        "headers": [(b"location", location.encode()), (b"cache-control", b"no-store"), (b"content-length", b"0")],
    })
    await send({"type": "http.response.body", "body": b""})


async def _json(send: Send, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"cache-control", b"no-store"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})

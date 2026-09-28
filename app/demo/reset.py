"""Сброс демо: очистка таблиц CRM и живой мир на текущий момент.

Сброс общий для всех гостей. Фоновая проверка раз в минуту решает, пора ли пересобрать мир:
- каждый день в 06:00 по Москве — полностью;
- тихо: мир собран больше часа назад и уже 20 минут никто ничего не менял — запланированное на прошедшие часы
  становится сделанным;
- потолок: мир не старше 6 часов, даже если в витрине всё время кто-то есть. Когда пора, пересборка ждёт
  5 минут тишины (ни изменений, ни открытых страниц), но не дольше 30 минут.
Пересборка идёт одной транзакцией под блокировкой в базе: гость не видит полупустой мир, две пересборки
одновременно не запускаются, а оборванная (перезапуск контейнера) откатывается целиком — остаётся прежний мир.
После неё VACUUM возвращает место удалённых строк: база не растёт от пересборок. Открытые вкладки прежнего мира
мягко уходят на стартовый экран с подписью «Данные обновлены» (app/demo/world.py).
"""

from __future__ import annotations

import logging
import threading
import time as clock
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.demo.accounts import DEMO_ACCOUNTS, demo_enabled
from app.demo.dataset import DemoSummary, build_demo_dataset
from app.demo.scenarios import set_epoch
from app.demo.world import world
from app.models.client import Client
from app.models.courier_cash_handover import CourierCashHandover
from app.models.log import OrderChangeLog
from app.models.order import Order
from app.models.payment import Payment
from app.models.user import User
from app.ui.login_preview import forget as forget_login_preview
from app.utils.dates import CRM_TIMEZONE


logger = logging.getLogger("app.demo")

MANUAL_RESET_COOLDOWN = timedelta(seconds=20)
# Раз в минуту: проверка — один запрос к базе, а потолок должен ловить 5 минут тишины.
CHECK_INTERVAL_SECONDS = 60
# Полная пересборка каждый день в 06:00 по Москве.
DAILY_REBUILD_AT = time(6, 0)
# Тихая пересборка: мир собран больше часа назад и 20 минут никто ничего не менял.
QUIET_REBUILD_AGE = timedelta(hours=1)
QUIET_REBUILD_IDLE = timedelta(minutes=20)
# Потолок: мир не старше 6 часов; ждём 5 минут тишины, но не дольше 30 минут.
CEILING_AGE = timedelta(hours=6)
CEILING_QUIET = timedelta(minutes=5)
CEILING_MAX_WAIT = timedelta(minutes=30)
TABLES_IN_DELETE_ORDER = (
    OrderChangeLog,
    Payment,
    CourierCashHandover,
    Order,
    Client,
    User,
)
ADVISORY_LOCK_ID = 7_261_003

_lock = threading.Lock()


@dataclass
class DemoState:
    last_reset: datetime | None = None


state = DemoState()


class ResetTooSoon(Exception):
    def __init__(self, seconds_left: int):
        super().__init__(f"Данные только что обновили. Повторить можно через {seconds_left} с.")
        self.seconds_left = seconds_left


def reset_demo(session_factory, *, manual: bool = False, now: datetime | None = None) -> DemoSummary:
    """Стереть данные CRM и собрать демо заново. Возвращает сводку по новым данным."""
    with _lock:
        now = (now or datetime.now(CRM_TIMEZONE)).astimezone(CRM_TIMEZONE)
        if manual and state.last_reset and now - state.last_reset < MANUAL_RESET_COOLDOWN:
            left = MANUAL_RESET_COOLDOWN - (now - state.last_reset)
            raise ResetTooSoon(max(1, int(left.total_seconds())))
        with session_factory() as db:
            locked = _try_advisory_lock(db)
            if not locked:
                raise ResetTooSoon(5)
            _wipe(db)
            summary = build_demo_dataset(db, today=now.date(), now=now)
            db.commit()
            world.set_version(demo_epoch(db))
            engine = db.get_bind()
        _vacuum(engine)
        # Превью на входе держит цифры 30 с — после пересборки они уже от другого мира.
        forget_login_preview()
        if manual:
            set_epoch(now.isoformat())
        state.last_reset = now
        logger.info(
            "Демо собрано на %s: заявок %s, оплат %s, сдач %s",
            summary.today.isoformat(), summary.orders, summary.payments, summary.handovers,
        )
        return summary


def demo_epoch(db: Session) -> str:
    """Метка текущего набора данных: время последней сборки демо, меняется при каждом сбросе."""
    admin = db.scalar(select(User).where(User.email == DEMO_ACCOUNTS["admin"].email))
    return admin.created_at.isoformat() if admin and admin.created_at else ""


def data_is_stale(db: Session, *, now: datetime | None = None) -> bool:
    """Демо собрано не сегодня (по Москве) или его нет вовсе."""
    now = now or datetime.now(CRM_TIMEZONE)
    built_at = _built_at(db)
    if built_at is None:
        return True
    return built_at.date() < now.astimezone(CRM_TIMEZONE).date()


_UNSET = object()


def rebuild_reason(db: Session, *, now: datetime | None = None, activity=_UNSET) -> str | None:
    """Пора ли пересобрать мир в фоне и почему. None — мир свежий или гости ещё работают.

    activity — когда посетитель в последний раз открывал страницу или что-то делал (по умолчанию — из памяти
    процесса); нужна только потолку, тихая пересборка смотрит лишь на изменения в базе.
    """
    now = (now or datetime.now(CRM_TIMEZONE)).astimezone(CRM_TIMEZONE)
    built_at = _built_at(db)
    if built_at is None:
        return "нет данных"
    daily = datetime.combine(now.date(), DAILY_REBUILD_AT, tzinfo=CRM_TIMEZONE)
    if now >= daily > built_at:
        return "утренняя пересборка"
    age = now - built_at
    if age >= QUIET_REBUILD_AGE:
        changed_at = last_change_at(db)
        if changed_at is None or now - changed_at >= QUIET_REBUILD_IDLE:
            return "мир отстал от часов"
        if age >= CEILING_AGE:
            seen = world.last_activity if activity is _UNSET else activity
            busy_at = max(moment for moment in (changed_at, seen) if moment is not None)
            if now - busy_at >= CEILING_QUIET:
                return "мир старше 6 часов"
            if age >= CEILING_AGE + CEILING_MAX_WAIT:
                return "мир старше 6 часов, тишины не дождались"
    return None


def last_change_at(db: Session) -> datetime | None:
    """Когда в мире что-то меняли в последний раз: заявки, оплаты, сдачи, клиенты, сотрудники.

    Берётся из базы, поэтому верно при любом числе процессов приложения.
    """
    moments = [
        db.scalar(select(func.max(OrderChangeLog.created_at))),
        db.scalar(select(func.max(Payment.created_at))),
        db.scalar(select(func.max(CourierCashHandover.created_at))),
        db.scalar(select(func.max(CourierCashHandover.confirmed_at))),
        db.scalar(select(func.max(Client.created_at))),
        db.scalar(select(func.max(User.created_at))),
    ]
    moments = [_aware(moment) for moment in moments if moment is not None]
    return max(moments).astimezone(CRM_TIMEZONE) if moments else None


def _aware(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


def _built_at(db: Session) -> datetime | None:
    """Время сборки демо по Москве: оно же время создания демо-владельца."""
    admin = db.scalar(select(User).where(User.email == DEMO_ACCOUNTS["admin"].email))
    if admin is None or admin.created_at is None:
        return None
    built_at = admin.created_at
    if built_at.tzinfo is None:
        built_at = built_at.replace(tzinfo=timezone.utc)
    return built_at.astimezone(CRM_TIMEZONE)


def start_daily_reset(session_factory) -> threading.Thread | None:
    """Фоновая проверка: при старте и каждые 15 минут пересобираем мир, если пора."""
    if not demo_enabled():
        return None

    def loop():
        while True:
            try:
                with session_factory() as db:
                    # Мир мог пересобрать другой процесс (seed_demo в контейнере) — версия для открытых вкладок.
                    world.set_version(demo_epoch(db))
                    reason = rebuild_reason(db)
                if reason:
                    logger.info("Пересборка демо: %s", reason)
                    reset_demo(session_factory)
            except Exception:  # фоновой задаче нельзя падать: следующая проверка попробует снова
                logger.exception("Автосброс демо не удался")
            clock.sleep(CHECK_INTERVAL_SECONDS)

    thread = threading.Thread(target=loop, name="demo-daily-reset", daemon=True)
    thread.start()
    return thread


def _wipe(db: Session) -> None:
    """Стереть данные CRM в текущей транзакции пересборки.

    DELETE, а не TRUNCATE: TRUNCATE берёт эксклюзивные блокировки таблиц по одной и попадает во взаимную
    блокировку со страницей, которая в этот момент читает те же таблицы (PostgreSQL обрывает одну из
    транзакций — сброс падал с 500). DELETE чтению не мешает: до фиксации посетители видят прежний мир,
    после — новый целиком. Счётчики номеров перезапускаются в той же транзакции (откатываются вместе
    с ней), поэтому номера и ссылки заявок в течение дня прежние.
    """
    for model in TABLES_IN_DELETE_ORDER:
        db.execute(delete(model))
    if db.bind.dialect.name == "postgresql":
        for model in TABLES_IN_DELETE_ORDER:
            sequence = db.scalar(text("SELECT pg_get_serial_sequence(:table, 'id')"), {"table": model.__tablename__})
            if sequence:
                db.execute(text(f"ALTER SEQUENCE {sequence} RESTART WITH 1"))
    db.flush()


def _vacuum(engine: Engine) -> None:
    """Вернуть место удалённых строк: без этого каждая пересборка оставляла бы прежний мир мёртвыми строками.

    Обычный VACUUM (не FULL) не мешает чтению и записи; свободное место в конце таблицы он отдаёт системе,
    остальное переиспользует следующая пересборка — размер базы держится на одном уровне.
    """
    if engine.dialect.name != "postgresql":
        return
    tables = ", ".join(model.__tablename__ for model in TABLES_IN_DELETE_ORDER)
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f"VACUUM (ANALYZE) {tables}"))
    except Exception:  # место вернёт автоочистка базы; пересборка уже зафиксирована
        logger.warning("VACUUM после пересборки не прошёл", exc_info=True)


def _try_advisory_lock(db: Session) -> bool:
    if db.bind.dialect.name != "postgresql":
        return True
    return bool(db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": ADVISORY_LOCK_ID}))

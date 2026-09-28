"""«День курьеров»: когда забирали и доставляли заказы выбранного дня — по журналу заявок.

Новый вид настоящих данных: только записанные системой отметки («у курьера», «доставлено» и смена
статуса из офиса). Планового времени у заявок в системе нет, поэтому будущее показано не точками
на шкале, а числом оставшихся остановок. Шкала — от часа до первой отметки до трёх часов после
«сейчас» (для прошлого дня — до часа после последней), чтобы отметки не жались в угол.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import OrderStatus
from app.models.log import OrderChangeLog
from app.models.order import Order
from app.utils.dates import CRM_TIMEZONE

EARLIEST_HOUR = 6
LATEST_HOUR = 24
MIN_SPAN_HOURS = 6
COURIER_ACTIONS = {"у курьера": "pickup", "доставлено": "delivered"}
STATUS_KINDS = {OrderStatus.AT_COURIER.value: "pickup", OrderStatus.DELIVERED.value: "delivered"}
CLUSTER_MINUTES = 11  # забрал несколько заказов почти разом — одна отметка с числом
MARK_GAP = 3.2  # % шкалы между соседними отметками (~20 px на обычной ширине), ближе — отметка сдвигается


@dataclass(frozen=True)
class DayEvent:
    kind: str
    at: datetime
    order_id: int
    order_code: str

    @property
    def minutes(self) -> int:
        return self.at.hour * 60 + self.at.minute


@dataclass
class DayMark:
    """Отметка на линии: одна или несколько подряд (курьер забрал два заказа разом)."""

    kind: str
    events: list[DayEvent]
    position: float = 0.0  # 0–100 % по шкале — настоящее время
    x: float = 0.0  # где нарисована: сдвинута, если наложилась бы на соседнюю

    @property
    def first(self) -> DayEvent:
        return self.events[0]

    @property
    def count(self) -> int:
        return len(self.events)

    @property
    def label(self) -> str:
        verb = "забрал" if self.kind == "pickup" else "доставил"
        codes = ", ".join(event.order_code for event in self.events)
        return f"{codes}: {verb} в {self.first.at:%H:%M}"


@dataclass
class DayRow:
    courier_id: int
    courier_name: str
    remaining: int = 0
    events: list[DayEvent] = field(default_factory=list)
    marks: list[DayMark] = field(default_factory=list)
    band_start: float = 0.0  # линия маршрута: от первой отметки
    band_end: float = 0.0  # до последней, а если остановки ещё есть — до «сейчас»

    @property
    def delivered(self) -> int:
        return sum(1 for event in self.events if event.kind == "delivered")


@dataclass(frozen=True)
class DayTimeline:
    day: date
    start_hour: int
    end_hour: int
    rows: list[DayRow]
    now: datetime | None = None  # только для сегодняшнего дня
    now_position: float | None = None

    @property
    def hour_step(self) -> int:
        return 1 if self.end_hour - self.start_hour <= 9 else 2

    @property
    def hours(self) -> list[tuple[int, float]]:
        """Подписи часов с шагом сетки — от первого часа шкалы, чтобы совпадали с линиями сетки."""
        span = self.end_hour - self.start_hour
        return [(hour, (hour - self.start_hour) * 100 / span) for hour in range(self.start_hour, self.end_hour + 1, self.hour_step)]

    @property
    def grid_percent(self) -> float:
        return round(self.hour_step * 100 / (self.end_hour - self.start_hour), 4)

    @property
    def has_events(self) -> bool:
        return any(row.events for row in self.rows)

    @property
    def has_group_pickups(self) -> bool:
        return any(mark.count > 1 for row in self.rows for mark in row.marks)

    def position(self, moment: datetime) -> float:
        minutes = (moment.hour - self.start_hour) * 60 + moment.minute
        return round(max(0.0, min(100.0, minutes * 100 / ((self.end_hour - self.start_hour) * 60))), 2)


def courier_day_timeline(db: Session, day: date, orders: list[Order], *, now: datetime | None = None) -> DayTimeline:
    """Строки по курьерам из заявок маршрута (тот же набор, что в сводке маршрута) и их журнала."""
    rows: dict[int, DayRow] = {}
    by_id: dict[int, Order] = {}
    for order in orders:
        if order.courier_id is None or order.courier is None:
            continue
        by_id[order.id] = order
        row = rows.setdefault(order.courier_id, DayRow(order.courier_id, order.courier.full_name))
        if order.status != OrderStatus.DELIVERED:
            row.remaining += 1
    current = (now or datetime.now(CRM_TIMEZONE)).astimezone(CRM_TIMEZONE)
    is_today = current.date() == day

    if by_id:
        logs = db.execute(
            select(OrderChangeLog.order_id, OrderChangeLog.action, OrderChangeLog.new_value, OrderChangeLog.created_at)
            .where(
                OrderChangeLog.order_id.in_(tuple(by_id)),
                OrderChangeLog.action.in_((*COURIER_ACTIONS, "статус")),
            )
            .order_by(OrderChangeLog.created_at, OrderChangeLog.id)
        ).all()
        for order_id, action, new_value, created_at in logs:
            kind = COURIER_ACTIONS.get(action) or _status_kind(new_value)
            if kind is None:
                continue
            moment = created_at.replace(tzinfo=timezone.utc) if created_at.tzinfo is None else created_at
            local = moment.astimezone(CRM_TIMEZONE)
            if local.date() != day:
                continue
            order = by_id[order_id]
            rows[order.courier_id].events.append(DayEvent(kind, local, order.id, order.order_code))

    start_hour, end_hour = _range([event for row in rows.values() for event in row.events], current if is_today else None)
    timeline = DayTimeline(day, start_hour, end_hour, sorted(rows.values(), key=lambda row: row.courier_name))
    # «Сейчас» за краями шкалы (ночью, до первого часа) не рисуем: прижатая к краю линия легла бы на подпись часа.
    in_range = start_hour * 60 <= current.hour * 60 + current.minute <= end_hour * 60
    now_position = timeline.position(current) if is_today and in_range else None
    for row in timeline.rows:
        row.marks = _layout(_marks(row.events), timeline)
        if row.events:
            positions = [timeline.position(event.at) for event in row.events]
            row.band_start = min(positions)
            row.band_end = max(positions)
            if row.remaining and now_position is not None:
                row.band_end = max(row.band_end, now_position)
    return DayTimeline(
        day, start_hour, end_hour, timeline.rows, now=current if is_today else None, now_position=now_position
    )


def _range(events: list[DayEvent], now: datetime | None) -> tuple[int, int]:
    """Часы шкалы: час до первой отметки — три часа после «сейчас» (прошлый день — час после последней)."""
    minutes = [event.minutes for event in events]
    if now is not None:
        minutes.append(now.hour * 60 + now.minute)
    if not minutes:
        return 8, 20
    start = math.floor(min(minutes) / 60) - 1
    end = math.ceil(max(minutes) / 60) + (3 if now is not None else 1)
    start, end = max(EARLIEST_HOUR, start), min(LATEST_HOUR, end)
    if end - start < MIN_SPAN_HOURS:
        end = min(LATEST_HOUR, start + MIN_SPAN_HOURS)
        start = max(EARLIEST_HOUR, end - MIN_SPAN_HOURS)
    return start, end


def _marks(events: list[DayEvent]) -> list[DayMark]:
    """Отметки по времени; «забрал» почти разом — одна отметка с числом."""
    marks: list[DayMark] = []
    for event in sorted(events, key=lambda item: item.at):
        last = marks[-1] if marks else None
        if last and event.kind == "pickup" == last.kind and event.minutes - last.events[-1].minutes <= CLUSTER_MINUTES:
            last.events.append(event)
        else:
            marks.append(DayMark(event.kind, [event]))
    return marks


def _layout(marks: list[DayMark], timeline: DayTimeline) -> list[DayMark]:
    """Все отметки на одной линии; наложившуюся сдвигаем вправо, время остаётся в подсказке."""
    previous = None
    for mark in marks:
        mark.position = timeline.position(mark.first.at)
        mark.x = mark.position if previous is None else max(mark.position, previous + MARK_GAP)
        mark.x = round(min(100.0, mark.x), 2)
        previous = mark.x
    return marks


def _status_kind(new_value: str | None) -> str | None:
    try:
        status = json.loads(new_value or "{}").get("status")
    except (ValueError, AttributeError):
        return None
    return STATUS_KINDS.get(status)

"""Живой демо-мир курьерской службы: 2,5 месяца истории, сегодняшний день и планы на два дня вперёд.

Мир детерминирован по дате: у каждого дня своё зерно случайности, у каждой заявки — своё. Пересборка
в течение одного дня даёт тот же день, двигается только граница между сделанным и запланированным:
всё, что по плану было до текущего момента, завершено, что идёт сейчас — в работе, дальше — запланировано.

У каждого курьера на день маршрут по его смене: пары заказов он забирает и развозит по очереди, поэтому
в пути у него одна-две заявки. Все люди, компании, телефоны и номера грузов вымышленные.
"""

from __future__ import annotations

import json
import random
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy.orm import Session

from app.demo.accounts import DEMO_ACCOUNTS, account_password
from app.models.client import Client
from app.models.courier_cash_handover import CourierCashHandover
from app.models.enums import (
    CourierCashHandoverStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    UserRole,
)
from app.models.log import OrderChangeLog
from app.models.order import Order
from app.models.payment import Payment
from app.models.user import User
from app.services.order_service import ORDER_SERIES, _order_snapshot, calculate_delivery_cost
from app.services.payment_service import _payment_snapshot
from app.utils.dates import CRM_TIMEZONE
from app.utils.security import hash_password


HISTORY_DAYS = 74
FUTURE_DAYS = 2
RANDOM_SEED = 20260926
TEN = Decimal("10")

# Офис принимает заявки и отмечает оплаты с 9 до 21; утренняя раздача маршрутов — с 07:15.
OFFICE_OPEN = time(9, 0)
OFFICE_LAST_ORDER = time(20, 30)
OFFICE_CLOSE = time(20, 45)
MORNING_DISPATCH = time(7, 15)

# Доли оплат: заранее переводом, при доставке, по счёту через несколько дней, частично.
PREPAID_SHARE = 0.3
PREPAID_SHARE_REGULAR = 0.55
PREPAID_SHARE_MARKET = 0.5
INVOICE_SHARE = 0.04
PARTIAL_SHARE = 0.012
CANCEL_SHARE = 0.013


@dataclass(frozen=True)
class StaffMember:
    email: str
    full_name: str
    phone: str
    role: UserRole
    # Рабочие дни недели (0 — понедельник), вес в распределении заявок и смена по будням.
    workdays: tuple[int, ...] = (0, 1, 2, 3, 4)
    weight: float = 1.0
    active: bool = True
    left_days_ago: int | None = None
    shift: tuple[time, time] = (time(8, 30), time(18, 30))
    weekend_shift: tuple[time, time] = (time(10, 0), time(17, 0))


STAFF: tuple[StaffMember, ...] = (
    StaffMember("olga@example.com", "Ольга Смирнова", "+79161234504", UserRole.MANAGER),
    StaffMember("belov@example.com", "Артём Белов", "+79161234511", UserRole.COURIER, (0, 1, 2, 3, 4), 1.1,
                shift=(time(8, 0), time(17, 30))),
    StaffMember("gafurov@example.com", "Руслан Гафуров", "+79161234512", UserRole.COURIER, (1, 2, 3, 4, 5, 6), 1.0,
                shift=(time(10, 0), time(20, 30)), weekend_shift=(time(10, 30), time(18, 0))),
    StaffMember("orlov@example.com", "Денис Орлов", "+79161234513", UserRole.COURIER, (0, 1, 2, 3, 4, 5), 0.9,
                shift=(time(8, 30), time(18, 30)), weekend_shift=(time(9, 30), time(16, 30))),
    StaffMember("safin@example.com", "Тимур Сафин", "+79161234514", UserRole.COURIER, (2, 3, 4, 5, 6), 0.8,
                shift=(time(11, 0), time(21, 30)), weekend_shift=(time(11, 0), time(19, 30))),
    StaffMember("zuev@example.com", "Павел Зуев", "+79161234515", UserRole.COURIER, (0, 1, 2, 3, 4), 0.9,
                active=False, left_days_ago=40, shift=(time(8, 0), time(17, 0))),
)
# Демо-курьер работает каждый день: у гостя всегда есть маршрут, где можно нажать «Забрал» и «Доставил».
DEMO_COURIER_WORKDAYS = (0, 1, 2, 3, 4, 5, 6)
DEMO_COURIER_WEIGHT = 1.5
DEMO_COURIER_SHIFT = (time(9, 0), time(20, 30))
DEMO_COURIER_WEEKEND_SHIFT = (time(10, 0), time(18, 0))


def demo_route_switch(day: date) -> datetime:
    """Когда сценарий курьера уводит гостя с маршрута этого дня на следующий: за 45 минут до конца смены."""
    shift_end = DEMO_COURIER_WEEKEND_SHIFT[1] if day.weekday() >= 5 else DEMO_COURIER_SHIFT[1]
    return datetime.combine(day, shift_end, tzinfo=CRM_TIMEZONE) - timedelta(minutes=45)


def demo_courier_route_day(now: datetime) -> date:
    """День, в котором у демо-курьера есть что забрать и доставить: сегодня, а после смены — завтра."""
    local = now.astimezone(CRM_TIMEZONE)
    return local.date() + timedelta(days=1) if local >= demo_route_switch(local.date()) else local.date()


# Названия — как их пишут по-русски: вид дела строчными, имя в кавычках-ёлочках.
BUSINESS_CLIENTS = (
    ("Кофейня «Зерно и пар»", "Звонить за час до приезда"),
    ("ООО «Текстиль Плюс»", "Принимает кладовщик с 9 до 18"),
    ("ИП Гусев Р. А.", None),
    ("Цветочная лавка «Флокс»", "Вход со двора, шлагбаум по звонку"),
    ("ООО «Уютный дом»", None),
    ("Салон красоты «Лаванда»", "Не звонить после 20:00"),
    ("Пекарня «Колос»", "Разгрузка только до 11:00"),
    ("ООО «Стройкомплект»", "Оплата переводом при получении"),
    ("Зоомагазин «Хвостик»", None),
    ("Студия «Лофт Декор»", "Постоянный клиент"),
    ("ИП Мельникова О. В.", None),
    ("Типография «Оттиск»", "Забирать документы на подпись"),
    ("ООО «Вектор Логистик»", None),
    ("Магазин одежды «Сезон»", "Примерка на месте, подождать 10 минут"),
    ("ИП Карпов Д. С.", None),
    ("Шоурум «Линия»", "Постоянный клиент"),
    ("ООО «Мебель Мастер»", "Нужны двое на подъём"),
    ("Кондитерская «Ваниль»", None),
    ("Автосервис «Гараж 21»", "Въезд с торца здания"),
    ("ООО «Эко Продукт»", None),
    ("Магазин «Хобби центр»", None),
    ("ИП Литвинова Е. Ю.", "Оплата наличными"),
    ("Фотостудия «Кадр»", None),
    ("ООО «Техно Склад»", "Пропуск заказывать заранее"),
    ("Барбершоп «Бритва»", None),
    ("Интернет-магазин «Полка»", "Постоянный клиент"),
    ("ООО «Медтехника Сервис»", None),
    ("Детский клуб «Совёнок»", "Принимать с 10 до 19"),
    ("ИП Широков А. П.", None),
    ("Магазин «Посуда центр»", "Хрупкое, стекло"),
    ("ООО «Ремонт под ключ»", None),
    ("Книжная лавка «Абзац»", None),
    ("Мастерская «Кожа и нить»", None),
    ("Сеть кофеен «Корица»", "Две точки, уточнять адрес"),
    ("ООО «Склад 24»", "Пропуск на КПП по фамилии курьера"),
)

MALE_NAMES = (
    "Алексей", "Дмитрий", "Сергей", "Никита", "Михаил", "Кирилл", "Роман", "Евгений", "Владимир",
    "Максим", "Антон", "Олег", "Григорий", "Степан", "Вадим", "Ярослав", "Фёдор", "Глеб", "Матвей",
)
FEMALE_NAMES = (
    "Анна", "Екатерина", "Ольга", "Ирина", "Наталья", "Светлана", "Юлия", "Марина", "Дарья", "Алина",
    "Ксения", "Виктория", "Полина", "Вера", "Елена", "Татьяна", "Софья", "Валерия",
)
SURNAMES = (
    "Лебедев", "Кузнецов", "Морозов", "Новиков", "Павлов", "Семёнов", "Егоров", "Тарасов", "Белоусов",
    "Комаров", "Орехов", "Зайцев", "Соловьёв", "Виноградов", "Богданов", "Воробьёв", "Фролов",
    "Мельников", "Щербаков", "Блинов", "Колесников", "Карпов", "Афанасьев", "Литвинов", "Гусев",
    "Титов", "Кудрявцев", "Баранов", "Широков", "Дроздов", "Ершов", "Рябов", "Голубев", "Панов",
)
PERSON_NOTES = (
    "Звонить за час до приезда",
    "Домофон не работает, звонить на мобильный",
    "Не звонить после 20:00",
    "Постоянный клиент",
    "Оплатит переводом",
    None, None, None, None, None, None, None, None,
)

# Улицы, а не готовые адреса: дом у каждого клиента свой (адрес не повторяется у двух клиентов), у людей — своя
# квартира, у организаций — офис или только дом. Улицы из подсказки формы заявки («ул. Покровка») здесь нет.
MOSCOW_STREETS = (
    "ул. Большая Дмитровка", "ул. Тверская", "Ленинский проспект", "ул. Профсоюзная", "Кутузовский проспект",
    "ул. Новый Арбат", "Ленинградский проспект", "ул. Сретенка", "Варшавское шоссе", "ул. Бауманская",
    "Щёлковское шоссе", "ул. Маросейка", "проспект Мира", "ул. Пятницкая", "Каширское шоссе",
    "ул. Академика Королёва", "ул. Люблинская", "Рязанский проспект", "ул. Митинская", "Дмитровское шоссе",
    "ул. Вавилова", "Волгоградский проспект", "ул. Садовая-Спасская", "ул. Большая Черкизовская",
    "Мичуринский проспект", "ул. Народного Ополчения", "ул. Земляной Вал", "Севастопольский проспект",
    "ул. Бутлерова", "ул. Нижняя Масловка", "Смоленский бульвар", "ул. Складочная", "ул. Нагатинская",
    "ул. Верхняя Красносельская", "Хорошёвское шоссе", "ул. Шаболовка", "ул. Вешняковская", "Алтуфьевское шоссе",
    "ул. Кировоградская", "ул. Электрозаводская", "ул. Бакунинская", "Нахимовский проспект", "ул. Генерала Белова",
)
REGION_STREETS = (
    ("Химки", "ул. Панфилова"), ("Химки", "Юбилейный проспект"), ("Мытищи", "Олимпийский проспект"),
    ("Люберцы", "Октябрьский проспект"), ("Подольск", "ул. Кирова"), ("Красногорск", "Волоколамское шоссе"),
    ("Балашиха", "ул. Советская"), ("Королёв", "проспект Космонавтов"), ("Одинцово", "Можайское шоссе"),
    ("Реутов", "ул. Победы"), ("Долгопрудный", "Лихачёвский проспект"), ("Видное", "ул. Школьная"),
    ("Щёлково", "ул. Талсинская"), ("Пушкино", "Московский проспект"), ("Лобня", "ул. Ленина"),
)

# Заметки к заявке — по месту: ресепшен и документы — у офиса, подъезд и этаж — у квартиры.
ORDER_NOTES = (
    "Позвонить за час до приезда",
    "Хрупкое: стекло, не кантовать",
    "Доставить после 18:00",
    "Коробки тяжёлые, нужна тележка",
    "Въезд со двора, шлагбаум по звонку",
)
OFFICE_NOTES = ("Оставить на ресепшене", "Забрать документы на подпись", "Разгрузка с 10 до 17")
HOME_NOTES = ("Клиент встретит у подъезда", "Подъём на 5 этаж без лифта", "Домофон не работает, звонить на мобильный")
COURIER_NOTES = (
    "Забрать на складе, ворота 3",
    "Груз на втором ярусе, спросить кладовщика",
    "Сверить количество мест при получении",
    "Получение по доверенности, фото накладной менеджеру",
    "Наличные при получении, сдача с 5 000",
)
# Номер груза — в духе подсказки формы самой системы («GR-24115»): без букв городов, складов и кодов клиентов.
CARGO_PREFIX = "GR"
SNAPSHOT_FIELDS = (
    "order_code", "client_name_snapshot", "client_phone_snapshot", "address", "yandex_url",
    "delivery_date", "base_delivery_cost", "market_cube_cost", "market_loader_cost",
    "market_storage_cost", "market_kara_cost", "market_other_cost", "delivery_cost", "courier_pay",
)


@dataclass
class DemoSummary:
    today: date
    users: int = 0
    clients: int = 0
    orders: int = 0
    payments: int = 0
    handovers: int = 0
    archived: int = 0


@dataclass
class _PayPlan:
    """Оплата по плану: способ, доля суммы и момент, когда её отметят в системе."""

    method: PaymentMethod
    share: Decimal | None  # None — вся сумма, иначе доля (частичная оплата)
    at: datetime
    rest: bool = False  # остаток после частичной оплаты


@dataclass
class _Plan:
    """План заявки на весь её путь. Какие события уже случились, решают часы сборки."""

    key: str
    day: date
    rng: random.Random
    client: Client
    address: str
    courier: User
    creator: User
    created_at: datetime
    assigned_at: datetime
    pickup_at: datetime
    delivered_at: datetime
    edited_at: datetime | None = None
    archived_at: datetime | None = None
    archived_by: User | None = None
    pays: list[_PayPlan] = field(default_factory=list)
    # после сборки
    order: Order | None = None
    written_payments: list[tuple[Decimal, PaymentMethod, datetime, User]] = field(default_factory=list)


def build_demo_dataset(db: Session, *, today: date, now: datetime | None = None) -> DemoSummary:
    return _Builder(db, today, now).build()


class _Builder:
    def __init__(self, db: Session, today: date, now: datetime | None = None):
        self.db = db
        self.today = today
        self.now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
        self.start = today - timedelta(days=HISTORY_DAYS)
        self.rng = random.Random(RANDOM_SEED)
        self.summary = DemoSummary(today=today)

    # ------------------------------------------------------------------ сборка
    def build(self) -> DemoSummary:
        self._create_users()
        self._create_clients()
        plans = [plan for plan in self._plan_world() if plan.created_at <= self.now]
        self._write_orders(plans)
        self._write_handovers(plans)
        self.db.flush()
        return self.summary

    def _create_users(self) -> None:
        # Время создания демо-сотрудников — момент сборки мира: по нему видно, когда пора пересобрать.
        created_at = self.now
        accounts = {}
        for key, account in DEMO_ACCOUNTS.items():
            user = User(
                email=account.email,
                full_name=account.full_name,
                phone=account.phone,
                role=account.role,
                is_active=True,
                password_hash=_password_hash(key, account_password(account)),
                created_at=created_at,
            )
            self.db.add(user)
            accounts[key] = user
        self.admin = accounts["admin"]
        self.manager = accounts["manager"]
        self.demo_courier = accounts["courier"]
        self.managers = [self.manager]
        self.couriers: list[tuple[User, StaffMember | None]] = [(self.demo_courier, None)]
        for member in STAFF:
            user = User(
                email=member.email,
                full_name=member.full_name,
                phone=member.phone,
                role=member.role,
                is_active=member.active,
                password_hash=_password_hash("staff", None),
                created_at=created_at,
            )
            self.db.add(user)
            if member.role == UserRole.MANAGER:
                self.managers.append(user)
            else:
                self.couriers.append((user, member))
        self.db.flush()
        self.summary.users = len(DEMO_ACCOUNTS) + len(STAFF)

    def _create_clients(self) -> None:
        rng = self.rng
        clients: list[tuple[Client, list[str], float]] = []
        used_phones: set[str] = set()
        self.used_addresses: set[str] = set()
        self.used_rooms: set[str] = set()
        for index, (name, note) in enumerate(BUSINESS_CLIENTS):
            addresses = self._client_addresses(2 if index % 4 == 0 else 1, region_share=0.25, business=True)
            weight = 3.2 if note == "Постоянный клиент" else rng.uniform(1.2, 2.6)
            clients.append((self._client(name, note, used_phones), addresses, weight))
        used_names: set[str] = set()
        while len(used_names) < 78:
            female = rng.random() < 0.52
            first = rng.choice(FEMALE_NAMES if female else MALE_NAMES)
            surname = rng.choice(SURNAMES) + ("а" if female else "")
            name = f"{first} {surname}"
            if name in used_names:
                continue
            used_names.add(name)
            addresses = self._client_addresses(1, region_share=0.35, business=False)
            clients.append((self._client(name, rng.choice(PERSON_NOTES), used_phones), addresses, rng.uniform(0.3, 1.0)))
        # Карточки клиентов заведены до начала истории — у каждого заказа клиент уже есть в базе.
        for client, _, _ in clients:
            day = self.start - timedelta(days=rng.randint(3, 300))
            client.created_at = self._local(day, time(rng.randint(9, 18), rng.randint(0, 59)))
            client.created_by_id = self.manager.id
        self.db.flush()
        self.clients = clients
        self.summary.clients = len(clients)

    def _client(self, name: str, note: str | None, used_phones: set[str]) -> Client:
        phone = self._phone(used_phones)
        client = Client(full_name=name, phone=phone, notes=note)
        self.db.add(client)
        return client

    def _phone(self, used: set[str]) -> str:
        while True:
            code = self.rng.choice(("903", "905", "910", "915", "916", "925", "926", "929", "965", "977", "985", "999"))
            digits = f"{self.rng.randint(0, 9999999):07d}"
            phone = f"+7 {code} {digits[:3]}-{digits[3:5]}-{digits[5:]}"
            if phone not in used:
                used.add(phone)
                return phone

    def _client_addresses(self, count: int, *, region_share: float, business: bool) -> list[str]:
        return [self._address(region_share, business) for _ in range(count)]

    def _address(self, region_share: float, business: bool) -> str:
        """Дом, которого ещё нет у других клиентов; людям — квартира, организациям — офис или только дом."""
        rng = self.rng
        while True:
            region = rng.random() < region_share
            if region and rng.random() < 0.08:
                house = f"Зеленоград, корп. {rng.randint(1101, 2045)}"
            elif region:
                town, street = rng.choice(REGION_STREETS)
                house = f"{town}, {street}, {self._house_number(rng)}"
            else:
                house = f"Москва, {rng.choice(MOSCOW_STREETS)}, {self._house_number(rng)}"
            if house not in self.used_addresses:
                self.used_addresses.add(house)
                break
        if business:
            if rng.random() >= 0.55:
                return house
            # Чаще небольшие офисы, реже — номера бизнес-центров.
            office = self._room(rng, "офис", 2, 48) if rng.random() < 0.7 else self._room(rng, "офис", 101, 520)
            return house + f", офис {office}"
        if region and not house.startswith("Зеленоград") and rng.random() < 0.15:
            return house  # частный дом
        flat = self._room(rng, "кв", 2, 320)
        if rng.random() < 0.3:
            # Подъезд — по номеру квартиры: в подъезде около 36 квартир.
            return house + f", под. {1 + (flat - 1) // 36}, кв. {flat}"
        return house + f", кв. {flat}"

    @staticmethod
    def _house_number(rng: random.Random) -> str:
        number = rng.randint(1, 140)
        return f"{number}, корп. {rng.randint(1, 4)}" if rng.random() < 0.12 else str(number)

    def _room(self, rng: random.Random, kind: str, low: int, high: int) -> int:
        """Номер квартиры или офиса, которого ещё нет в мире: «кв. 12» не повторяется у разных адресов."""
        while True:
            number = rng.randint(low, high)
            if f"{kind}{number}" not in self.used_rooms:
                self.used_rooms.add(f"{kind}{number}")
                return number

    # ------------------------------------------------------------- план мира
    def _plan_world(self) -> list[_Plan]:
        plans: list[_Plan] = []
        last = self.today + timedelta(days=FUTURE_DAYS)
        day = self.start
        while day <= last:
            plans.extend(self._plan_day(day))
            day += timedelta(days=1)
        return plans

    def _orders_count(self, day: date, rng: random.Random) -> int:
        base = (20, 22, 21, 23, 25, 12, 7)[day.weekday()]
        return max(3, round(base * rng.uniform(0.9, 1.1)))

    def _working_couriers(self, day: date) -> list[tuple[User, str, float, time, time]]:
        weekend = day.weekday() >= 5
        result = []
        for user, member in self.couriers:
            if member is None:
                shift = DEMO_COURIER_WEEKEND_SHIFT if weekend else DEMO_COURIER_SHIFT
                if day.weekday() in DEMO_COURIER_WORKDAYS:
                    result.append((user, "demo", DEMO_COURIER_WEIGHT, *shift))
                continue
            if member.left_days_ago is not None and (self.today - day).days < member.left_days_ago:
                continue
            if member.left_days_ago is None and not member.active:
                continue
            if day.weekday() in member.workdays:
                shift = member.weekend_shift if weekend else member.shift
                result.append((user, member.email.split("@")[0], member.weight, *shift))
        return result

    def _plan_day(self, day: date) -> list[_Plan]:
        rng = random.Random(f"{RANDOM_SEED}|day|{day.isoformat()}")
        couriers = self._working_couriers(day)
        total = self._orders_count(day, rng)
        weights = [weight for _, _, weight, _, _ in couriers]
        counts = [max(1, round(total * weight / sum(weights))) for weight in weights]
        plans: list[_Plan] = []
        taken: set[str] = set()  # адреса дня: одна и та же остановка не достаётся двум курьерам
        for (courier, key, _, shift_start, shift_end), count in zip(couriers, counts):
            start = self._local(day, shift_start) + timedelta(minutes=rng.randint(-10, 15))
            end = self._local(day, shift_end) - timedelta(minutes=rng.randint(0, 20))
            # Первая доставка — через час после начала смены, остальные — равномерно до её конца.
            first = start + timedelta(minutes=rng.randint(45, 70))
            if count == 1:
                delivered = [first]
            else:
                step = (end - first) / (count - 1)
                delivered = [first] + [first + step * k - timedelta(minutes=rng.randint(0, 14)) for k in range(1, count)]
            route = []
            for k in range(count):
                pair = k // 2
                if pair == 0:
                    pickup = start
                else:
                    pickup = delivered[2 * pair - 1] + timedelta(minutes=rng.randint(8, 20))
                route.append(self._plan_stop(day, f"{day.isoformat()}|{key}|{k}", courier, pickup, delivered[k], taken))
            # Маршрут курьер видит по времени создания заявок — пусть оно идёт в порядке остановок.
            ordered = sorted(plan.created_at for plan in route)
            for plan, created in zip(route, ordered):
                created = min(created, plan.pickup_at - timedelta(minutes=45))
                plan.created_at = created
                plan.assigned_at = self._assigned_at(day, created, plan.pickup_at, _event_rng(plan.key, "assign"))
                if plan.edited_at is not None and not created < plan.edited_at < plan.assigned_at:
                    plan.edited_at = None
                if plan.archived_at is not None and plan.archived_at <= created:
                    plan.archived_at = created + timedelta(minutes=40)
            if key == "demo":
                self._keep_demo_route_open(day, route)
            plans.extend(route)
        return plans

    @staticmethod
    def _keep_demo_route_open(day: date, route: list[_Plan]) -> None:
        """У гостя-курьера в любой час есть что забрать. Последний забор дня — не раньше, чем сценарий уводит гостя
        на завтра; заявки дня созданы и назначены до того, как этот день становится его маршрутом."""
        if not route:
            return
        # Время плана — в UTC, как у остальных событий мира.
        last = route[-1]
        late_pickup = demo_route_switch(day).astimezone(timezone.utc)
        if last.pickup_at < late_pickup <= last.delivered_at - timedelta(minutes=20):
            last.pickup_at = late_pickup
        opens = demo_route_switch(day - timedelta(days=1)).astimezone(timezone.utc)
        for index, plan in enumerate(route):
            plan.assigned_at = min(plan.assigned_at, opens - timedelta(minutes=10 + 4 * (len(route) - index)))
            plan.created_at = min(plan.created_at, plan.assigned_at - timedelta(minutes=25))
            if plan.edited_at is not None and not plan.created_at < plan.edited_at < plan.assigned_at:
                plan.edited_at = None
            if plan.archived_at is not None and plan.archived_at <= plan.created_at:
                plan.archived_at = plan.created_at + timedelta(minutes=40)

    def _plan_stop(
        self, day: date, key: str, courier: User, pickup: datetime, delivered: datetime, taken: set[str]
    ) -> _Plan:
        rng = random.Random(f"{RANDOM_SEED}|order|{key}")
        weights = [item[2] for item in self.clients]
        for _ in range(60):
            client, addresses, _ = rng.choices(self.clients, weights=weights)[0]
            address = rng.choice(addresses)
            if address not in taken:
                break
        taken.add(address)
        created = self._created_at(day, pickup, rng)
        plan = _Plan(
            key=key,
            day=day,
            rng=rng,
            client=client,
            address=address,
            courier=courier,
            creator=self._creator(rng),
            created_at=created,
            assigned_at=self._assigned_at(day, created, pickup, rng),
            pickup_at=pickup,
            delivered_at=delivered,
        )
        if rng.random() < 0.08:
            edit = created + timedelta(minutes=rng.randint(10, 180))
            plan.edited_at = min(edit, plan.assigned_at - timedelta(minutes=2))
            if plan.edited_at <= created:
                plan.edited_at = None
        # Отмены — только у штатных курьеров: маршрут гостя-курьера на день всегда целый. Жребий тянем и тут,
        # чтобы остальной мир от этого не сдвигался.
        if rng.random() < CANCEL_SHARE and "|demo|" not in key:
            cancel = created + timedelta(minutes=rng.randint(40, 360))
            if cancel < pickup - timedelta(minutes=15):
                plan.archived_at = cancel
                plan.archived_by = self.admin if rng.random() < 0.4 else self.manager
        return plan

    def _created_at(self, day: date, pickup: datetime, rng: random.Random) -> datetime:
        roll = rng.random()
        if roll < 0.3:
            moment = self._local(day - timedelta(days=2), time(rng.randint(10, 19), rng.randint(0, 59)))
        elif roll < 0.88:
            moment = self._local(day - timedelta(days=1), time(rng.randint(9, 20), rng.randint(0, 59)))
        else:
            moment = self._local(day, time(rng.randint(7, 11), rng.randint(0, 59)))
        latest = pickup - timedelta(minutes=45)
        if moment > latest:
            moment = self._local(day - timedelta(days=1), time(rng.randint(10, 19), rng.randint(0, 59)))
        return moment

    def _assigned_at(self, day: date, created: datetime, pickup: datetime, rng: random.Random) -> datetime:
        """Курьера ставят в маршрут в рабочее время: заявке на сегодня — сразу, на следующие дни — позже."""
        local = created.astimezone(CRM_TIMEZONE)
        same_day = local.date() == day
        moment = created + timedelta(minutes=rng.randint(10, 30) if same_day else rng.randint(40, 170))
        moment_local = moment.astimezone(CRM_TIMEZONE)
        if moment_local.time() > OFFICE_LAST_ORDER or moment_local.time() < MORNING_DISPATCH:
            morning_day = moment_local.date() + timedelta(days=1) if moment_local.time() > OFFICE_LAST_ORDER else moment_local.date()
            moment = self._local(morning_day, MORNING_DISPATCH) + timedelta(minutes=rng.randint(0, 25))
        return min(moment, pickup - timedelta(minutes=10))

    def _creator(self, rng: random.Random) -> User:
        roll = rng.random()
        if roll < 0.08:
            return self.admin
        if roll < 0.72:
            return self.manager
        return self.managers[-1]

    # ------------------------------------------------------------------ запись
    def _write_orders(self, plans: list[_Plan]) -> None:
        plans.sort(key=lambda item: (item.created_at, item.key))
        for number, plan in enumerate(plans, start=1):
            order = self._order_from_plan(plan, number)
            plan.order = order
            self._plan_payments(plan)
            self._apply_clock(plan)
            self.db.add(order)
        self.db.flush()

        for plan in plans:
            self._write_order_history(plan)
        self.summary.orders = len(plans)
        self.summary.archived = sum(1 for plan in plans if plan.order.is_archived)

    def _order_from_plan(self, plan: _Plan, number: int) -> Order:
        rng = plan.rng
        is_region = not plan.address.startswith("Москва")
        kind = rng.choices(("market", "parcel", "docs"), weights=(62, 26, 12))[0]
        weight = volume = None
        places = 1
        costs = {key: Decimal("0") for key in ("cube", "loader", "storage", "kara", "other")}
        cargo_number = cargo_phone = None
        if kind == "market":
            places = rng.randint(1, 8)
            weight = Decimal(str(round(rng.uniform(4, 140), 1)))
            volume = Decimal(str(round(rng.uniform(0.08, 2.4), 2)))
            costs["cube"] = _round10(max(150, float(volume) * rng.uniform(320, 420)))
            if weight > 25:
                costs["loader"] = _round10(rng.uniform(250, 600))
            if rng.random() < 0.42:
                costs["kara"] = _round10(rng.uniform(60, 220))
            if rng.random() < 0.16:
                costs["storage"] = _round10(rng.uniform(100, 400))
            if rng.random() < 0.1:
                costs["other"] = _round10(rng.uniform(50, 200))
            cargo_number = self._cargo_number(rng, number)
            if rng.random() < 0.45:
                cargo_phone = f"+7 495 {rng.randint(100, 999)}-{rng.randint(10, 99)}-{rng.randint(10, 99)}"
            base = _round10(rng.uniform(1000, 1600) + (rng.uniform(600, 1000) if is_region else 0))
        elif kind == "parcel":
            places = rng.randint(1, 3)
            weight = Decimal(str(round(rng.uniform(0.5, 18), 1)))
            volume = Decimal(str(round(rng.uniform(0.01, 0.3), 2)))
            if rng.random() < 0.12:
                costs["storage"] = _round10(rng.uniform(100, 250))
            if rng.random() < 0.3:
                cargo_number = self._cargo_number(rng, number)
            base = _round10(rng.uniform(850, 1350) + (rng.uniform(500, 900) if is_region else 0))
        else:
            weight = Decimal(str(round(rng.uniform(0.2, 1.5), 1)))
            base = _round10(rng.uniform(650, 900) + (rng.uniform(300, 500) if is_region else 0))
        if kind == "docs":
            courier_pay = Decimal("300")
        else:
            courier_pay = _round10(rng.uniform(550, 780) if is_region else rng.uniform(340, 460))

        order = Order(
            order_series=ORDER_SERIES,
            order_number=number,
            order_code=f"{ORDER_SERIES}-{number:04d}",
            client_id=plan.client.id,
            client_name_snapshot=plan.client.full_name,
            client_phone_snapshot=plan.client.phone,
            address=plan.address,
            yandex_url=self._yandex_url(rng) if rng.random() < 0.3 else None,
            delivery_date=plan.day,
            general_note=self._order_note(rng, plan.address) if rng.random() < 0.24 else None,
            cargo_number=cargo_number,
            cargo_phone=cargo_phone,
            client_note=None,
            staff_note=rng.choice(COURIER_NOTES) if kind == "market" and rng.random() < 0.3 else None,
            weight=weight,
            volume=volume,
            places_count=places,
            courier_id=None,
            courier_pay=courier_pay,
            base_delivery_cost=base,
            market_cube_cost=costs["cube"],
            market_loader_cost=costs["loader"],
            market_storage_cost=costs["storage"],
            market_kara_cost=costs["kara"],
            market_other_cost=costs["other"],
            status=OrderStatus.IN_WORK,
            created_by_id=plan.creator.id,
            created_at=plan.created_at,
            updated_at=plan.created_at,
            is_archived=False,
        )
        order.delivery_cost = calculate_delivery_cost(order)
        return order

    def _plan_payments(self, plan: _Plan) -> None:
        """Когда и как клиент платит. План не зависит от часов сборки — только от зерна заявки."""
        if plan.archived_at is not None:
            return
        rng = plan.rng
        regular = plan.client.notes == "Постоянный клиент"
        # Расходы на рынке служба платит сама при заборе — такие заявки клиенты чаще оплачивают заранее.
        fronted = plan.order.market_cube_cost > 0
        share = PREPAID_SHARE_REGULAR if regular else PREPAID_SHARE_MARKET if fronted else PREPAID_SHARE
        roll = rng.random()
        if roll < share:
            plan.pays.append(_PayPlan(PaymentMethod.TRANSFER, None, self._office_time(plan.created_at + timedelta(minutes=rng.randint(5, 90)), rng)))
            return
        method = PaymentMethod.CASH if rng.random() < 0.58 else PaymentMethod.TRANSFER
        recorded = self._office_time(plan.delivered_at + timedelta(minutes=rng.randint(10, 40)), rng)
        roll = rng.random()
        if roll < INVOICE_SHARE:
            later = plan.delivered_at + timedelta(days=rng.randint(1, 6), hours=rng.randint(0, 6))
            plan.pays.append(_PayPlan(PaymentMethod.TRANSFER, None, self._office_time(later, rng)))
        elif roll < INVOICE_SHARE + PARTIAL_SHARE:
            share = Decimal(str(round(rng.uniform(0.4, 0.7), 2)))
            plan.pays.append(_PayPlan(PaymentMethod.CASH, share, recorded))
            later = plan.delivered_at + timedelta(days=rng.randint(1, 4), hours=rng.randint(0, 6))
            plan.pays.append(_PayPlan(PaymentMethod.TRANSFER, share, self._office_time(later, rng), rest=True))
        else:
            plan.pays.append(_PayPlan(method, None, recorded))

    def _office_time(self, moment: datetime, rng: random.Random) -> datetime:
        """Оплату отмечает менеджер в рабочее время; вечером — на следующее утро."""
        local = moment.astimezone(CRM_TIMEZONE)
        if local.time() > OFFICE_CLOSE:
            return self._local(local.date() + timedelta(days=1), time(9, rng.randint(5, 50)))
        if local.time() < OFFICE_OPEN:
            return self._local(local.date(), time(9, rng.randint(5, 50)))
        return moment

    def _apply_clock(self, plan: _Plan) -> None:
        """Состояние заявки на момент сборки: что по плану уже случилось, то и случилось."""
        now, order = self.now, plan.order
        events = [plan.created_at]
        if plan.archived_at is not None and plan.archived_at <= now:
            order.is_archived = True
            order.archived_at = plan.archived_at
            order.archived_by_id = plan.archived_by.id
            events.append(plan.archived_at)
        else:
            if plan.assigned_at <= now:
                order.courier_id = plan.courier.id
                events.append(plan.assigned_at)
            if plan.pickup_at <= now:
                order.courier_id = plan.courier.id
                order.status = OrderStatus.AT_COURIER
                events.append(plan.pickup_at)
            if plan.delivered_at <= now:
                order.status = OrderStatus.DELIVERED
                events.append(plan.delivered_at)
        if plan.edited_at is not None and plan.edited_at <= now:
            events.append(plan.edited_at)
        total = order.delivery_cost
        for index, pay in enumerate(plan.pays):
            if pay.at > now:
                continue
            if pay.share is None:
                amount = total
            else:
                first = _round10(total * pay.share)
                amount = total - first if pay.rest else first
            if amount > 0:
                # Свой генератор на каждое событие: кто отметил оплату, не зависит от часов сборки.
                cashier = self.manager if _event_rng(plan.key, f"cashier{index}").random() < 0.7 else self.managers[-1]
                plan.written_payments.append((amount.quantize(Decimal("0.01")), pay.method, pay.at, cashier))
                events.append(pay.at)
        order.updated_at = max(events)

    def _write_order_history(self, plan: _Plan) -> None:
        order = plan.order
        now = self.now

        def snapshot(status, courier_id, archived=False):
            view = SimpleNamespace(
                **{name: getattr(order, name) for name in SNAPSHOT_FIELDS},
                status=status,
                courier_id=courier_id,
                is_archived=archived,
                archived_at=order.archived_at if archived else None,
                archived_by_id=order.archived_by_id if archived else None,
            )
            return json.dumps(_order_snapshot(view), ensure_ascii=False, sort_keys=True)

        logs: list[OrderChangeLog] = []
        previous = snapshot(OrderStatus.IN_WORK, None)
        logs.append(self._log(order, plan.creator, "создание", None, previous, plan.created_at))
        if plan.edited_at is not None and plan.edited_at <= now:
            logs.append(self._log(order, plan.creator, "редактирование", previous, previous, plan.edited_at))
        if order.is_archived:
            current = snapshot(OrderStatus.IN_WORK, None, archived=True)
            logs.append(self._log(order, plan.archived_by, "архив", previous, current, plan.archived_at))
        else:
            if plan.assigned_at <= now:
                current = snapshot(OrderStatus.IN_WORK, plan.courier.id)
                dispatcher = self.manager if _event_rng(plan.key, "dispatch").random() < 0.75 else self.managers[-1]
                logs.append(self._log(order, dispatcher, "редактирование", previous, current, plan.assigned_at))
                previous = current
            if plan.pickup_at <= now:
                current = snapshot(OrderStatus.AT_COURIER, plan.courier.id)
                logs.append(self._log(order, plan.courier, "у курьера", previous, current, plan.pickup_at))
                previous = current
            if plan.delivered_at <= now:
                current = snapshot(OrderStatus.DELIVERED, plan.courier.id)
                logs.append(self._log(order, plan.courier, "доставлено", previous, current, plan.delivered_at))

        for amount, method, paid_at, cashier in plan.written_payments:
            payment = Payment(
                order_id=order.id,
                amount=amount,
                method=method,
                status=PaymentStatus.PAID,
                paid_at=paid_at,
                created_at=paid_at,
                created_by_id=cashier.id,
            )
            self.db.add(payment)
            payment_json = json.dumps(_payment_snapshot(payment, user=cashier), ensure_ascii=False, sort_keys=True)
            logs.append(self._log(order, cashier, "оплата", None, payment_json, paid_at))
            self.summary.payments += 1
        self.db.add_all(logs)

    # --------------------------------------------------------- сдачи наличных
    def _write_handovers(self, plans: list[_Plan]) -> None:
        """Курьер сдаёт наличные в конце смены; владелец подтверждает вечером или утром.

        Последние 30 дней — по дням, раньше — раз в неделю.
        """
        by_day: dict[tuple[int, date], list[_Plan]] = {}
        for plan in plans:
            if plan.order.is_archived or plan.day > self.today:
                continue
            by_day.setdefault((plan.courier.id, plan.day), []).append(plan)

        couriers = {user.id: user for user, _ in self.couriers}
        weekly: dict[tuple[int, date], list] = {}
        rejected_day = self.today - timedelta(days=12)
        rejected_done = False
        for (courier_id, day), day_plans in sorted(by_day.items(), key=lambda item: (item[0][1], item[0][0])):
            rng = random.Random(f"{RANDOM_SEED}|handover|{day.isoformat()}|{courier_id}")
            last_delivery = max(plan.delivered_at for plan in day_plans)
            sent_at = last_delivery + timedelta(minutes=rng.randint(15, 45))
            if sent_at > self.now:
                continue
            cash = Decimal("0")
            pay = Decimal("0")
            for plan in day_plans:
                recorded = [part for part in plan.written_payments if part[2] <= sent_at]
                if plan.delivered_at > sent_at or not recorded:
                    continue
                cash += sum((part[0] for part in recorded if part[1] == PaymentMethod.CASH), Decimal("0"))
                pay += plan.order.courier_pay
            amount = cash - pay
            if amount <= 0:
                continue
            age = (self.today - day).days
            courier = couriers[courier_id]
            if age > 30:
                week_start = day - timedelta(days=day.weekday())
                weekly.setdefault((courier_id, week_start), []).append((day, amount))
                continue
            if rng.random() < 0.45:
                confirmed_at = sent_at + timedelta(minutes=rng.randint(30, 90))
            else:
                confirmed_at = self._local(day + timedelta(days=1), time(rng.randint(9, 11), rng.randint(0, 59)))
            if not rejected_done and day == rejected_day:
                rejected_done = True
                self._handover(
                    courier, day, day, amount - Decimal("500"), CourierCashHandoverStatus.REJECTED,
                    sent_at - timedelta(minutes=40), min(sent_at - timedelta(minutes=5), self.now),
                    comment="Не сходится с кассой на 500 ₽, пересчитать",
                )
            if confirmed_at <= self.now:
                self._handover(courier, day, day, amount, CourierCashHandoverStatus.CONFIRMED, sent_at, confirmed_at)
            else:
                self._handover(courier, day, day, amount, CourierCashHandoverStatus.PENDING, sent_at, None)

        for (courier_id, week_start), items in sorted(weekly.items(), key=lambda item: (item[0][1], item[0][0])):
            start = min(day for day, _ in items)
            end = max(day for day, _ in items)
            total = sum((amount for _, amount in items), Decimal("0"))
            rng = random.Random(f"{RANDOM_SEED}|weekly|{week_start.isoformat()}|{courier_id}")
            created_at = self._local(end, time(20, rng.randint(0, 50)))
            confirmed_at = self._local(end + timedelta(days=1), time(rng.randint(9, 11), rng.randint(0, 59)))
            self._handover(couriers[courier_id], start, end, total, CourierCashHandoverStatus.CONFIRMED, created_at, confirmed_at)

    def _handover(self, courier, start, end, amount, status, created_at, confirmed_at, comment=None) -> None:
        self.db.add(
            CourierCashHandover(
                courier_id=courier.id,
                period_start=start,
                period_end=end,
                amount=amount.quantize(Decimal("0.01")),
                status=status,
                created_by_id=courier.id,
                confirmed_by_id=self.admin.id if status != CourierCashHandoverStatus.PENDING else None,
                created_at=created_at,
                confirmed_at=confirmed_at,
                comment=comment,
            )
        )
        self.summary.handovers += 1

    # ------------------------------------------------------------- мелочи
    def _log(self, order, user, action, old_value, new_value, created_at) -> OrderChangeLog:
        return OrderChangeLog(
            order_id=order.id,
            user_id=user.id if user else None,
            action=action,
            old_value=old_value,
            new_value=new_value,
            created_at=created_at,
        )

    @staticmethod
    def _order_note(rng: random.Random, address: str) -> str:
        place = HOME_NOTES if ", кв. " in address else OFFICE_NOTES if ", офис " in address else ()
        return rng.choice(ORDER_NOTES + place)

    @staticmethod
    def _cargo_number(rng: random.Random, number: int) -> str:
        # Номер заявки в хвосте делает номер груза уникальным среди активных заявок.
        return f"{CARGO_PREFIX}-{rng.randint(1, 9)}{number:04d}"

    @staticmethod
    def _yandex_url(rng: random.Random) -> str:
        lat = round(rng.uniform(55.57, 55.91), 6)
        lon = round(rng.uniform(37.36, 37.84), 6)
        return f"https://yandex.ru/maps/?pt={lon},{lat}&z=17&l=map"

    def _at(self, day: date, hour: int, minute: int) -> datetime:
        return self._local(day, time(min(hour, 23), min(minute, 59)))

    @staticmethod
    def _local(day: date, moment: time) -> datetime:
        return datetime.combine(day, moment, tzinfo=CRM_TIMEZONE).astimezone(timezone.utc)


def _event_rng(key: str, event: str) -> random.Random:
    return random.Random(f"{RANDOM_SEED}|{key}|{event}")


def _round10(value) -> Decimal:
    return ((Decimal(str(value)) / TEN).quantize(Decimal("1")) * TEN).quantize(Decimal("0.01"))


_HASH_CACHE: dict[tuple[str, str | None], str] = {}


def _password_hash(cache_key: str, password: str | None) -> str:
    """bcrypt медленный: хеш считаем один раз на процесс.

    Без пароля в окружении ставим случайный — вход по паролю для такой учётки закрыт.
    """
    key = (cache_key, password)
    if key not in _HASH_CACHE:
        _HASH_CACHE[key] = hash_password(password or secrets.token_urlsafe(24))
    return _HASH_CACHE[key]

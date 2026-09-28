import os
import unittest
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SECRET_KEY", "test-secret")

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.demo import reset as demo_reset
from app.demo.accounts import DEMO_ACCOUNTS
from app.demo.dataset import BUSINESS_CLIENTS, demo_courier_route_day
from app.demo.reset import ResetTooSoon, data_is_stale, demo_epoch, reset_demo
from app.models.client import Client
from app.models.courier_cash_handover import CourierCashHandover
from app.models.enums import CourierCashHandoverStatus, OrderStatus, UserRole
from app.models.log import OrderChangeLog
from app.models.order import Order
from app.models.payment import Payment
from app.models.user import User
from app.services.accounting_service import get_accounting_report
from app.services.courier_service import get_courier_dashboard
from app.services.dashboard_service import get_dashboard
from app.utils.dates import CRM_TIMEZONE

# Рабочий вторник: все курьеры в смене, день в разгаре.
WEEKDAY = date(2026, 9, 29)


def moscow(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=CRM_TIMEZONE)


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def new_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, autoflush=False)


def build(now: datetime):
    engine, SessionLocal = new_engine()
    demo_reset.state.last_reset = None
    summary = reset_demo(SessionLocal, now=now)
    return engine, SessionLocal, summary


class DemoDatasetTest(unittest.TestCase):
    """Живой мир: 2,5 месяца истории, день в разгаре и планы на ближайшие дни."""

    @classmethod
    def setUpClass(cls):
        cls.today = WEEKDAY
        cls.now = moscow(WEEKDAY, 15, 0)
        cls.engine, cls.SessionLocal, cls.summary = build(cls.now)

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(cls.engine)
        cls.engine.dispose()

    def test_history_covers_two_and_a_half_months_and_the_next_days(self):
        with self.SessionLocal() as db:
            first = db.scalar(select(func.min(Order.delivery_date)))
            last = db.scalar(select(func.max(Order.delivery_date)))

        self.assertEqual(first, self.today - timedelta(days=74))
        self.assertGreaterEqual(last, self.today + timedelta(days=1))
        self.assertGreater(self.summary.orders, 900)
        self.assertGreater(self.summary.payments, 800)
        self.assertGreater(self.summary.handovers, 60)

    def test_weekdays_are_busier_than_weekends(self):
        with self.SessionLocal() as db:
            rows = db.execute(
                select(Order.delivery_date, func.count())
                .where(Order.delivery_date < self.today)
                .group_by(Order.delivery_date)
            ).all()
        by_kind = {"weekday": [], "weekend": []}
        for day, count in rows:
            by_kind["weekend" if day.weekday() >= 5 else "weekday"].append(count)
        weekday_avg = sum(by_kind["weekday"]) / len(by_kind["weekday"])
        weekend_avg = sum(by_kind["weekend"]) / len(by_kind["weekend"])

        self.assertGreater(weekday_avg, weekend_avg * 1.8)

    def test_demo_accounts_exist_with_their_roles(self):
        with self.SessionLocal() as db:
            for account in DEMO_ACCOUNTS.values():
                user = db.scalar(select(User).where(User.email == account.email))
                with self.subTest(account=account.key):
                    self.assertIsNotNone(user)
                    self.assertEqual(user.role, account.role)
                    self.assertTrue(user.is_active)
            active_couriers = db.scalar(
                select(func.count()).select_from(User).where(User.role == UserRole.COURIER, User.is_active.is_(True))
            )
            inactive = db.scalar(select(func.count()).select_from(User).where(User.is_active.is_(False)))

        self.assertGreaterEqual(active_couriers, 5)
        self.assertEqual(inactive, 1)

    def test_every_courier_on_shift_has_a_whole_day_route(self):
        with self.SessionLocal() as db:
            orders = db.scalars(
                select(Order).where(Order.delivery_date == self.today, Order.is_archived.is_(False))
            ).all()
        routes: dict[int, Counter] = {}
        for order in orders:
            if order.courier_id is not None:
                routes.setdefault(order.courier_id, Counter())[order.status] += 1

        self.assertGreaterEqual(len(routes), 4)
        for courier_id, statuses in routes.items():
            with self.subTest(courier=courier_id):
                # До текущего часа доставлено, одна-две заявки в пути, остальное запланировано.
                self.assertGreaterEqual(statuses[OrderStatus.DELIVERED], 1)
                self.assertLessEqual(statuses[OrderStatus.AT_COURIER], 2)
        in_transit = sum(statuses[OrderStatus.AT_COURIER] for statuses in routes.values())
        planned = sum(statuses[OrderStatus.IN_WORK] for statuses in routes.values())
        self.assertGreaterEqual(in_transit, 3)
        self.assertGreaterEqual(planned, 2)

    def test_the_day_is_spread_over_working_hours_not_squeezed(self):
        with self.SessionLocal() as db:
            delivered = [
                utc(moment).astimezone(CRM_TIMEZONE)
                for moment in db.scalars(
                    select(OrderChangeLog.created_at)
                    .join(Order)
                    .where(OrderChangeLog.action == "доставлено", Order.delivery_date == self.today)
                ).all()
            ]
        self.assertTrue(delivered)
        self.assertLessEqual(min(delivered).hour, 11)
        self.assertGreaterEqual(max(delivered) - min(delivered), timedelta(hours=3))
        self.assertLessEqual(max(delivered), self.now)

    def test_past_days_are_finished(self):
        with self.SessionLocal() as db:
            unfinished = db.scalar(
                select(func.count()).select_from(Order).where(
                    Order.delivery_date < self.today,
                    Order.is_archived.is_(False),
                    Order.status != OrderStatus.DELIVERED,
                )
            )
        self.assertEqual(unfinished, 0)

    def test_attention_is_a_few_plausible_items(self):
        with self.SessionLocal() as db:
            admin = db.scalar(select(User).where(User.email == DEMO_ACCOUNTS["admin"].email))
            dashboard = get_dashboard(db, admin, today=self.today)
        self.assertLessEqual(len(dashboard.attention), 3)
        for item in dashboard.attention:
            with self.subTest(item=item.key):
                self.assertLessEqual(item.count, 6)

    def test_demo_courier_route_has_work_to_do_now(self):
        with self.SessionLocal() as db:
            courier = db.scalar(select(User).where(User.email == DEMO_ACCOUNTS["courier"].email))
            dashboard = get_courier_dashboard(db, courier, today=self.today, mode="date", detail="assigned")
            statuses = Counter(order.status for order in dashboard.period_orders)

        self.assertGreaterEqual(statuses[OrderStatus.DELIVERED], 2)
        self.assertGreaterEqual(statuses[OrderStatus.AT_COURIER] + statuses[OrderStatus.IN_WORK], 2)
        self.assertGreater(dashboard.money.cash_total + dashboard.money.paid_total, Decimal("0"))

    def test_accounting_has_money_for_every_recent_day_and_profit(self):
        with self.SessionLocal() as db:
            day = get_accounting_report(db, period="day", selected_date=self.today - timedelta(days=1))
            month = get_accounting_report(db, period="month", selected_date=self.today)

        self.assertGreater(day.summary.orders_count, 0)
        self.assertGreater(month.summary.paid_total, Decimal("0"))
        self.assertGreater(month.summary.market_expenses_total, Decimal("0"))
        self.assertGreater(month.summary.courier_pay_total, Decimal("0"))
        self.assertGreater(month.summary.profit_total, Decimal("0"))
        self.assertGreater(month.expenses.kara, Decimal("0"))

    def test_orders_are_consistent_with_crm_rules(self):
        with self.SessionLocal() as db:
            orders = db.scalars(select(Order)).all()
            codes = [order.order_code for order in orders]
            active_cargo = [
                (order.cargo_number or "").casefold()
                for order in orders
                if order.cargo_number and not order.is_archived
            ]
            for order in orders[:300]:
                expected = sum(
                    (
                        order.base_delivery_cost,
                        order.market_cube_cost,
                        order.market_loader_cost,
                        order.market_storage_cost,
                        order.market_kara_cost,
                        order.market_other_cost,
                    ),
                    Decimal("0"),
                )
                self.assertEqual(order.delivery_cost, expected)
                self.assertEqual(order.client_name_snapshot, order.client.full_name)
                if order.status in (OrderStatus.AT_COURIER, OrderStatus.DELIVERED):
                    self.assertIsNotNone(order.courier_id)
                paid = sum((p.amount for p in order.payments), Decimal("0"))
                self.assertLessEqual(paid, order.delivery_cost)
                self.assertLessEqual(utc(order.client.created_at), utc(order.created_at))
            logs = db.scalars(select(OrderChangeLog.action)).all()

        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(len(active_cargo), len(set(active_cargo)))
        for action in ("создание", "у курьера", "доставлено", "оплата", "архив"):
            self.assertIn(action, logs)

    def test_addresses_look_like_real_ones(self):
        """У организаций офис или только дом, у людей своя квартира; дом не повторяется у разных клиентов,
        номер квартиры — у разных адресов; в один день два курьера не едут по одному адресу."""
        with self.SessionLocal() as db:
            orders = db.scalars(select(Order)).all()
            clients = {client.id: client for client in db.scalars(select(Client)).all()}

        business = {name for name, _ in BUSINESS_CLIENTS}
        houses: dict[str, int] = {}
        flats: dict[str, str] = {}
        by_day: dict[date, list[Order]] = {}
        for order in orders:
            client = clients[order.client_id]
            address = order.address
            if client.full_name in business:
                self.assertNotIn("кв.", address, client.full_name)
            else:
                self.assertNotIn("офис", address, client.full_name)
            house = address.split(", офис")[0].split(", под.")[0].split(", кв.")[0]
            self.assertEqual(houses.setdefault(house, client.id), client.id, f"{house}: два клиента")
            if ", кв. " in address:
                flat = address.rsplit(", кв. ", 1)[1]
                self.assertEqual(flats.setdefault(flat, house), house, f"кв. {flat} у двух адресов")
            by_day.setdefault(order.delivery_date, []).append(order)
        for day, day_orders in by_day.items():
            addresses = [order.address for order in day_orders]
            self.assertEqual(len(addresses), len(set(addresses)), f"{day}: один адрес дважды")

    def test_names_are_written_as_in_russian(self):
        """«Интернет-магазин «Полка»», а не «Интернет-Магазин Полка»: вид дела строчными, название в ёлочках."""
        for name, _ in BUSINESS_CLIENTS:
            if name.startswith("ИП "):
                self.assertRegex(name, r"^ИП [А-ЯЁ][а-яё]+ [А-ЯЁ]\. [А-ЯЁ]\.$")
                continue
            self.assertRegex(name, r"«[^«»]+»$", name)
            kind = name.split("«")[0].split()
            self.assertTrue(kind, name)
            for word in kind[1:]:
                self.assertTrue(word.islower() or word == "ООО", name)
        with self.SessionLocal() as db:
            people = [c.full_name for c in db.scalars(select(Client)).all() if c.full_name not in {n for n, _ in BUSINESS_CLIENTS}]
        self.assertEqual(len(people), len(set(people)), "одинаковые имена у разных клиентов")

    def test_cargo_numbers_are_neutral(self):
        """Номер груза без букв городов, складов и кодов клиентов — в духе подсказки формы («GR-24115»)."""
        with self.SessionLocal() as db:
            numbers = db.scalars(select(Order.cargo_number).where(Order.cargo_number.is_not(None))).all()
        self.assertGreater(len(numbers), 100)
        for number in numbers:
            self.assertRegex(number, r"^GR-\d{5}$")

    def test_fresh_data_is_not_stale_and_has_an_epoch(self):
        with self.SessionLocal() as db:
            self.assertFalse(data_is_stale(db, now=self.now))
            self.assertTrue(demo_epoch(db))


class DemoClockTest(unittest.TestCase):
    """Мир детерминирован по дате: в течение дня двигается только граница сделанного."""

    @classmethod
    def setUpClass(cls):
        cls.builds = {}
        for moment in ((3, 20), (10, 0), (15, 0), (21, 30)):
            engine, SessionLocal, _ = build(moscow(WEEKDAY, *moment))
            with SessionLocal() as db:
                moments = []
                for column in (
                    Order.created_at, Order.updated_at, Order.archived_at, OrderChangeLog.created_at,
                    Payment.paid_at, Payment.created_at, CourierCashHandover.created_at,
                    CourierCashHandover.confirmed_at, Client.created_at,
                ):
                    moments += [utc(value) for value in db.scalars(select(column)).all() if value is not None]
                orders = {
                    order.order_code: (order.delivery_date, order.client_name_snapshot, order.delivery_cost, order.status)
                    for order in db.scalars(select(Order)).all()
                }
                handovers = Counter(db.scalars(select(CourierCashHandover.status)).all())
            Base.metadata.drop_all(engine)
            engine.dispose()
            cls.builds[moment] = {"now": moscow(WEEKDAY, *moment), "moments": moments, "orders": orders, "handovers": handovers}

    def test_no_event_is_later_than_the_moment_of_build(self):
        for moment, build_data in self.builds.items():
            with self.subTest(moment=moment):
                self.assertLessEqual(max(build_data["moments"]), build_data["now"])

    def test_orders_keep_their_codes_and_only_move_forward(self):
        rank = {OrderStatus.IN_WORK: 0, OrderStatus.AT_COURIER: 1, OrderStatus.DELIVERED: 2}
        moments = sorted(self.builds)
        for earlier, later in zip(moments, moments[1:]):
            before, after = self.builds[earlier]["orders"], self.builds[later]["orders"]
            with self.subTest(earlier=earlier, later=later):
                # Всё, что было утром, есть и днём: тот же номер, клиент, дата и сумма, статус не откатывается.
                self.assertTrue(set(before) <= set(after))
                for code, (day, client, cost, status) in before.items():
                    self.assertEqual(after[code][:3], (day, client, cost))
                    self.assertGreaterEqual(rank[after[code][3]], rank[status])

    def test_rebuild_at_the_same_moment_gives_the_same_world(self):
        engine, SessionLocal, _ = build(moscow(WEEKDAY, 10, 0))
        with SessionLocal() as db:
            orders = {
                order.order_code: (order.delivery_date, order.client_name_snapshot, order.delivery_cost, order.status)
                for order in db.scalars(select(Order)).all()
            }
        engine.dispose()
        self.assertEqual(orders, self.builds[(10, 0)]["orders"])

    def test_night_before_work_everything_today_is_still_planned(self):
        today = [value for value in self.builds[(3, 20)]["orders"].values() if value[0] == WEEKDAY]
        self.assertTrue(today)
        self.assertTrue(all(status == OrderStatus.IN_WORK for _, _, _, status in today))
        yesterday = [value for value in self.builds[(3, 20)]["orders"].values() if value[0] == WEEKDAY - timedelta(days=1)]
        self.assertTrue(yesterday)

    def test_late_evening_the_day_is_complete_and_cash_is_handed_over(self):
        today = [value for value in self.builds[(21, 30)]["orders"].values() if value[0] == WEEKDAY]
        statuses = Counter(status for _, _, _, status in today)
        self.assertEqual(statuses[OrderStatus.AT_COURIER], 0)
        self.assertGreaterEqual(statuses[OrderStatus.DELIVERED], len(today) - 2)
        handovers = self.builds[(21, 30)]["handovers"]
        self.assertEqual(handovers[CourierCashHandoverStatus.REJECTED], 1)
        self.assertGreater(handovers[CourierCashHandoverStatus.CONFIRMED], 50)
        self.assertGreaterEqual(handovers[CourierCashHandoverStatus.PENDING], 1)

    def test_guest_courier_always_has_something_to_pick_up(self):
        # Вечер будня и субботы, до и после перехода сценария на завтра: в маршруте гостя есть «Забрал».
        saturday = WEEKDAY + timedelta(days=4)
        for moment in (moscow(WEEKDAY, 19, 30), moscow(WEEKDAY, 19, 50), moscow(WEEKDAY, 23, 40),
                       moscow(saturday, 16, 45), moscow(saturday, 17, 20), moscow(saturday + timedelta(days=1), 17, 30)):
            with self.subTest(moment=moment.isoformat()):
                engine, SessionLocal, _ = build(moment)
                with SessionLocal() as db:
                    courier = db.scalar(select(User).where(User.email == DEMO_ACCOUNTS["courier"].email))
                    waiting = db.scalar(
                        select(func.count()).select_from(Order).where(
                            Order.courier_id == courier.id,
                            Order.delivery_date == demo_courier_route_day(moment),
                            Order.status == OrderStatus.IN_WORK,
                            Order.is_archived.is_(False),
                        )
                    )
                Base.metadata.drop_all(engine)
                engine.dispose()
                self.assertGreaterEqual(waiting, 1)

    def test_demo_courier_scenario_moves_to_tomorrow_after_the_shift(self):
        self.assertEqual(demo_courier_route_day(moscow(WEEKDAY, 12, 0)), WEEKDAY)
        self.assertEqual(demo_courier_route_day(moscow(WEEKDAY, 21, 0)), WEEKDAY + timedelta(days=1))
        self.assertEqual(demo_courier_route_day(moscow(WEEKDAY, 6, 0)), WEEKDAY)


class DemoResetTest(unittest.TestCase):
    def setUp(self):
        self.engine, self.SessionLocal = new_engine()
        demo_reset.state.last_reset = None

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_reset_is_repeatable_and_wipes_visitor_changes(self):
        now = moscow(WEEKDAY, 12, 0)
        first = reset_demo(self.SessionLocal, now=now)
        with self.SessionLocal() as db:
            order = db.scalar(select(Order).order_by(Order.id.desc()))
            order.address = "Москва, ул. Гостевая, 1"
            db.add(Client(full_name="Гость Витрины", phone="+7 900 000-00-01"))
            db.commit()
            admin_id = db.scalar(select(User.id).where(User.email == DEMO_ACCOUNTS["admin"].email))

        demo_reset.state.last_reset = None
        second = reset_demo(self.SessionLocal, now=now)

        with self.SessionLocal() as db:
            self.assertIsNone(db.scalar(select(Client).where(Client.full_name == "Гость Витрины")))
            self.assertIsNone(db.scalar(select(Order).where(Order.address == "Москва, ул. Гостевая, 1")))
            self.assertEqual(
                db.scalar(select(User.id).where(User.email == DEMO_ACCOUNTS["admin"].email)),
                admin_id,
            )
            counts = (
                db.scalar(select(func.count()).select_from(Order)),
                db.scalar(select(func.count()).select_from(Payment)),
            )
        self.assertEqual((first.orders, first.payments), (second.orders, second.payments))
        self.assertEqual(counts, (second.orders, second.payments))

    def test_manual_reset_has_a_short_cooldown(self):
        reset_demo(self.SessionLocal, manual=True)

        with self.assertRaises(ResetTooSoon):
            reset_demo(self.SessionLocal, manual=True)

    def test_data_from_previous_day_is_stale(self):
        reset_demo(self.SessionLocal)
        tomorrow = datetime.now(CRM_TIMEZONE) + timedelta(days=1)
        with self.SessionLocal() as db:
            self.assertTrue(data_is_stale(db, now=tomorrow))

    def test_passwords_come_only_from_environment(self):
        with patch.dict(os.environ, {"DEMO_MANAGER_PASSWORD": "from-env-123"}, clear=False):
            os.environ.pop("DEMO_ADMIN_PASSWORD", None)
            reset_demo(self.SessionLocal)
        from app.utils.security import verify_password

        with self.SessionLocal() as db:
            manager = db.scalar(select(User).where(User.email == DEMO_ACCOUNTS["manager"].email))
            admin = db.scalar(select(User).where(User.email == DEMO_ACCOUNTS["admin"].email))
            self.assertTrue(verify_password("from-env-123", manager.password_hash))
            self.assertFalse(verify_password("admin123", admin.password_hash))


class DemoRebuildRulesTest(unittest.TestCase):
    """Когда фоновая проверка пересобирает мир сама."""

    def setUp(self):
        self.engine, self.SessionLocal = new_engine()

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def built(self, moment: datetime):
        with self.SessionLocal() as db:
            account = DEMO_ACCOUNTS["admin"]
            # Время в базе — в UTC, как пишет приложение.
            db.add(User(email=account.email, full_name=account.full_name, role=account.role,
                        password_hash="x", created_at=utc(moment)))
            db.commit()

    def visitor_changed(self, moment: datetime):
        """Гость что-то поменял: в журнале заявки появилась запись."""
        with self.SessionLocal() as db:
            client = Client(full_name="Клиент Гостя", phone="+7 900 000-00-02", created_at=utc(moment) - timedelta(days=5))
            db.add(client)
            db.flush()
            order = Order(
                order_series="ХД", order_number=1, order_code="ХД-0001", client_id=client.id,
                client_name_snapshot=client.full_name, client_phone_snapshot=client.phone,
                address="Москва", status=OrderStatus.IN_WORK, created_at=utc(moment) - timedelta(days=1),
            )
            db.add(order)
            db.flush()
            db.add(OrderChangeLog(order_id=order.id, action="редактирование", created_at=utc(moment)))
            db.commit()

    def reason(self, now):
        with self.SessionLocal() as db:
            return demo_reset.rebuild_reason(db, now=now)

    def test_missing_world_is_built_at_once(self):
        self.assertEqual(self.reason(moscow(WEEKDAY, 10)), "нет данных")

    def test_full_rebuild_every_morning_at_six(self):
        self.built(moscow(WEEKDAY - timedelta(days=1), 23, 50))
        self.visitor_changed(moscow(WEEKDAY, 5, 58))
        self.assertIsNone(self.reason(moscow(WEEKDAY, 5, 59)))
        # В 06:00 — даже если гость только что работал.
        self.assertEqual(self.reason(moscow(WEEKDAY, 6, 1)), "утренняя пересборка")

    def test_quiet_rebuild_when_world_is_old_and_nobody_changes_anything(self):
        self.built(moscow(WEEKDAY, 7, 0))
        self.assertIsNone(self.reason(moscow(WEEKDAY, 7, 50)))
        self.assertEqual(self.reason(moscow(WEEKDAY, 8, 5)), "мир отстал от часов")

    def test_visitor_changes_postpone_the_quiet_rebuild(self):
        self.built(moscow(WEEKDAY, 7, 0))
        self.visitor_changed(moscow(WEEKDAY, 8, 50))
        self.assertIsNone(self.reason(moscow(WEEKDAY, 9, 0)))
        self.assertEqual(self.reason(moscow(WEEKDAY, 9, 11)), "мир отстал от часов")

    def test_last_change_is_read_from_the_database(self):
        self.built(moscow(WEEKDAY, 7, 0))
        self.visitor_changed(moscow(WEEKDAY, 8, 50))
        with self.SessionLocal() as db:
            self.assertEqual(demo_reset.last_change_at(db), moscow(WEEKDAY, 8, 50))


if __name__ == "__main__":
    unittest.main()

"""Автопилот витрины: полгода без присмотра.

Пересборка мира (потолок 6 часов), вкладки прежнего мира, полный сброс внесённого, /health, запрет индексации,
демо-входы, которые нельзя сломать, безопасный вывод текста посетителей, закреплённые версии и контейнеры,
которые поднимаются сами.
"""

import os
import re
import unittest
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SECRET_KEY", "test-secret")

import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.demo import reset as demo_reset
from app.demo import scenarios
from app.demo.accounts import DEMO_ACCOUNTS
from app.demo.router import get_session_factory
from app.demo.world import DATA_REFRESHED, SESSION_KEY, world
from app.main import app
from app.models.client import Client
from app.models.courier_cash_handover import CourierCashHandover
from app.models.enums import CourierCashHandoverStatus, OrderStatus, PaymentMethod, PaymentStatus, UserRole
from app.models.log import OrderChangeLog
from app.models.order import Order
from app.models.payment import Payment
from app.models.user import User
from app.ui import login_preview
from app.utils.dates import CRM_TIMEZONE, crm_today

ROOT = Path(__file__).resolve().parent.parent
WEEKDAY = date(2026, 9, 29)


def moscow(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=CRM_TIMEZONE)


def utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def new_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, autoflush=False)


class CeilingRebuildTest(unittest.TestCase):
    """Мир не старше 6 часов, даже если в витрине всё время кто-то есть."""

    def setUp(self):
        self.engine, self.SessionLocal = new_engine()
        # Мир собран в 07:00; гость меняет что-то каждые 10 минут — тихая пересборка не наступает.
        with self.SessionLocal() as db:
            account = DEMO_ACCOUNTS["admin"]
            db.add(User(email=account.email, full_name=account.full_name, role=account.role, password_hash="x",
                        created_at=utc(moscow(WEEKDAY, 7, 0))))
            client = Client(full_name="Клиент", phone="+7 900 000-00-02", created_at=utc(moscow(WEEKDAY, 6, 30)))
            db.add(client)
            db.flush()
            order = Order(order_series="ХД", order_number=1, order_code="ХД-0001", client_id=client.id,
                          client_name_snapshot=client.full_name, client_phone_snapshot=client.phone, address="Москва",
                          status=OrderStatus.IN_WORK, created_at=utc(moscow(WEEKDAY, 6, 30)))
            db.add(order)
            db.commit()
            self.order_id = order.id

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def changed(self, moment: datetime):
        with self.SessionLocal() as db:
            db.add(OrderChangeLog(order_id=self.order_id, action="редактирование", created_at=utc(moment)))
            db.commit()

    def reason(self, now, activity=None):
        with self.SessionLocal() as db:
            return demo_reset.rebuild_reason(db, now=now, activity=activity)

    def test_busy_showcase_is_not_rebuilt_before_six_hours(self):
        self.changed(moscow(WEEKDAY, 12, 55))
        self.assertIsNone(self.reason(moscow(WEEKDAY, 13, 0), activity=moscow(WEEKDAY, 12, 59)))

    def test_after_six_hours_it_waits_for_five_quiet_minutes(self):
        self.changed(moscow(WEEKDAY, 12, 58))
        # 13:01 — мир старше 6 часов, но посетитель только что открывал страницу: ждём.
        self.assertIsNone(self.reason(moscow(WEEKDAY, 13, 1), activity=moscow(WEEKDAY, 13, 0)))
        # 13:06 — 5 минут тишины (ни изменений, ни страниц): пересборка.
        self.assertEqual(self.reason(moscow(WEEKDAY, 13, 6), activity=moscow(WEEKDAY, 13, 0)), "мир старше 6 часов")

    def test_page_views_count_as_activity_for_the_ceiling(self):
        self.changed(moscow(WEEKDAY, 12, 50))
        self.assertIsNone(self.reason(moscow(WEEKDAY, 13, 2), activity=moscow(WEEKDAY, 12, 59)))

    def test_it_does_not_wait_longer_than_thirty_minutes(self):
        self.changed(moscow(WEEKDAY, 13, 29))
        self.assertIsNone(self.reason(moscow(WEEKDAY, 13, 29), activity=moscow(WEEKDAY, 13, 29)))
        self.assertEqual(
            self.reason(moscow(WEEKDAY, 13, 31), activity=moscow(WEEKDAY, 13, 30)),
            "мир старше 6 часов, тишины не дождались",
        )

    def test_checks_run_every_minute(self):
        self.assertEqual(demo_reset.CHECK_INTERVAL_SECONDS, 60)
        self.assertEqual(demo_reset.CEILING_AGE, timedelta(hours=6))
        self.assertEqual(demo_reset.CEILING_QUIET, timedelta(minutes=5))
        self.assertEqual(demo_reset.CEILING_MAX_WAIT, timedelta(minutes=30))


class ResetWipesEverythingTest(unittest.TestCase):
    """Сброс удаляет всё, что внесли посетители, во всех таблицах CRM; других таблиц с данными нет."""

    def setUp(self):
        self.engine, self.SessionLocal = new_engine()
        demo_reset.state.last_reset = None

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def counts(self):
        with self.SessionLocal() as db:
            return {model.__tablename__: db.scalar(select(func.count()).select_from(model))
                    for model in demo_reset.TABLES_IN_DELETE_ORDER}

    def test_every_table_returns_to_the_built_world(self):
        now = moscow(WEEKDAY, 12, 0)
        demo_reset.reset_demo(self.SessionLocal, now=now)
        built = self.counts()
        with self.SessionLocal() as db:
            guest = User(email="guest@example.com", full_name="Гость", role=UserRole.MANAGER, password_hash="x")
            client = Client(full_name="Гость Витрины", phone="+7 900 111-22-33")
            db.add_all([guest, client])
            db.flush()
            courier = db.scalar(select(User).where(User.email == DEMO_ACCOUNTS["courier"].email))
            order = Order(order_series="ХД", order_number=9000, order_code="ХД-9000", client_id=client.id,
                          client_name_snapshot=client.full_name, client_phone_snapshot=client.phone,
                          address="<b>Гостевая</b>", status=OrderStatus.DELIVERED, courier_id=courier.id,
                          delivery_cost=Decimal("500.00"))
            db.add(order)
            db.flush()
            db.add(Payment(order_id=order.id, amount=Decimal("500.00"), method=PaymentMethod.CASH,
                           status=PaymentStatus.PAID))
            db.add(OrderChangeLog(order_id=order.id, action="создание"))
            db.add(CourierCashHandover(courier_id=courier.id, amount=Decimal("100.00"), period_start=WEEKDAY,
                                       period_end=WEEKDAY, status=CourierCashHandoverStatus.PENDING))
            db.commit()
        self.assertNotEqual(self.counts(), built)

        demo_reset.state.last_reset = None
        demo_reset.reset_demo(self.SessionLocal, now=now)

        self.assertEqual(self.counts(), built)
        with self.SessionLocal() as db:
            self.assertIsNone(db.scalar(select(User).where(User.email == "guest@example.com")))
            self.assertIsNone(db.scalar(select(Order).where(Order.order_code == "ХД-9000")))

    def test_all_data_tables_are_wiped(self):
        # Новая таблица с данными посетителей должна попасть в сброс: иначе она росла бы полгода.
        data_tables = {table for table in Base.metadata.tables}
        self.assertEqual(data_tables, {model.__tablename__ for model in demo_reset.TABLES_IN_DELETE_ORDER})

    def test_rebuild_returns_space_to_the_database(self):
        source = (ROOT / "app/demo/reset.py").read_text(encoding="utf-8")
        self.assertIn("VACUUM (ANALYZE)", source)
        self.assertIn('isolation_level="AUTOCOMMIT"', source)

    def test_rebuild_is_atomic_and_single(self):
        source = (ROOT / "app/demo/reset.py").read_text(encoding="utf-8")
        # Одна транзакция и блокировка в базе: оборванная пересборка откатывается, вторая не начинается.
        self.assertIn("pg_try_advisory_xact_lock", source)
        self.assertLess(source.index("_wipe(db)"), source.index("db.commit()"))

    def test_rebuild_sets_the_world_version(self):
        demo_reset.reset_demo(self.SessionLocal, now=moscow(WEEKDAY, 12, 0))
        with self.SessionLocal() as db:
            self.assertEqual(world.version, demo_reset.demo_epoch(db))


class AppTestBase(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"DEMO_MODE": "true"})
        self.env.start()
        self.engine, self.SessionLocal = new_engine()
        demo_reset.state.last_reset = None
        scenarios.set_epoch("")
        login_preview.forget()
        demo_reset.reset_demo(self.SessionLocal, now=datetime.now(CRM_TIMEZONE))

        def override_get_db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_session_factory] = lambda: self.SessionLocal
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.env.stop()

    def login(self, role):
        response = self.client.post(f"/demo/login/{role}", follow_redirects=False)
        self.assertEqual(response.status_code, 303)


class OpenTabsAfterRebuildTest(AppTestBase):
    """Вкладка прежнего мира не падает и не меняет чужую заявку: стартовый экран и «Данные обновлены»."""

    def rebuild_elsewhere(self):
        # Мир пересобрала фоновая проверка: у этой сессии версия прежняя.
        world.set_version("2099-01-01T00:00:00")

    def some_order_id(self):
        with self.SessionLocal() as db:
            return db.scalar(select(Order.id).where(Order.status == OrderStatus.IN_WORK).order_by(Order.id))

    def test_page_of_the_old_world_leads_to_the_start_screen(self):
        self.login("manager")
        self.rebuild_elsewhere()

        response = self.client.get("/orders", headers={"accept": "text/html"}, follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/dashboard")
        page = self.client.get("/dashboard", headers={"accept": "text/html"})
        self.assertEqual(page.status_code, 200)
        self.assertIn(DATA_REFRESHED, page.text)
        # Дальше — обычная работа, без повторной подписи.
        again = self.client.get("/orders", headers={"accept": "text/html"}, follow_redirects=False)
        self.assertEqual(again.status_code, 200)

    def test_form_of_the_old_world_is_not_applied(self):
        self.login("manager")
        order_id = self.some_order_id()
        self.rebuild_elsewhere()

        response = self.client.post(f"/orders/{order_id}/status", data={"status": "at_courier"}, follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/dashboard")
        with self.SessionLocal() as db:
            self.assertEqual(db.get(Order, order_id).status, OrderStatus.IN_WORK)

    def test_script_request_of_the_old_world_gets_409(self):
        self.login("courier")
        self.rebuild_elsewhere()

        response = self.client.post("/courier/orders/1/delivered", headers={"x-requested-with": "fetch"})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["message"], DATA_REFRESHED)

    def test_courier_lands_on_the_route(self):
        self.login("courier")
        self.rebuild_elsewhere()
        response = self.client.get("/courier/orders/1", headers={"accept": "text/html"}, follow_redirects=False)
        self.assertEqual(response.headers["location"], "/courier")

    def test_start_screen_itself_just_shows_the_note(self):
        self.login("admin")
        self.rebuild_elsewhere()
        response = self.client.get("/dashboard", headers={"accept": "text/html"}, follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        self.assertIn(DATA_REFRESHED, response.text)

    def test_fresh_login_and_anonymous_pages_are_not_touched(self):
        self.assertEqual(self.client.get("/login").status_code, 200)
        self.login("admin")
        self.assertEqual(self.client.get("/orders", follow_redirects=False).status_code, 200)

    def test_manual_reset_does_not_show_the_note_twice(self):
        self.login("admin")
        demo_reset.state.last_reset = None
        response = self.client.post("/demo/reset", follow_redirects=False)
        self.assertEqual(response.headers["location"], "/dashboard")
        page = self.client.get("/dashboard")
        self.assertEqual(page.text.count(DATA_REFRESHED), 1)

    def test_rebuild_during_a_request_is_a_soft_return_not_an_error(self):
        # Заявка создавалась, а мир как раз пересобрали: клиент прежнего мира исчез, база отказала.
        from sqlalchemy.exc import IntegrityError

        self.login("manager")

        def create_during_rebuild(*args, **kwargs):
            world.set_version("2099-01-01T00:00:00")
            raise IntegrityError("INSERT INTO orders", {}, Exception("orders_client_id_fkey"))

        with patch("app.routers.orders.create_order", create_during_rebuild):
            response = self.client.post("/orders", data={"client_name": "Гость"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/dashboard")
        self.assertIn(DATA_REFRESHED, self.client.get("/dashboard").text)

        # То же из скрипта: 409, и скрипт отправит форму обычным путём.
        def fetch_during_rebuild(*args, **kwargs):
            world.set_version("2099-02-02T00:00:00")
            raise IntegrityError("INSERT INTO orders", {}, Exception("orders_client_id_fkey"))

        with patch("app.routers.orders.create_order", fetch_during_rebuild):
            fetch = self.client.post("/orders", data={"client_name": "Гость"}, headers={"x-requested-with": "fetch"},
                                     follow_redirects=False)
        self.assertEqual(fetch.status_code, 409)

    def test_ordinary_errors_still_surface(self):
        self.login("manager")

        def broken(*args, **kwargs):
            raise RuntimeError("не связано с пересборкой")

        with patch("app.routers.orders.create_order", broken):
            client = TestClient(app, raise_server_exceptions=False)
            client.cookies = self.client.cookies
            response = client.post("/orders", data={"client_name": "Гость"}, follow_redirects=False)
        self.assertEqual(response.status_code, 500)

    def test_scripts_fall_back_to_the_form_on_409(self):
        # Скрипты курьера при ошибке отправляют форму обычным путём — она и приводит на стартовый экран.
        dashboard = (ROOT / "app/templates/couriers/dashboard.html").read_text(encoding="utf-8")
        order = (ROOT / "app/templates/couriers/order.html").read_text(encoding="utf-8")
        for template in (dashboard, order):
            self.assertIn("HTMLFormElement.prototype.submit.call(form)", template)


class HealthAndIndexingTest(AppTestBase):
    def test_health_is_ok_when_app_and_database_are(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.client.head("/health").status_code, 200)

    def test_health_fails_when_database_does_not_answer(self):
        def broken_db():
            db = self.SessionLocal()
            db.execute = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("база недоступна"))
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = broken_db
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "error")

    def test_health_answers_quickly_when_database_hangs(self):
        import time as clock

        from app import main

        def hanging_db():
            db = self.SessionLocal()
            db.execute = lambda *args, **kwargs: clock.sleep(2)
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = hanging_db
        with patch.object(main, "HEALTH_TIMEOUT_SECONDS", 0.5):
            started = clock.monotonic()
            response = self.client.get("/health")
            elapsed = clock.monotonic() - started
        self.assertEqual(response.status_code, 503)
        self.assertLess(elapsed, 1.5)
        self.assertLessEqual(main.HEALTH_TIMEOUT_SECONDS, 3)

    def test_health_fails_when_the_world_is_missing(self):
        with self.SessionLocal() as db:
            db.query(User).filter(User.email == DEMO_ACCOUNTS["admin"].email).delete()
            db.commit()
        self.assertEqual(self.client.get("/health").status_code, 503)

    def test_search_engines_are_told_not_to_index(self):
        robots = self.client.get("/robots.txt")
        self.assertEqual(robots.status_code, 200)
        self.assertEqual(robots.text, "User-agent: *\nDisallow: /\n")
        self.assertIn('<meta name="robots" content="noindex, nofollow">', self.client.get("/login").text)
        standalone = (ROOT / "app/templates/errors/_standalone.html").read_text(encoding="utf-8")
        self.assertIn('<meta name="robots" content="noindex, nofollow">', standalone)
        caddy = (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
        self.assertIn('X-Robots-Tag "noindex, nofollow"', caddy)


class DemoCannotBeBrokenTest(AppTestBase):
    """Что бы посетитель ни сделал, вход в один клик работает у всех трёх ролей."""

    def test_demo_accounts_survive_every_attempt(self):
        self.login("admin")
        for key, account in DEMO_ACCOUNTS.items():
            with self.SessionLocal() as db:
                user = db.scalar(select(User).where(User.email == account.email))
            self.client.post(f"/users/{user.id}/edit", data={
                "full_name": "Взломщик", "phone": "", "role": "courier" if key != "courier" else "admin",
                "password": "hacked-123", "is_active": "",
            })
        # Учётку с логином демо-входа не создать, удалить сотрудника нельзя вовсе.
        duplicate = self.client.post("/users", data={
            "full_name": "Двойник", "email": DEMO_ACCOUNTS["admin"].email, "password": "x", "role": "admin",
            "is_active": "on",
        })
        self.assertEqual(duplicate.status_code, 400)
        self.assertFalse([route for route in app.routes if "delete" in getattr(route, "path", "") and "/users" in route.path])

        for key, account in DEMO_ACCOUNTS.items():
            with self.SessionLocal() as db:
                user = db.scalar(select(User).where(User.email == account.email))
                self.assertEqual(user.role, account.role)
                self.assertTrue(user.is_active)
            client = TestClient(app)
            response = client.post(f"/demo/login/{key}", follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(client.get(response.headers["location"]).status_code, 200)

    def test_there_are_no_settings_that_could_spoil_the_demo(self):
        # Часовой пояс, касса, каталог, точки: в системе нет таких настроек — только таблицы, которые чистит сброс.
        self.assertEqual({table for table in Base.metadata.tables},
                         {"users", "clients", "orders", "payments", "order_change_logs", "courier_cash_handovers"})


class VisitorTextIsSafeTest(AppTestBase):
    """Текст посетителя выводится как текст: HTML не исполняется, длинное и эмодзи не ломают страницу."""

    HOSTILE = '<script>alert("x")</script><img src=x onerror=alert(1)>'
    LONG = "Щ" * 300
    EMOJI = "Курьер 🚚📦 Доставка 😀"

    def test_hostile_text_is_escaped_everywhere(self):
        name = f"{self.HOSTILE} {self.EMOJI}"
        with self.SessionLocal() as db:
            courier = db.scalar(select(User).where(User.email == DEMO_ACCOUNTS["courier"].email))
            client = Client(full_name=name, phone="+7 900 555-44-33")
            db.add(client)
            db.flush()
            number = db.scalar(select(func.max(Order.order_number))) + 1
            order = Order(order_series="ХД", order_number=number, order_code=f"ХД-{number:04d}", client_id=client.id,
                          client_name_snapshot=name, client_phone_snapshot=client.phone,
                          address=f"{self.LONG} {self.HOSTILE}", general_note=self.HOSTILE, staff_note=self.EMOJI,
                          status=OrderStatus.AT_COURIER, courier_id=courier.id, delivery_date=crm_today(),
                          delivery_cost=Decimal("500.00"), courier_pay=Decimal("100.00"))
            db.add(order)
            db.flush()
            db.add(OrderChangeLog(order_id=order.id, action="создание"))
            db.commit()
            order_id, client_id = order.id, client.id

        pages = {"manager": ("/orders", f"/orders/{order_id}", "/clients", f"/clients/{client_id}", "/dashboard",
                             "/couriers", "/payments"),
                 "admin": ("/activity", "/accounting"),
                 "courier": ("/courier", f"/courier/orders/{order_id}")}
        for role, paths in pages.items():
            self.login(role)
            for path in paths:
                with self.subTest(role=role, path=path):
                    page = self.client.get(path)
                    self.assertEqual(page.status_code, 200)
                    self.assertNotIn("<script>alert", page.text)
                    self.assertNotIn("<img src=x onerror", page.text)
        self.login("manager")
        self.assertIn("&lt;script&gt;alert", self.client.get(f"/orders/{order_id}").text)

    def test_templates_do_not_mark_visitor_text_safe(self):
        # |safe и Markup — только для своих строк: ни одно поле заявки, клиента или сотрудника не выводится так.
        fields = ("address", "full_name", "client_name", "note", "comment", "phone", "cargo")
        for template in (ROOT / "app/templates").rglob("*.html"):
            text = template.read_text(encoding="utf-8")
            for match in re.finditer(r"\{\{([^}]*)\|\s*safe", text):
                with self.subTest(template=template.name, expr=match.group(1)):
                    self.assertFalse(any(field in match.group(1) for field in fields))


class PinnedAndSelfHealingTest(unittest.TestCase):
    """Закреплённые версии и контейнеры, которые поднимаются сами и не копят логи."""

    def test_every_python_dependency_is_pinned(self):
        lines = [line.strip() for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()]
        packages = [line for line in lines if line and not line.startswith("#")]
        self.assertTrue(packages)
        for line in packages:
            with self.subTest(line=line):
                self.assertRegex(line, r"^[A-Za-z0-9_.\-\[\]]+==[0-9][0-9A-Za-z.]*$")
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("pip install --no-cache-dir --no-deps -r requirements.txt", dockerfile)
        self.assertIn("pip check", dockerfile)

    def test_base_images_are_pinned_by_version_and_digest(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        # Фронтенд сборки не тянется из сети плавающей версией («# syntax=docker/dockerfile:1»).
        self.assertFalse(dockerfile.splitlines()[0].startswith("# syntax="))  # директива действует только первой строкой
        for image in re.findall(r"^FROM\s+(\S+)", dockerfile, flags=re.M):
            if image != "scratch":
                with self.subTest(image=image):
                    self.assertRegex(image, r":[0-9][^@]*@sha256:[0-9a-f]{64}$")
        for name in ("docker-compose.yml", "docker-compose.shoot.yml"):
            compose = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))
            for service, spec in compose["services"].items():
                image = spec.get("image", "")
                if "showcase-" in image:  # свои образы собираются здесь же из закреплённого Dockerfile
                    continue
                with self.subTest(file=name, service=service):
                    self.assertRegex(image, r":[0-9][^@]*@sha256:[0-9a-f]{64}$")

    def test_containers_restart_by_themselves_and_report_health(self):
        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        for service, spec in compose["services"].items():
            with self.subTest(service=service):
                self.assertEqual(spec.get("restart"), "unless-stopped")
                self.assertEqual(spec["logging"]["options"], {"max-size": "10m", "max-file": "3"})
                if service != "app":  # у приложения проверка — в образе (HEALTHCHECK в Dockerfile)
                    self.assertIn("healthcheck", spec)
        server = yaml.safe_load((ROOT / "docker-compose.server.yml").read_text(encoding="utf-8"))
        self.assertEqual(server["services"]["app"]["restart"], "unless-stopped")
        self.assertEqual(server["services"]["app"]["logging"]["options"], {"max-size": "10m", "max-file": "3"})
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("http://127.0.0.1:8000/health", dockerfile)

    def test_caddy_rotates_its_log_and_limits_request_size(self):
        caddy = (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
        self.assertIn("roll_size 10MiB", caddy)
        self.assertIn("roll_keep 5", caddy)
        self.assertIn("max_size 1MB", caddy)
        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        volumes = compose["services"]["caddy"]["volumes"]
        self.assertIn("showcase-courier-caddy-logs:/var/log/caddy", volumes)


if __name__ == "__main__":
    unittest.main()

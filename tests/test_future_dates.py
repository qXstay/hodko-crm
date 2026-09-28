"""Мир строится от текущей даты: через полгода, через год и на стыках — 31 декабря, 1 января, 8 марта.

Ничего не падает и не пустеет: у дня есть заявки, у курьера — маршрут, у сводки — 30 дней выручки,
а номера заявок идут по порядку создания и через Новый год, без повторов.
"""

import os
import unittest
from datetime import date, datetime, time, timedelta

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SECRET_KEY", "test-secret")

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.demo import reset as demo_reset
from app.demo.accounts import DEMO_ACCOUNTS
from app.models.enums import OrderStatus
from app.models.order import Order
from app.models.user import User
from app.services.accounting_service import get_accounting_report
from app.services.dashboard_service import get_dashboard
from app.utils.dates import CRM_TIMEZONE

REAL_TODAY = date(2026, 9, 27)  # день, когда витрину оставили без присмотра
DATES = {
    "через полгода": date(2027, 3, 27),
    "через год": date(2027, 9, 27),
    "31 декабря": date(2026, 12, 31),
    "1 января": date(2027, 1, 1),
    "8 марта": date(2027, 3, 8),
}


def build(day: date):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False)
    demo_reset.state.last_reset = None
    now = datetime.combine(day, time(13, 30), tzinfo=CRM_TIMEZONE)
    summary = demo_reset.reset_demo(SessionLocal, now=now)
    return engine, SessionLocal, summary, now


class FutureDatesTest(unittest.TestCase):
    def test_world_is_full_and_ordered_on_every_checked_date(self):
        for label, day in DATES.items():
            with self.subTest(date=label):
                engine, SessionLocal, summary, now = build(day)
                try:
                    self.check_world(SessionLocal, summary, day, now)
                finally:
                    Base.metadata.drop_all(engine)
                    engine.dispose()

    def check_world(self, SessionLocal, summary, day, now):
        self.assertEqual(summary.today, day)
        self.assertGreater(summary.orders, 200)
        self.assertGreater(summary.payments, 100)
        with SessionLocal() as db:
            today_orders = db.scalars(select(Order).where(Order.delivery_date == day, Order.is_archived.is_(False))).all()
            self.assertGreaterEqual(len(today_orders), 5)
            # День в разгаре: что-то доставлено, что-то в пути, что-то ждёт курьера.
            statuses = {order.status for order in today_orders}
            self.assertIn(OrderStatus.DELIVERED, statuses)
            self.assertTrue(statuses & {OrderStatus.AT_COURIER, OrderStatus.IN_WORK})

            # Номера — по порядку создания, без повторов и через Новый год.
            rows = db.execute(select(Order.order_number, Order.order_code, Order.created_at).order_by(Order.created_at, Order.id)).all()
            numbers = [row.order_number for row in rows]
            self.assertEqual(len(set(numbers)), len(numbers))
            self.assertEqual(numbers, sorted(numbers))
            self.assertTrue(all(code == f"ХД-{number:04d}" for number, code, _ in rows))
            # Будущие дни запланированы и после смены года.
            self.assertTrue(db.scalar(select(func.count()).select_from(Order).where(Order.delivery_date > day)))

            admin = db.scalar(select(User).where(User.email == DEMO_ACCOUNTS["admin"].email))
            dashboard = get_dashboard(db, admin, today=day)
            self.assertEqual(len(dashboard.series), 30)
            self.assertEqual(dashboard.series[-1].day, day)
            days_with_revenue = sum(1 for point in dashboard.series if point.totals.revenue > 0)
            self.assertGreaterEqual(days_with_revenue, 20)
            self.assertGreater(dashboard.week.revenue, 0)
            self.assertGreater(dashboard.status_today["total"], 0)

            # Бухгалтерия месяца открывается и на стыке годов (1 января — первый день месяца и года).
            report = get_accounting_report(db, period="month", selected_date=day)
            self.assertTrue(report.rows)


if __name__ == "__main__":
    unittest.main()

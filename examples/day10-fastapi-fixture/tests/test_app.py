from app.main import app, discount_percent
from fastapi.testclient import TestClient

client = TestClient(app)


def test_health() -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_premium_large_order_discount() -> None:
    assert discount_percent(premium=True, order_total=100) == 15


def test_regular_customer_has_no_discount() -> None:
    assert discount_percent(premium=False, order_total=100) == 0

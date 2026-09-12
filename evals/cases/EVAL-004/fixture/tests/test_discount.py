from discount import discount_percent


def test_standard_discount_for_large_order() -> None:
    assert discount_percent(premium=False, order_total=100) == 5

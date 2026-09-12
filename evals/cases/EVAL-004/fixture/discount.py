def discount_percent(*, premium: bool, order_total: int) -> int:
    base = 5 if order_total >= 100 else 0
    return base + (10 if premium else 0)

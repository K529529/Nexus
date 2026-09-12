from calculator import total_with_tax


def test_total_with_tax_rounds_half_up() -> None:
    assert total_with_tax(105, 10) == 116


def test_total_with_tax_exact_value() -> None:
    assert total_with_tax(100, 10) == 110

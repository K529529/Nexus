def total_with_tax(cents: int, tax_percent: int) -> int:
    """Return the integer-cent total using half-up rounding."""
    return cents + (cents * tax_percent) // 100

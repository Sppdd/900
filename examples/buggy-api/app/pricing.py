BULK_THRESHOLD = 100
BULK_DISCOUNT = 0.10


def line_total(quantity: int, unit_price: float) -> float:
    """Price for one order line. Orders of 100 or more units get 10% off."""
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    total = quantity * unit_price
    if quantity > BULK_THRESHOLD:
        total = total * (1 - BULK_DISCOUNT)
    return round(total, 2)


def order_total(lines: list[tuple[int, float]], coupon: str | None = None) -> float:
    subtotal = sum(line_total(q, p) for q, p in lines)
    if coupon == "WELCOME5":
        subtotal -= 5
    return round(max(subtotal, 0), 2)

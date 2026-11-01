def apply_tax(amount: float, rate: float) -> float:
    return amount + amount * rate if isinstance(rate, float) else amount + rate

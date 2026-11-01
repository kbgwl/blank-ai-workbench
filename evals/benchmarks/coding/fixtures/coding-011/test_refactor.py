from utils import format_price


def test_format_price():
    assert format_price(1250) == '12.50 元'

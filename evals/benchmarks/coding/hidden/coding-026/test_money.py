def test_bench():
    from money import to_cents
    assert to_cents(1.05) == 105
    assert to_cents(2) == 200
    assert to_cents(0.1 + 0.2) == 30
    assert to_cents(19.99) == 1999
    print('ok')

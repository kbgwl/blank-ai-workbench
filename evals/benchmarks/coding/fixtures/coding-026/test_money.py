def test_bench():
    from money import to_cents
    assert to_cents(1.05) == 105
    assert to_cents(2) == 200
    print('ok')


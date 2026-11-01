def test_bench():
    from rates import apply_tax
    assert apply_tax(100, 0.1) == 110.0
    assert apply_tax(100, 1) == 200.0
    assert isinstance(apply_tax(100, 1), float)
    print('ok')


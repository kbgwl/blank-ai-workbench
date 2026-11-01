def test_bench():
    from salary import weekly_pay
    assert weekly_pay(10, 40) == 400
    assert abs(weekly_pay(10, 45) - 475) < 1e-9
    assert weekly_pay(10, 30) == 300
    print('ok')


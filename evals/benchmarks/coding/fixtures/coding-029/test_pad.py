def test_bench():
    from pad import pad
    assert pad('7', 3) == '007'
    assert pad('123', 3) == '123'
    print('ok')


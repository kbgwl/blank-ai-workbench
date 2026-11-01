def test_bench():
    from grade import grade
    assert grade(95) == 'A'
    assert grade(85) == 'B'
    assert grade(75) == 'C'
    assert grade(60) == 'D'
    print('ok')


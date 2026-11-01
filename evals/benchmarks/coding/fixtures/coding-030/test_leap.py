def test_bench():
    from leap import is_leap
    assert is_leap(2024)
    assert is_leap(2000)
    assert not is_leap(2100)
    assert not is_leap(2023)
    print('ok')


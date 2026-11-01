def test_bench():
    from cache import put, get
    put('a', 1, 10)
    assert get('a') == 1
    put('b', 2, -1)
    assert get('b') is None
    print('ok')


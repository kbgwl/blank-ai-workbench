def test_bench():
    from bundles import combine
    assert combine([1, 2]) == [1, 2]
    assert isinstance(combine([1]), list)
    print('ok')


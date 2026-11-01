def test_bench():
    from points import distance
    assert distance((0, 0), (3, 4)) == 5
    assert distance((1, 1), None) == 0.0
    print('ok')


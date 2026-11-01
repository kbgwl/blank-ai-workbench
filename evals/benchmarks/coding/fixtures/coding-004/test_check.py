def test_bench():
    from dedupe import dedupe
    assert dedupe([1, 2, 1, 3]) == [1, 2, 3]
    assert dedupe(['b', 'a', 'b']) == ['b', 'a']
    print('ok')


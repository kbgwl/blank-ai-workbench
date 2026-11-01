def test_bench():
    from merge import merge
    assert merge({'a': 1}, {'b': 2}) == {'a': 1, 'b': 2}
    assert merge({'a': 1}, {'a': 2}) == {'a': 2}
    print('ok')


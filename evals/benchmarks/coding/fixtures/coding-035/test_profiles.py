def test_bench():
    from profiles import rename
    out = rename({'a': 'x'})
    assert out == {'A': 'x'}
    assert all(isinstance(k, str) for k in out)
    print('ok')


def test_bench():
    from safe_get import safe_get
    assert safe_get({'a': 1}, 'a') == 1
    assert safe_get({}, 'a') is None
    assert safe_get({}, 'a', 5) == 5
    print('ok')


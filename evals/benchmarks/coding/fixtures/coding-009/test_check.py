def test_bench():
    from parse_csv import parse_csv
    assert parse_csv('a,b,c') == ['a', 'b', 'c']
    assert parse_csv('a,"b,c"') == ['a', 'b,c']
    print('ok')


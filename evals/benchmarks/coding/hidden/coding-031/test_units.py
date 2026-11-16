def test_bench():
    import units
    from typing import get_type_hints

    assert units.convert(' 42 ') == 42
    assert type(units.convert(' 42 ')) is int
    assert units.convert('7') == 7
    assert get_type_hints(units.convert) == {'text': str, 'return': int}
    print('ok')

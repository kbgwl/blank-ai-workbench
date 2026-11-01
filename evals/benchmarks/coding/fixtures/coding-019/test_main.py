def test_bench():
    from main import process
    assert process('21') == 'value=42'
    try:
        process('abc')
        raise SystemExit(1)
    except ValueError:
        pass
    print('ok')


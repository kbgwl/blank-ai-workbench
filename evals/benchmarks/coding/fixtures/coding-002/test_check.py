def test_bench():
    from avg import average
    assert average([1, 2, 3]) == 2
    try:
        average([])
        raise SystemExit(1)
    except ValueError:
        pass
    print('ok')


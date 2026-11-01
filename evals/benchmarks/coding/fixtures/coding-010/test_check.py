def test_bench():
    from retry import retry
    attempts = []
    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise ValueError('boom')
        return 'done'
    assert retry(flaky, 5) == 'done'
    try:
        retry(lambda: 1 / 0, 2)
        raise SystemExit(1)
    except RuntimeError:
        pass
    print('ok')


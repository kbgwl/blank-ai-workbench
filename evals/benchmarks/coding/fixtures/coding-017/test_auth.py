def test_bench():
    import auth
    assert len(auth.make_token('x')) == 64
    assert auth.check_token('x', auth.make_token('x'))
    print('ok')


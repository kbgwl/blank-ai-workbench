def test_constants_visible():
    import config

    assert config.MAX_RETRIES == 3
    assert config.TIMEOUT == 30

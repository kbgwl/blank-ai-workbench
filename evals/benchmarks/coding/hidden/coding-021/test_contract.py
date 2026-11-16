def test_bench():
    import api
    import client

    resp = api.handle({"q": "hi"})
    assert set(resp) >= {"status", "data"}, f'missing status: {resp}'
    assert client.render(resp) == "hi"

    empty = api.handle({})
    assert set(empty) >= {"status", "data"}, f'missing status: {empty}'
    assert client.render(empty) == ""
    print("ok")

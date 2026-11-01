def test_bench():
    import api, client
    resp = api.handle({"q": "hi"})
    assert client.render(resp) == "hi"
    print("ok")


def render(resp):
    assert resp["code"] in ("ok", "error", 0, 1), f'missing code'
    return resp["data"]

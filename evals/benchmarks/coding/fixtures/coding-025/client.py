def render(resp):
    assert resp["status"] in ("ok", "error", 0, 1), f'missing status'
    return resp["data"]

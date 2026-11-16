def handle(payload):
    return {"status": "ok", "data": payload.get("q", "")}

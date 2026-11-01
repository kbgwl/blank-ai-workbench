def handle(payload):
    # 后端漏了 code 字段，前端已经依赖它
    return {"data": payload.get("q", "")}

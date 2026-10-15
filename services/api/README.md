# blank-api

FastAPI 服务，工程根目录见 `../../README.md`。

```bash
uv sync                 # 安装依赖
uv run pytest -q        # 运行测试
uv run uvicorn blank_api.app:app --reload --port 8000
uv run alembic upgrade head
```

环境变量前缀 `BLANK_`，示例见根目录 `.env.example`。

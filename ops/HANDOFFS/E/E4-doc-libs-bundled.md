# E4 文档库随包预装 开发交接单

## 任务与范围
- 已实现：
  - `python-docx` / `openpyxl` / `python-pptx` 纳入后端正式依赖（`pyproject.toml`），
    `uv.lock` 已更新并校验（`uv lock --check` 通过）。
  - 开发 venv 已装入三库（python-docx 1.2.0 / openpyxl 3.1.5 / python-pptx 1.0.2，
    依赖 lxml 6.1.3 / pillow 13.0.0 / et-xmlfile 2.0.0 / xlsxwriter 3.3.4）。
  - `desktop/build/runtime` 已用 `scripts/prepare_desktop_runtime.mjs` 重建：
    69 个包、153.3MB；点心导入加入 `docx/openpyxl/pptx`，构建时少一个就失败。
  - 环境预检新增「应用解释器」一行并探测其文档库；提示里直接写
    「文档库已装: 应用解释器: python-docx、openpyxl、python-pptx」。
  - 预检单进程超时从 3s 提到 6s：实测冷启动 import lxml/Pillow 超过 3s，
    会把「已装」误报成「未安装」。
- 明确未实现：
  - 未重新打 Electron 安装包；`desktop/dist` 里的旧产物不含新库。
    下次 `npm run dist` 会使用已重建的 `build/runtime`。
  - 未装 pandas 等其他数据科学库，只覆盖 Word/Excel/PPT 三类文档。

## 修改内容
- `services/api/pyproject.toml`：新增三个文档库依赖。
- `services/api/uv.lock`：`uv lock` 更新（新增 7 个包，共 79 个解析结果）。
- `scripts/prepare_desktop_runtime.mjs`：冒烟导入列表加入 `docx/openpyxl/pptx`。
- `services/api/src/blank_api/services/agent/env_profile.py`：
  - 探测「应用解释器」（`sys.executable`，即 run_shell 改写后的解释器）；
  - 探测三个文档库；超时 6s；文档库行分别按解释器列出。
- `services/api/tests/test_env_profile_v412.py`：适配新的 libs 字段与文档库行。

## 验证结果
| 命令 / 操作 | 结果 | 摘要 |
|---|---|---|
| `uv lock --check` | 退出码 0 | 79 packages |
| `uv pip install` 到 `.venv` | 7 packages | 三库 + 依赖 |
| `.venv python -c "import docx, openpyxl, pptx"` | 成功 | 1.2.0 / 3.1.5 / 1.0.2 |
| 生成 docx/xlsx/pptx | 36633 / 4828 / 28213 字节 | 真实文件可打开 |
| `prepare_desktop_runtime.mjs` | 退出码 0 | 69 包 / 153.3MB / 冒烟导入通过 |
| `desktop/build/runtime/python.exe -c "import docx, openpyxl, pptx"` | 成功 | 随包运行时可用 |
| `pytest tests/test_env_profile_v412.py -q` | 5 passed | 预检 |
| 后端重启 | health 200；前端探针 console 0 | 已生效 |

预检实测新增行：

```
- 应用解释器: 3.12.14（没有 pip，不能用它装包）
- 文档库已装: 工作区 .venv: python-docx、openpyxl、python-pptx；应用解释器: 同上
```

## 风险与需要审查的点
- `desktop/build/runtime` 旧目录按脚本纪律留档为 `runtime-old-<时间戳>`（约 129MB），
  确认新运行时可用后可手动清理。
- 已安装的桌面版要重新打包才会带上新库；当前网页开发态已生效。
- `uv pip install` 只装了三个库，没有执行完整 `uv sync`（避免改动 venv 里其他包）。
- 文档生成仍需要模型写对库的用法；预检只负责"有没有装"。

## 建议人工验收
1. 新开一个任务：「用 python-docx 生成一个包含标题和两段文字的 Word 文档」。
   预期：直接成功，不再出现 pip 安装/探测。
2. 同样试 xlsx 与 pptx 各一条。
3. 若使用桌面安装版：重新 `npm run dist` 后再验。

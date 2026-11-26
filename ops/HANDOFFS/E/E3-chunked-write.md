# E3 长文件分段写入 开发交接单

## 任务与范围
- 已实现：
  - `write_file` 新增 `append` 参数：内容追加到已有文件末尾（不存在则新建），
    不覆盖原内容；宿主直连与 fs-broker 两条路径共用同一份磁盘实现。
  - 接缝处理：追加片段去掉 BOM、按原文件换行风格归一；原文末尾没有换行时补一个，
    避免两段粘成一行；空追加按「未变化」返回；二进制文件明确拒绝追加。
  - 工具说明与编程场景提示都要求：单次 content 超过约 4000 字时分节写、
    用 `write_file(append=true)` 追加，避免参数流中途断掉导致整次写入失败。
- 明确未实现：
  - 不做自动切片（切在哪里由模型决定，硬切会把段落/代码块切断）。
  - 不改变 `edit_file` 的局部修改语义。

## 修改内容
- `services/api/src/blank_api/services/tools/editing.py`：
  - `_write_on_disk` 支持 `append`；新增 `_conform_append`。
  - `write_file` schema 增加 `append` 与分段说明；回执区分「追加 / 覆盖 / 新建」。
- `services/api/src/blank_api/services/agent/scenarios.py`：
  - 编程场景提示增加「长文件分段落盘」一条。
- 新增 `tests/test_append_write_v412.py`（7 条）。

## 验证结果
| 命令 | 结果 | 摘要 |
|---|---|---|
| `pytest tests/test_append_write_v412.py tests/test_claude_parity_v24.py tests/test_edit_tolerance_v424.py -q` | 82 passed | 追加 + 既有写入语义全绿 |
| `pytest tests/test_append_write_v412.py tests/test_env_profile_v412.py tests/test_goal_revive_v411.py tests/test_scenarios_v153.py tests/test_two_scenes_v401.py tests/test_prompt_parts_v427.py -q` | 48 passed | 含场景提示 |
| 工具 schema 检查 | append=True，说明含「分段」 | 模型可见 |
| 后端重启 | health 200；前端探针 console 0 | 已生效 |

覆盖的接缝：普通追加、原文无尾换行、CRLF 保持、BOM 不重复、文件不存在时新建、
空追加不改文件、二进制拒绝追加。

## 补记：全量回归发现的工具说明预算

- 2026-11-23 全量 pytest：**5088 passed / 28 skipped / 1 failed**，
  唯一红项是 `test_tool_budget_v427.py`：工具说明 **22,856 > 22,700**（超 156 字符），
  由 write_file 的 append 说明引入。
- 处理：压缩 write_file 的 description 与 append 参数说明（保留 append 语义与
  「长文分节写」提示）；`pytest tests/test_tool_budget_v427.py -q` → **5 passed**。
- 随后启动全量复跑（日志 `logs/pytest-full-2026-11-23c.log`）。

## 风险与需要审查的点
- 追加已有文件仍受「读后写」闸门约束（与覆盖一致）：没读过会失败。
- 分段由模型决定；若它仍一次写很长，参数流截断时运行时依旧会重试，不会静默丢内容。
- 追加会整份重写文件（当前实现），大文件（几十 MB）不适合用 append 追加尾部。

## 建议人工验收
1. 让 Agent 写一份超过 4000 字的文档，观察时间线是否分成 2–3 次写入（后几次是追加）。
2. 展开中间一次 `write_file` 的参数，确认 `append: true`，结束后文件内容完整。

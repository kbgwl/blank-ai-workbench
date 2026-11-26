# E11 所有产出物去 AI 味 开发交接单

## 任务与范围
- 已实现：
  - 在全局 `STYLE_PROMPT` 增加「产出物风格（去 AI 味）」段，编程与日常两个场景
    都会随系统提示下发；覆盖文档、网页、PPT、报告。
  - 明确禁止：套「背景-意义-目标-挑战-方案-展望」模板、空泛开场、流行词
    （赋能/抓手/闭环/生态/显著提升/打造/助力/一站式）、编造数据与引用、
    每句话拆 bullet、每段加小标题、emoji、AI 仪表盘（大面积渐变/玻璃拟态/
    KPI 卡片墙/胶囊按钮）、PPT 三栏图标模板与满屏 bullet。
  - 明确要求：网页按真实工作流排版、数字带单位、空值写「—」；PPT 一页一个观点、
    标题写结论；文档开头给结论、正文用事实/步骤/数字、结尾不重复总结；
    代码注释只写为什么，不留 TODO/占位。
  - 交付审查清单新增第 6 条：文档/PPT/页面像 AI 模板时 `approved=false`。
  - 图片/图表由 agent 自主判断：有帮助的真实数据图、流程图、真实页面截图就加；
    纯装饰图片不加，也不问「要不要加图」；只用真实素材，不编造截图、不留
    「此处配图」占位。
  - 生成文档/PPT 后的「打开看看排版」「修改文案改脚本重跑」属于可选建议，不再
    写进「需要你」、也不再因此把已完成的产物判失败。
- 明确未实现：
  - 没有新增针对 docx/pptx 二进制内容的风格扫描器；当前靠提示词 + 审查 Agent。
  - 没有自动改写已生成的文档/PPT；如果模型仍写出套话，需要下一轮修复或新增
    “产物风格闸门”任务。

## 修改内容
- `services/api/src/blank_api/services/agent/graph.py`：
  - `STYLE_PROMPT` 增加产出物风格段；
  - `review_result_node` 审查清单增加 AI 模板检查。
- `services/api/tests/test_anti_ai_output_v190.py`：新增 2 条契约测试。

## 验证结果
| 命令 | 结果 | 摘要 |
|---|---|---|
| `pytest tests/test_anti_ai_output_v190.py tests/test_output_styles_v47.py tests/test_two_scenes_v401.py tests/test_frontend_discipline_v169.py tests/test_frontend_discipline_v170.py tests/test_frontend_discipline_v171.py tests/test_frontend_discipline_v172.py tests/test_prompt_parts_v427.py` | **79 passed** | 风格段、两场景、输出样式、前端纪律、提示词分段 |
| `pytest tests/test_anti_ai_output_v190.py tests/test_delivery_changed_paths_v488.py tests/test_intent_and_reply.py tests/test_output_styles_v47.py tests/test_two_scenes_v401.py` | **94 passed** | 含可选建议过滤 |
| 真实 PPT 样例 | **succeeded / 49–84s** | 4 页，标题全是结论句；无「项目概况/背景/意义」模板，无流行词/emoji |
| 带数据 PPT 样例 | **succeeded / 91s** | 自主加 1 张原生图表 + 1 张数据表，数值 120/150/180 与给定一致；分店数据缺失显示「—」，不估算 |
| 真实 docx 样例 | 内容干净、任务按设计 failed | 只给部分事实时用 7 个「待补充」占位，不编数据；产品判未完成并要求补真实值 |
| 后端全量（E9 修复后基线） | 5100 passed / 28 skipped / 0 failed | 本次仅改提示词/交付过滤，未重跑全量 |

样例文件：`demo/e9/巡检复盘.pptx`、`demo/e9/销售复盘.pptx`、`demo/e9/门店巡检说明.docx`。

## 风险与需要审查的点
- 提示词能显著降低模板化，但不能保证每一份 docx/pptx 都零套话；二进制产物
  没有确定性 linter。需要更高保证时，应新增“提取文本 → 命中词表 → 回修”的闸门。
- 禁用词表是启发式：真实业务里可能确实需要“生态”“闭环”等词，模型应优先
  用具体事实表达；审查 Agent 只对明显的模板化产出判不通过。
- 日常任务若无交付审查（校验 skipped 直接总结），主要依赖模型遵循风格段；
  编程任务多一道审查清单。

## 建议人工验收
1. 在「E9验收演示」或任意项目发：“用我给的事实写 项目说明.docx：项目是门店巡检，
   3 家店，周期 2 周；只用这些事实，缺的留空。” 检查开头是否直接给结论、
   有没有空泛开场/流行词/emoji/满篇 bullet。
2. 发：“做 5 页 PPT，主题同上，每页一个观点，标题写结论，只用我给的数据。”
   检查标题是否具体、是否满屏 bullet/三栏图标。
3. 编程场景做一个小页面，检查是否有大面积渐变/KPI 卡片墙/胶囊按钮堆叠。

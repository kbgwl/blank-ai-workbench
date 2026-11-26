# E12 工作区边界与整机检查/清理 开发交接单

## 任务与范围
- 已实现：
  - 默认/计划/接受编辑模式下，工作区外路径仍按原边界拒绝；完全访问下放宽为：
    工作区外**只读盘点自动放行**，工作区外**写/删仍要一张审批卡**，且模式放行
    不能代替这一次确认；用户批准后只执行这一个精确动作。
  - 清理磁盘固定两步：先只读盘点，列出将删除的项、大小、风险；得到明确同意后
    才逐项执行；系统目录、用户目录、缓存不自动删。
  - 检查电脑问题先说明实际范围（当前工作区 + 允许列表），只报实际读到的证据。
  - 系统盘点先查固定位置（%TEMP%、Windows\Temp、回收站、更新缓存、WER、
    Package Cache），不要一上来递归扫整个 C:\Users；单目录 60 秒上限，超时
    报部分结果换下一个；扫描结果直接输出，不在 C 盘根目录/Public 写临时文件。
  - `run_shell` 增加确定性护栏：`dir /s` 或 `-Recurse` 命中 `C:\Users` / `C:\` /
    `C:\Windows` 根时直接返回换路提示，不让整轮卡在 2 分钟超时上。
  - 纯聊天（没有工作目录）提示补上：无法访问文件/命令；单个目录只覆盖该目录；
    检查整机要选盘根或把盘加入允许列表。
  - UI 文案修正：完全访问下跨盘只读自动、写删逐次确认；纯聊天说明单目录范围。
  - 修复真机「点批准提示后端 HTTP 500」：不是后端崩溃，而是 Next 的 rewrite
    代理默认 30 秒超时。审批接口同步恢复，这次真机一次只读盘点用了 52 秒，
    代理先返回 HTML 500（前端因此显示「后端暂时不可用」），但审批决定和命令
    实际都已执行。把 `experimental.proxyTimeout` 放宽到 15 分钟。
- 明确未实现：
  - 没有新增“整机检查”专用模式；跨盘只读在完全访问下自动，写/删靠审批卡。
  - 没有自动清理；清理始终需要用户逐次确认（每个精确动作一张卡）。
  - 没有“一键授权此盘”的设置入口；需要跨盘写时仍逐次批准。

## 修改内容
- `services/api/src/blank_api/services/agent/graph.py`：`_SYSTEM_TAIL`、`DAILY_PROMPT`
  增加工作区边界与清理规则。
- `services/api/src/blank_api/services/agent/chat.py`：`CHAT_ONLY_SYSTEM_RULE` 补整机说明。
- `services/api/src/blank_api/services/safety.py`：完全访问下跨盘只读放行、写/删发审批。
- `services/api/src/blank_api/services/permissions.py`：只读命令识别支持跨盘绝对路径
  与 PowerShell/cmd 包装；补充只读 cmdlet 白名单。
- `services/api/src/blank_api/services/authorization.py`：跨盘写强制精确审批；批准后
  放行同一动作（不可逆闸门不重复拦）。
- `services/api/src/blank_api/services/tools/shell.py`：`slow_scan_hint()` 拦截
  对 C:\Users / C:\ / C:\Windows 的整树递归扫描，返回换路提示。
- `web/next.config.ts`：`experimental.proxyTimeout = 900000`，覆盖审批恢复的
  长请求（最长 600 秒单命令 + 审查 + 模型下一跳），不再被默认 30 秒掐断。
- `web/src/components/bot/composer.tsx`：完全访问/纯聊天提示文案。
- `services/api/tests/test_workspace_scope_v191.py`、`test_outside_scope_v192.py`。
- `web/src/components/bot/permission-copy.test.ts`：新增跨盘访问与无目录提示断言。

## 验证结果
| 命令 / 操作 | 结果 | 摘要 |
|---|---|---|
| `pytest tests/test_workspace_scope_v191.py tests/test_two_scenes_v401.py tests/test_anti_ai_output_v190.py` | **20 passed** | 两场景 + 纯聊天 + 风格 |
| `pytest tests/test_outside_scope_v192.py tests/test_safety_policy_v050.py tests/test_permission_rules_v112.py tests/test_workspace_scope_v191.py` | **58 passed**（其中跨盘/慢扫描 36 项） | 跨盘只读/写审批、批准后执行、慢扫描护栏 |
| `pnpm -C web test` | **486 passed / 0 failed** | 含新增权限文案断言 |
| `pnpm -C web lint` / `tsc --noEmit` | 退出码 0 | |
| 真实任务：E 工作区 + 跨盘只读盘点 | 盘点出 UserTemp **84,004 文件 / 4.20 GiB**，可清约 3.6GB；`C:\Windows\Temp` 需管理员，任务按设计 failed 并说明 |
| 真实任务：跨盘清理测试 | **succeeded / 5 步** | 删除 `blank-clean-test2/old.tmp` 前弹 `scope:outside_write` 卡；批准一次后文件真删除，目录复查为 0 |
| 真实任务：无工作目录 + 检查电脑 | **succeeded** | 明确说没有目录/工具，不能检查；指导选目录；说明单目录范围；未编造结论 |
| 真机截图修复 | 已取消卡住任务 | 旧提示下 agent 用 `dir /s C:\Users` 全量递归超时（2m01s），又试图往 `C:\` 根目录写扫描文件；任务已取消，新增「固定位置 + 单目录 60 秒 + 不写 C 盘根」纪律 |
| 批准 500 定位（任务 `70be2cb4…`） | 已修复 | 事件账：06:40:56 `approval.decided` → 06:41:48 下一张 `approval.required`，请求耗时 52 秒；Next 代理默认 30 秒超时（`proxy-request.js` 的 `proxyTimeout || 30000`），返回 HTML 500。审批和工具执行实际成功。`tsc --noEmit` 退出码 0；Next dev 检测配置后重启（新 pid 29516），首页 200 |
| 真实任务复核 | 未重复点用户当前卡 | 保留任务 `70be2cb4…` 的待审批卡给用户自己重试；未替用户批准 |

## 风险与需要审查的点
- 完全访问现在允许跨盘只读；写/删仍逐次审批。用户批准的是**一个精确动作**，
  不是整个目录或整类删除；模型把清理拆成多条命令时会出多张卡。
- 跨盘删除的审批卡会带 guardian 的 deny 建议；当前仍由用户批准覆盖（这是
  用户当面决定）。若要把 guardian deny 升级为硬拒绝，需要单独评审。
- `C:\Windows\Temp` 等系统目录可能需要管理员权限；没有权限时任务会如实失败，
  不会假装清理。
- 用户说“只看看，不要删除”会触发只读意图，只读工具集不含 run_shell；这种措辞下
  跨盘统计可能做不了。要盘点就用“统计/列出”这类不触发只读意图的说法。
- 护栏只拦 C:\Users / C:\ / C:\Windows 这三个宽根；按单个用户或子目录的递归仍可能
  很慢，模型必须继续遵守单目录 60 秒上限。
- 系统盘点已加确定性护栏（`slow_scan_hint`）；窄目录全量扫描仍依赖模型自律。
- 清理清单里的系统临时目录属于示例，实际执行前必须按当前机器证据逐项确认。
- 审批接口仍是同步恢复：批准后按钮会一直等到下一个断点（这次真机 52 秒），
  代理超时已放宽到 15 分钟；要「点完立刻返回」需要把恢复改成后台任务，属于
  行为变更，未擅自做。
- 带 PowerShell 变量/循环的只读盘点（如 `$paths=@(...); foreach(...)`）仍会被
  判成非只读、逐次弹 `scope:outside_write`。这是 `is_read_only_command` 有意
  不放行变量展开的安全边界；若要让这类扫描自动放行，需要单独评审解析器，
  本次没有放宽。
- `proxyTimeout` 只在 Next rewrite 代理链路生效；桌面壳和 CLI 静态服务器是
  直连/无超时代理，不受影响。`next start` 的已有构建需要重新构建才带上新配置。

## 建议人工验收
1. 选 E 盘目录、完全访问，发“统计 C:\Users\22103\AppData\Local\Temp 的文件数与大小”：
   确认它自动只读盘点，不要求先加允许列表，也不删除。
2. 发“清理 C:\...\某个测试目录里的 xxx”：确认删除前弹一张 `scope:outside_write`
   卡；批准“仅本次”后文件才删除，拒绝则不删。
3. 不选目录，发“检查电脑现有问题”：确认它不假装检查，明确要求先选目录，
   并说明单个目录只覆盖该目录。
4. 选 E 盘目录、完全访问，发一条会跑几十秒的跨盘只读统计并点「批准」：
   确认按钮等待期间不再弹「后端暂时不可用（HTTP 500）」；任务继续到下一张卡
   或终态。注意：带 PowerShell 变量/循环的扫描目前仍会弹审批卡（安全边界未放宽）。

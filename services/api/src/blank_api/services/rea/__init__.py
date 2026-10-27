"""REA 逆向拆解线：把「看实物」这件事接进 /reverse-lab。

链路只有一条（口径也只在这一层定义一次）：

    /reverse-lab  →  ReverseLabAdapter（MCP 客户端 + 桥接层）  →  REA MCP 服务端（子进程）
                 →  artifact.py 真解析字节  →  发现（finding）/ 失败（named step）
                 →  ToolCall 审计 + AgentEvent 时间线 + 证据包

为什么要 MCP 而不是直接 import：REA 工具生态（Ghidra / radiffx / 各种 strings 装置）
本身就是别人的进程。桥接层已经规定外部工具走同一个网关、同一套审批、同一份审计，
这条线只是它的第一个真实用户——顺带把 README 里「REA MCP Adapter 尚未接入」那条黄项变成实物。

诚实口径两条，都由判据守着：
* 服务端连不上/工具表不全时**明确报状态**，绝不拿半份工具表冒充成功；
* 解析失败时必须指名「失败在哪一步、被解析对象是谁」，不许退化成一句笼统的 500。
"""

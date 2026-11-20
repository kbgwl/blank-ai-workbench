/**
 * 一轮失败了，该怎么说，用户才知道下一步。
 *
 * 现状是 `reason` 原样端上来：红字一行，写着「模型请求超时：gpt-4o」或者
 * `all_models_failed`。它回答的是"出了什么事"，没回答"我现在干什么"。
 * 用户能做的只剩点重试——而有些失败重试一百次也一样（密钥错了、额度空了、
 * 这个模型压根不支持工具调用）。
 *
 * 所以拆成三层，顺序就是人遇到故障时的提问顺序：
 *
 * 1. `title`  发生了什么。一句人话，不出现模型名、不出现错误码。
 * 2. `action` 你现在能做什么。**能点得到**才算数；没有能做的事就留空，
 *             不画一个点了没反应的按钮。
 * 3. 技术详情（码 / 原始报错）默认折叠。它是排查依据，不是第一眼该读的东西。
 *
 * 纯函数，不 import React 也不 import 别名路径——node --test 能直接跑。
 */

/** 用户接下来最该做的那个动作。界面按它决定按钮长什么样。 */
export type FailureNext =
  /** 同样的要求再发一遍有意义：网络抖了一下、上游排队、模型偶尔不回。 */
  | "retry"
  /** 重试没用，要去改设置或换模型。 */
  | "settings"
  /** 做什么都没用，是这一轮自己结束了（用户点的取消、审批过期）。 */
  | "none";

export type FailureCopy = {
  /** 一句人话。空串表示"这次没什么可说的"（比如用户自己取消的）。 */
  title: string;
  /** 一句怎么办。空串表示没有能做的事。 */
  action: string;
  next: FailureNext;
  /** 登记过的错误码；没码时为空，界面就不画那一行。 */
  code: string;
};

/**
 * 按后端错误码查。码比报错文本稳定——同一句中文换个供应商就变了，
 * 码是 `ModelError` 里写死的字面量。所以先按码查，查不到再读文本。
 */
const BY_CODE: Record<string, FailureCopy> = {
  model_timeout: {
    title: "模型一直没回话，这一轮被掐断了。",
    action: "多半是上游在排队。再发一次通常就好；连着两次超时，说明这个模型现在不稳定，换一个更快。",
    next: "retry",
    code: "model_timeout",
  },
  model_unreachable: {
    title: "连不上模型服务。",
    action: "先确认网络是通的；如果用的是自建网关，检查地址和证书对不对。这是连接问题，重发同一句话不会好。",
    next: "settings",
    code: "model_unreachable",
  },
  model_http_error: {
    title: "模型服务拒了这次请求。",
    action: "看下面「技术详情」里的状态码：401 / 403 是密钥失效或没权限，429 是额度或频率到了，400 多半是参数被上游不认。改完再试。",
    next: "settings",
    code: "model_http_error",
  },
  model_stream_empty: {
    title: "模型回了空内容。",
    action: "有些网关不认流式返回；也有模型把额度全花在思考上、正文一个字没留。再发一次可能还是空——换个模型更快。",
    next: "settings",
    code: "model_stream_empty",
  },
  model_stream_error: {
    title: "回答说到一半，连接断了。",
    action: "再发一次。如果反复在同一句话上断，把这句话拆短一点。",
    next: "retry",
    code: "model_stream_error",
  },
  model_output_not_json: {
    title: "模型没按要求的格式回答。",
    action: "再发一次通常就好。连着三次一样的话，说明这件事的格式要求太长，把它拆成两轮做。",
    next: "retry",
    code: "model_output_not_json",
  },
  model_tools_unsupported: {
    title: "现在这个模型不支持调用工具。",
    action: "它只能聊天、不能读写文件跑命令，这一轮的事它做不了。换一个支持工具调用的模型。",
    next: "settings",
    code: "model_tools_unsupported",
  },
  all_models_failed: {
    title: "所有候选模型都试过了，没有一个能用。",
    action: "这不是偶发问题。去看模型配置和额度：密钥、余额、网关地址，三样里至少一样是坏的。",
    next: "settings",
    code: "all_models_failed",
  },
  approval_expired: {
    title: "审批卡放太久没人处理，这一轮已经收尾了。",
    action: "再发一次；这次审批卡一出来就点掉。",
    next: "retry",
    code: "approval_expired",
  },
  background_dispatched: {
    title: "这一轮转成后台任务在跑了。",
    action: "去后台任务清单看进度，这里不用等。",
    next: "none",
    code: "background_dispatched",
  },
  context_overflow: {
    title: "这次要读的东西太多了，塞不进模型的上下文。",
    action: "把范围收小：先让它只看某个目录、某个文件，或者把这次的事拆成两轮。",
    next: "settings",
    code: "context_overflow",
  },

  // ── 终态码（后端 agent/terminal.py 的 TERMINAL_CODES）───────────────────
  // 这些是"这一轮以什么方式结束"，由后端在同一处把事实翻成码。有了它们，
  // 这里就不必再拿中文去猜——下面 BY_TEXT 那一层只留给升级前的旧数据。
  budget_exhausted: {
    title: "这一轮把步数预算用完了，事没做完。",
    action: "把它拆小：一次只让它做一件事，或者直接说要哪一部分结果。重发一整句大要求只会再撞一次预算。",
    next: "retry",
    code: "budget_exhausted",
  },
  verification_blocked: {
    title: "独立事实校验没通过，这一轮不算完成。",
    action: "看下面那条具体问题：说了改了却没落盘、交付里假称测试通过、或者自己写着没做完。它不是模型的自评，是磁盘上的事实。",
    next: "retry",
    code: "verification_blocked",
  },
  validation_failed: {
    title: "自动化校验没通过，这一轮按失败结束。",
    action: "看下面贴出的命令与输出。要它继续修，直接把那段报错一起发过去。",
    next: "retry",
    code: "validation_failed",
  },
  validation_timeout: {
    title: "校验命令跑超时了，没拿到结论。",
    action: "这不算测试失败，是这条命令不适合在一次调用里跑完。让它只跑相关的那个测试文件，或者在设置里放宽超时。",
    next: "retry",
    code: "validation_timeout",
  },
  validation_error: {
    title: "校验命令没能跑起来。",
    action: "多半是环境或命令本身的问题（依赖没装、脚本名不对）。让它先确认这条命令能手工跑通。",
    next: "retry",
    code: "validation_error",
  },
  validation_blocked: {
    title: "同一条校验命令反复跑，结果没有变化，已经停下。",
    action: "再跑一遍还是同一个结果。先改代码里真正的原因，或者换一条更小的校验命令。",
    next: "retry",
    code: "validation_blocked",
  },
  validation_skipped: {
    title: "这一轮没有跑自动化校验。",
    action: "结论是按它自己说的给的，没有机器核对过。要确认就让它跑一次测试。",
    next: "retry",
    code: "validation_skipped",
  },
  hook_blocked: {
    title: "这条请求被一个钩子拦下了，没进模型。",
    action: "钩子是项目里配的规则。看下面的原话确认是你自己配的那条；要放行就改钩子配置。",
    next: "settings",
    code: "hook_blocked",
  },
  model_error: {
    title: "模型这一轮出错了。",
    action: "下面「技术详情」里是具体的码；按那个码判断要不要重试。",
    next: "retry",
    code: "model_error",
  },
  failed: {
    title: "这一轮失败了，但后端没给出更具体的原因。",
    action: "再发一次；反复失败就把这段对话的编号一起报出来。",
    next: "retry",
    code: "",
  },
  // 取消与暂停不是失败，但终态码会带上来。它们必须在这里登记：否则
  // `explainFailure("", "cancelled")` 会掉进"后端没留下原因"那条兜底——
  // 而原因明明就在码里。文案为空标题也算"没话说"，`title` 仍给一句人话。
  cancelled: {
    title: "这一轮被取消了。",
    action: "",
    next: "none",
    code: "cancelled",
  },
  paused: {
    title: "这一轮暂停了。",
    action: "",
    next: "none",
    code: "paused",
  },

  // ── verifier 的硬违规码（agent/verifier.py 的 BLOCKING_CODES）──────────
  // 它们是 `verification_blocked.detail`，比族码具体，所以优先命中。
  zero_change: {
    title: "它说改了，但工作区里没有任何文件变化。",
    action: "再发一次，并把要改哪个文件说清楚。如果你本来只是想问一下，明确说是问答，它就不该动文件。",
    next: "retry",
    code: "zero_change",
  },
  false_claim: {
    title: "交付里说测试通过了，但实际验证结果不支持。",
    action: "这一轮的结论不能直接用。让它把测试重跑一遍，并把输出贴出来。",
    next: "retry",
    code: "false_claim",
  },
  delivery_incomplete: {
    title: "交付里自己写了没做完。",
    action: "看它列出的「需要你」那部分：要你手工恢复的，多半是它做不到的事。把剩下的要求单独再发一轮。",
    next: "retry",
    code: "delivery_incomplete",
  },
  test_config_broken: {
    title: "这轮改动把测试配置弄坏了，之后一条测试都没跑成。",
    action: "让它先把测试命令修回来（或回滚那处配置），再继续做原来的事。",
    next: "retry",
    code: "test_config_broken",
  },
  zero_delivery: {
    title: "计划里要求改文件，但工作区里一个都没动。",
    action: "再发一次，先说清要改哪几个文件；也可以先让它只做一件最小的事。",
    next: "retry",
    code: "zero_delivery",
  },
};

/**
 * 文本兜底。后端大量 reason 是一整句中文（`ModelError` 的 message、钩子拦下的
 * 说明、空转拦截的判定），没有码可查。这里只认几种最常见的成因，
 * 认不出就老实说"没登记"，不要编一个看起来像原因的原因。
 */
const BY_TEXT: { match: RegExp; copy: FailureCopy }[] = [
  {
    match: /不支持工具|tools?_unsupported|tool.?calling/i,
    copy: BY_CODE.model_tools_unsupported,
  },
  {
    match: /上下文.{0,6}(超|满|长)|context.{0,8}(length|window|overflow)|too many tokens/i,
    copy: BY_CODE.context_overflow,
  },
  {
    match: /429|rate.?limit|限流|频率|quota|额度|余额/i,
    copy: {
      title: "被限流了：这一会儿的请求太多，或者额度用完了。",
      action: "等一两分钟再发。如果马上又撞上同一堵墙，那就是额度问题，去充值或换个密钥。",
      next: "settings",
      code: "",
    },
  },
  {
    match: /401|403|unauthorized|forbidden|invalid.?api.?key|密钥|未授权|认证/i,
    copy: {
      title: "密钥不对，或者没有这个模型的权限。",
      action: "去模型设置里换一个能用的密钥。重发不会好。",
      next: "settings",
      code: "",
    },
  },
  {
    match: /超时|timeout|timed out|ETIMEDOUT/i,
    copy: BY_CODE.model_timeout,
  },
  {
    match: /ECONNREFUSED|ENOTFOUND|unreachable|连接被拒绝|无法连接|连不上|网络不可达/i,
    copy: BY_CODE.model_unreachable,
  },
  {
    match: /钩子|hook/i,
    copy: {
      title: "这件事被一个钩子拦下了。",
      action: "钩子是项目里配的规则，不是它的判断。看下面的原话，确认是你自己配的那条；要放行就改钩子配置。",
      next: "settings",
      code: "",
    },
  },
  {
    match: /独立审查未通过|guardian|审查/i,
    copy: {
      title: "独立审查没通过，这一步没被执行。",
      action: "下面写了审查的具体理由。要么换个做法，要么这次先不审查。",
      next: "settings",
      code: "",
    },
  },
  {
    match: /取消|cancel/i,
    copy: {
      title: "这一轮被取消了。",
      action: "",
      next: "none",
      code: "",
    },
  },
];

/**
 * 一段陌生的、又长又像报错的文本，值不值得占正文一块地方。
 * 英文栈、`Traceback`、HTTP 状态码行——这些对用户是噪音，但对排查是唯一线索，
 * 所以不丢弃，只是塞进折叠区。
 */
function looksTechnical(text: string): boolean {
  return /traceback|exception|error:|at\s+\w+\.|\.py", line|\{"|<\/?[a-z]+>/i.test(text);
}
/**
 * 把一段 reason 说成人话。
 *
 * `code` 是后端 `extra` 里带的稳定标识（`ModelError.code`），有就优先用；
 * 没有就从文本里认。两个都没有，退回「这一轮没跑完」——不装懂。
 */
export function explainFailure(reason: string, code = ""): FailureCopy {
  const raw = String(reason ?? "").trim();
  const key = String(code ?? "").trim().toLowerCase();
  if (key && BY_CODE[key]) return BY_CODE[key];

  const asCode = raw.toLowerCase();
  if (BY_CODE[asCode]) return BY_CODE[asCode];

  for (const entry of BY_TEXT) {
    if (entry.match.test(raw)) return entry.copy;
  }

  if (!raw) {
    return {
      title: "这一轮没跑完，但后端没留下原因。",
      action: "再发一次；如果每次都这样，把这段对话的编号一起报出来。",
      next: "retry",
      code: "",
    };
  }

  // 认不出的码：把码留在详情里，标题说人话。
  if (/^[a-z][a-z0-9_]{2,39}$/.test(raw)) {
    return {
      title: "这一轮没跑完。",
      action: "再发一次；反复失败的话，把下面那个码报出来。",
      next: "retry",
      code: raw,
    };
  }

  // 英文栈 / JSON 残片 / HTML：它不是一句话，是一段日志。抬成标题只会把
  // 一行正常大小的红字撑成半屏乱码，所以标题说人话，原文全部进折叠区。
  if (looksTechnical(raw)) {
    return {
      title: "这一轮报了个错，没能跑完。",
      action: "原始报错收在下面。看不懂就把那一段发出来，比截屏有用。",
      next: "retry",
      code: "",
    };
  }

  // 长中文句：后端已经说清了"发生了什么"，缺的只是"怎么办"。
  // 所以原句抬成标题，动作给通用的一条——不硬编一个可能对不上的建议。
  // 但标题只留第一句：后端有时把三条失败原因用"；"串成一段，整段当标题没人读完。
  const head = raw.split(/[。；\n]/).map((part) => part.trim()).find(Boolean) ?? raw;
  const title = head.length > 72 ? `${head.slice(0, 72)}…` : head;
  return {
    title: /[。；]$/.test(head) ? head : `${title}。`,
    action: /失败|错误|异常|无法|不能/.test(raw)
      ? "再发一次；同样的错连着出现，说明不是偶发，看下面的原始报错。"
      : "",
    next: "retry",
    code: "",
  };
}

/** 折叠区第一行：错误码 / 状态 / 时间。空的部分不占位。 */
export function failureMeta(code: string, at: string): string[] {
  const rows: string[] = [];
  if (code.trim()) rows.push(`code ${code.trim()}`);
  const stamp = String(at ?? "").trim();
  if (stamp) rows.push(stamp);
  return rows;
}

/** 原始报错该不该原样进折叠区。太短的一律不进——"失败"两个字展开也是"失败"。 */
export function failureOriginal(reason: string): string {
  const raw = String(reason ?? "").trim();
  if (raw.length < 8) return "";
  return raw;
}

/** 一整段裸标识符：`approval_expired`、`model_timeout`。它是个码，不是一句报错。 */
function isBareCode(text: string): boolean {
  return /^[a-z][a-z0-9_]{2,39}$/.test(text);
}

/**
 * 折叠区该分两栏还是一栏。
 *
 * 老数据里 `reason` 本身就是一个裸码（`approval_expired`），没有单独的 `code` 字段。
 * 如果照"长于 8 个字就当成原始报错"处理，它会掉进正文那一栏，在时间下面再印一遍
 * 顶上已经翻译过的那个词——用户读到的是一段重复，还以为漏了什么。
 *
 * 所以裸码一律归到第一行（`code approval_expired · 18:09`），正文栏只放真的原文。
 */
export function failureParts(reason: string, code: string, at: string): { meta: string[]; original: string } {
  const raw = String(reason ?? "").trim();
  const explicit = String(code ?? "").trim();
  const bare = !explicit && isBareCode(raw) ? raw : "";
  return {
    meta: failureMeta(explicit || bare, at),
    original: bare ? "" : failureOriginal(raw),
  };
}

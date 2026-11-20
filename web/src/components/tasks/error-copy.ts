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

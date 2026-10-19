export type ReportTone = "done" | "undone" | "next";

export type ReportSection = { title: string; tone: ReportTone; items: string[] };

export type ParsedReport = { sections: ReportSection[]; body: string };

const TONES: Record<string, { tone: ReportTone; title: string }> = {
  已完成: { tone: "done", title: "已完成" },
  已做: { tone: "done", title: "已完成" },
  未完成: { tone: "undone", title: "未完成" },
  待做: { tone: "undone", title: "未完成" },
  下一步: { tone: "next", title: "下一步" },
  建议: { tone: "next", title: "下一步" },
};

const TITLE_RE = /^\s*(?:#{1,4}\s*)?\*{0,2}(已完成|已做|未完成|待做|下一步|建议)\*{0,2}\s*[:：]?\s*$/;

/**
 * 把交付文本拆成「已完成 / 未完成 / 下一步」三段。
 *
 * 只认行首独立成行的标题，正文里偶尔提到"下一步"不会被误切。
 * 没有这三段的（直接回答的路径）返回空 sections，交给富文本整段渲染。
 */
export function splitReport(text: string): ParsedReport {
  const lines = (text ?? "").replace(/\r\n/g, "\n").split("\n");
  const sections: ReportSection[] = [];
  let current: ReportSection | null = null;
  const loose: string[] = [];

  for (const line of lines) {
    const match = TITLE_RE.exec(line);
    if (match) {
      const meta = TONES[match[1]];
      if (meta) {
        current = { title: meta.title, tone: meta.tone, items: [] };
        sections.push(current);
        continue;
      }
    }
    if (!current) {
      loose.push(line);
      continue;
    }
    const cleaned = line.replace(/^\s*(?:[-*•]|\d+[.、)])\s*/, "").trim();
    if (cleaned) current.items.push(cleaned);
    else if (current.items.length > 0 && current.items[current.items.length - 1] !== "") {
      // 段内空行不产生空条目，避免渲染出多余留白
    }
  }

  // 只有一段时大概率不是三段式（模型只写了"下一步"），按正文处理更稳妥
  if (sections.length < 2) {
    return { sections: [], body: (text ?? "").trim() };
  }
  return { sections, body: loose.join("\n").trim() };
}

export function isTrivialItem(item: string): boolean {
  return /^(无|没有|暂无|无。|暂无。|n\/a|-)$/i.test(item.trim());
}

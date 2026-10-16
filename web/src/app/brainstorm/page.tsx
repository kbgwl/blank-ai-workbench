"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { chatProvider } from "@/components/settings/api";
import { createTask, fetchProjects, fetchProviders, type Project, type Provider, type Task } from "@/components/tasks/api";

type Idea = { name: string; summary: string; upside: string; downside: string; effort: string };

const starterIdeas: Idea[] = [
  { name: "最小改造", summary: "在现有模块边界内增加能力，先验证需求。", upside: "成本低，容易回滚。", downside: "长期扩展性一般。", effort: "1–2 天" },
  { name: "独立模块", summary: "抽出清晰接口，把新能力作为可替换模块。", upside: "边界清楚，后续容易扩展。", downside: "初始设计成本更高。", effort: "3–5 天" },
  { name: "平台化能力", summary: "建立通用协议和插件接口，支持更多场景。", upside: "复用性和长期上限最高。", downside: "容易过度设计。", effort: "1–2 周" },
];

function parseIdeas(text: string): Idea[] {
  const json = text.match(/\{[\s\S]*\}/)?.[0];
  if (!json) return [];
  try {
    const value = JSON.parse(json) as { ideas?: unknown };
    if (!Array.isArray(value.ideas)) return [];
    return value.ideas
      .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
      .map((item) => ({
        name: String(item.name ?? "未命名方案"),
        summary: String(item.summary ?? ""),
        upside: String(item.upside ?? "未说明"),
        downside: String(item.downside ?? "未说明"),
        effort: String(item.effort ?? "待评估"),
      }))
      .filter((idea) => idea.summary)
      .slice(0, 5);
  } catch {
    return [];
  }
}

export default function BrainstormPage() {
  const [question, setQuestion] = useState("");
  const [ideas, setIdeas] = useState<Idea[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [projectId, setProjectId] = useState("");
  const [providerId, setProviderId] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [createdTask, setCreatedTask] = useState<Task | null>(null);

  useEffect(() => {
    Promise.all([fetchProjects(), fetchProviders()]).then(([nextProjects, nextProviders]) => {
      setProjects(nextProjects);
      setProviders(nextProviders);
      setProjectId(nextProjects[0]?.id ?? "");
      setProviderId(nextProviders.find((provider) => provider.enabled)?.id ?? "");
    }).catch(() => setNotice("无法读取项目或模型配置；仍可使用本地方案模板。"));
  }, []);

  const selectedIdea = useMemo(() => ideas.find((idea) => idea.name === selected) ?? null, [ideas, selected]);

  async function start() {
    const prompt = question.trim();
    if (!prompt || busy) return;
    setBusy(true);
    setNotice("");
    setCreatedTask(null);
    try {
      if (!providerId) throw new Error("没有可用模型");
      const response = await chatProvider(providerId, `围绕以下问题提出最多 5 个可执行方案：${prompt}\n只返回 JSON，不要 Markdown，格式必须是 {"ideas":[{"name":"方案名","summary":"方案概述","upside":"主要收益","downside":"主要代价或风险","effort":"预计工作量"}]}`);
      const parsed = parseIdeas(response.text);
      if (!parsed.length) throw new Error("模型没有返回可解析方案");
      setIdeas(parsed);
      setSelected(parsed[0].name);
    } catch {
      setIdeas(starterIdeas.map((idea) => ({ ...idea, summary: `${idea.summary} 针对：${prompt}` })));
      setSelected(starterIdeas[0].name);
      setNotice("模型方案不可用，已切换为本地模板；配置模型后可重新发散。 ");
    } finally {
      setBusy(false);
    }
  }

  async function convertToTask() {
    if (!selectedIdea || !projectId || busy) return;
    setBusy(true);
    setNotice("");
    try {
      const task = await createTask(projectId, {
        projectId,
        providerId: providerId || undefined,
        title: `${selectedIdea.name}：${question.trim() || "头脑风暴方案"}`,
        prompt: `${question.trim()}\n\n选定方案：${selectedIdea.name}\n方案：${selectedIdea.summary}\n收益：${selectedIdea.upside}\n代价与风险：${selectedIdea.downside}\n预计工作量：${selectedIdea.effort}`,
        acceptanceCriteria: ["实现选定方案", "补充对应测试", "保留方案中的约束和风险说明"],
      });
      setCreatedTask(task);
      setNotice("已创建开发任务。 ");
    } catch {
      setNotice("创建任务失败，请先确认后端已启动并选择一个项目。 ");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 p-6">
      <header>
        <div className="text-[11px] text-muted">空白格 / 头脑风暴</div>
        <h1 className="mt-2 text-xl font-semibold tracking-tight">先想清楚，再让 Agent 动手</h1>
        <p className="mt-1 max-w-2xl text-[13px] leading-relaxed text-muted">模型只负责给出可比较的方案；确认后直接生成带约束和验收条件的开发任务。</p>
      </header>
      <section className="rounded-lg border border-line bg-surface/25 p-4">
        <textarea value={question} onChange={(event) => setQuestion(event.target.value)} rows={3} placeholder="例如：如何给系统增加插件机制？" className="w-full resize-none bg-transparent text-[14px] leading-relaxed text-text outline-none placeholder:text-faint" />
        <div className="mt-3 flex flex-wrap items-center justify-between gap-3 border-t border-line pt-3">
          <div className="flex items-center gap-3 font-mono text-[10px] text-faint">
            <span>技术方案 · Debug · 产品拆解</span>
            <select value={providerId} onChange={(event) => setProviderId(event.target.value)} className="bg-transparent text-muted outline-none" aria-label="选择模型">
              <option value="">本地模板</option>
              {providers.filter((provider) => provider.enabled).map((provider) => <option key={provider.id} value={provider.id}>{provider.name} · {provider.model}</option>)}
            </select>
          </div>
          <button type="button" onClick={start} disabled={busy || !question.trim()} className="rounded-md bg-brand px-3 py-1.5 text-[11px] font-medium text-bg hover:bg-brand/85 disabled:cursor-not-allowed disabled:opacity-40">{busy ? "处理中…" : "开始发散"}</button>
        </div>
      </section>
      {notice && <div className="rounded-md border border-line px-3 py-2 text-[11px] text-muted">{notice}</div>}
      {ideas.length > 0 && <section className="grid grid-cols-1 gap-2 lg:grid-cols-3">
        {ideas.map((idea) => <article key={idea.name} className={`flex min-h-56 flex-col rounded-lg border p-4 ${selected === idea.name ? "border-brand/70 bg-brand/5" : "border-line bg-surface/25"}`}>
          <button type="button" onClick={() => setSelected(idea.name)} className="flex items-center justify-between text-left"><h2 className="text-[13px] font-medium">{idea.name}</h2><span className="font-mono text-[10px] text-faint">{idea.effort}</span></button>
          <p className="mt-4 text-[12px] leading-relaxed text-text/85">{idea.summary}</p>
          <dl className="mt-5 space-y-2 text-[11px]"><div><dt className="text-faint">收益</dt><dd className="mt-0.5 text-muted">{idea.upside}</dd></div><div><dt className="text-faint">代价</dt><dd className="mt-0.5 text-muted">{idea.downside}</dd></div></dl>
          <button type="button" onClick={() => setSelected(idea.name)} className="mt-auto border-t border-line pt-3 text-left text-[11px] text-brand hover:text-text">{selected === idea.name ? "已选择" : "选择方案"}</button>
        </article>)}
      </section>}
      {ideas.length > 0 && <section className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-line bg-surface/25 p-4">
        <div className="flex items-center gap-3 text-[11px] text-muted"><span>已选：{selectedIdea?.name ?? "未选择"}</span><select value={projectId} onChange={(event) => setProjectId(event.target.value)} className="rounded border border-line bg-transparent px-2 py-1 outline-none" aria-label="选择项目"><option value="">选择项目</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></div>
        <button type="button" onClick={convertToTask} disabled={!selectedIdea || !projectId || busy} className="rounded-md bg-brand px-3 py-1.5 text-[11px] font-medium text-bg hover:bg-brand/85 disabled:cursor-not-allowed disabled:opacity-40">转为开发任务 →</button>
      </section>}
      {createdTask && <div className="text-[11px] text-muted">任务已创建：<Link className="text-brand hover:underline" href={`/tasks/detail?id=${createdTask.id}`}>{createdTask.title}</Link></div>}
      {ideas.length === 0 && <div className="py-16 text-center font-mono text-[11px] text-faint">输入问题后，方案会以卡片形式出现。</div>}
    </div>
  );
}

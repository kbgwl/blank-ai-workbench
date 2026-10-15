"use client";

import Link from "next/link";
import { useState } from "react";
import { createTask } from "@/components/tasks/api";


const evidence = [
  { kind: "入口", value: "main / app bootstrap", detail: "待接入 REA MCP" },
  { kind: "调用关系", value: "bootstrap → runtime → renderer", detail: "证据链占位" },
  { kind: "行为结论", value: "目标尚未开始分析", detail: "导入目标后生成" },
];

export default function ReverseLabPage() {
  const [target, setTarget] = useState("");
  const [question, setQuestion] = useState("这个目标的启动流程和关键功能入口是什么？");
  const [projectId, setProjectId] = useState("");
  const [notice, setNotice] = useState("");

  async function createRebuildTask() {
    if (!projectId.trim() || !target.trim()) {
      setNotice("请先填写目标路径和项目 ID；接入 REA 后这里会改为项目选择器。");
      return;
    }
    try {
      await createTask(projectId.trim(), {
        projectId: projectId.trim(),
        title: `重建逆向目标：${target.trim()}`,
        prompt: `逆向目标：${target.trim()}\n调查问题：${question.trim()}\n\n请基于保存的证据重建行为，不要凭空推测。`,
        acceptanceCriteria: ["记录目标类型和分析范围", "保存关键调用关系与证据", "实现重建行为并补充验证"],
      });
      setNotice("重建任务已创建，可在任务工作台继续执行。");
    } catch {
      setNotice("任务创建失败，请确认后端已启动、项目 ID 有效。");
    }
  }

  return (
    <div className="mx-auto grid w-full max-w-7xl gap-4 p-6 xl:grid-cols-[17rem_1fr_22rem]">
      <aside className="rounded-lg border border-line bg-surface/20 p-4">
        <div className="font-mono text-[10px] text-faint">REVERSE LAB</div>
        <h1 className="mt-2 text-[15px] font-semibold">逆向分析项目</h1>
        <p className="mt-2 text-[12px] leading-relaxed text-muted">把目标、调查、证据和重建任务放在同一条链路里。</p>
        <label className="mt-6 block text-[11px] text-faint">目标文件 / URL</label>
        <input value={target} onChange={(event) => setTarget(event.target.value)} placeholder="app.exe / app.asar / https://…" className="mt-2 w-full rounded border border-line bg-transparent px-2 py-2 text-[12px] outline-none focus:border-brand" />
        <label className="mt-4 block text-[11px] text-faint">目标项目 ID</label>
        <input value={projectId} onChange={(event) => setProjectId(event.target.value)} placeholder="用于创建重建任务" className="mt-2 w-full rounded border border-line bg-transparent px-2 py-2 text-[12px] outline-none focus:border-brand" />
        <Link href="/features" className="mt-5 inline-block text-[11px] text-brand hover:underline">按需启用依赖 →</Link>
      </aside>
      <main className="rounded-lg border border-line bg-surface/20 p-5">
        <div className="flex items-center justify-between gap-3"><div><div className="font-mono text-[10px] text-faint">CURRENT INVESTIGATION</div><h2 className="mt-2 text-[16px] font-semibold">调查问题</h2></div><span className="rounded border border-warn/30 px-2 py-1 text-[10px] text-warn">REA Adapter 待接入</span></div>
        <textarea value={question} onChange={(event) => setQuestion(event.target.value)} rows={4} className="mt-5 w-full resize-none rounded border border-line bg-transparent p-3 text-[13px] leading-relaxed outline-none focus:border-brand" />
        <div className="mt-5 border-t border-line pt-4"><div className="font-mono text-[10px] text-faint">调查步骤</div><ol className="mt-3 space-y-3 text-[12px] text-muted"><li>01 · 识别目标类型与入口</li><li>02 · 搜索符号、字符串和关键资源</li><li>03 · 追踪调用关系与运行时行为</li><li>04 · 保存证据并转为重建任务</li></ol></div>
        <button type="button" onClick={() => setNotice("分析队列已准备；安装 REA Adapter 后将执行真实分析。 ")} className="mt-6 rounded-md bg-brand px-3 py-2 text-[11px] font-medium text-bg hover:bg-brand/85">开始分析</button>
      </main>
      <aside className="rounded-lg border border-line bg-surface/20 p-4"><div className="font-mono text-[10px] text-faint">EVIDENCE</div><h2 className="mt-2 text-[15px] font-semibold">证据与结果</h2><div className="mt-5 space-y-3">{evidence.map((item) => <div key={item.kind} className="border-b border-line pb-3"><div className="text-[10px] text-faint">{item.kind}</div><div className="mt-1 text-[12px] text-text">{item.value}</div><div className="mt-1 text-[10px] text-muted">{item.detail}</div></div>)}</div><button type="button" onClick={createRebuildTask} className="mt-6 w-full rounded-md border border-line px-3 py-2 text-[11px] text-muted hover:border-brand hover:text-text">转为重建任务 →</button>{notice && <p className="mt-3 text-[11px] leading-relaxed text-muted">{notice}</p>}</aside>
    </div>
  );
}

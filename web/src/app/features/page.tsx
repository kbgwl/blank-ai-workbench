"use client";

import { useState } from "react";
import { PageBody, PageHeader } from "@/components/page";
import { readJson, removeString, writeJson } from "@/lib/safe-storage";

type Feature = {
  id: string;
  name: string;
  description: string;
  dependency: string;
  status: "core" | "installed" | "available";
};

const DEFAULT_FEATURES: Feature[] = [
  { id: "coding", name: "Coding Agent", description: "读取项目、修改文件、执行测试和查看 Diff。", dependency: "核心内置", status: "core" },
  { id: "brainstorm", name: "头脑风暴", description: "把模糊想法收敛成方案、约束和验收条件。", dependency: "无额外依赖", status: "installed" },
  { id: "reverse-lab", name: "Reverse Lab", description: "使用 REA 分析二进制、Electron 和浏览器行为。", dependency: "REA / Ghidra（按需）", status: "available" },
  { id: "browser", name: "Browser Tools", description: "观察页面行为、网络请求和交互结果。", dependency: "Playwright（按需）", status: "available" },
  { id: "project-map", name: "Project Map", description: "构建模块、依赖、测试和影响范围画像。", dependency: "后台索引服务", status: "available" },
  { id: "model-arena", name: "Model Arena", description: "用多个模型跑同一任务并比较交付结果。", dependency: "多模型配置", status: "available" },
];

const STORAGE_KEY = "blank.features";

function initialFeatures(): Feature[] {
  const saved = readJson<unknown>(STORAGE_KEY, null);
  if (!Array.isArray(saved) || !saved.every((item) => typeof item === "string")) {
    if (saved !== null) removeString(STORAGE_KEY);
    return DEFAULT_FEATURES;
  }
  const ids = new Set<string>(saved);
  return DEFAULT_FEATURES.map((feature): Feature => ({ ...feature, status: feature.status === "core" ? "core" : ids.has(feature.id) ? "installed" : "available" }));
}

export default function FeaturesPage() {
  const [features, setFeatures] = useState<Feature[]>(initialFeatures);

  function toggle(id: string) {
    setFeatures((current) => {
      const next: Feature[] = current.map((feature) => feature.id === id && feature.status !== "core" ? { ...feature, status: feature.status === "installed" ? "available" : "installed" } : feature);
      writeJson(STORAGE_KEY, next.filter((feature) => feature.status === "installed" || feature.status === "core").map((feature) => feature.id));
      return next;
    });
  }

  return (
    <PageBody>
      <PageHeader
        title="按需加载功能"
        description="核心保持轻量。启用功能后，相关工具、提示词和可选依赖才会进入当前工作区。"
      />
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {features.map((feature) => (
          <article key={feature.id} className="flex min-h-32 flex-col justify-between rounded-lg border border-line bg-surface p-4 transition-colors hover:border-line-strong">
            <div>
              <div className="flex items-center gap-2">
                <span className={`size-1.5 rounded-full ${feature.status === "core" || feature.status === "installed" ? "bg-accent" : "bg-faint"}`} />
                <h2 className="text-[14px] font-medium">{feature.name}</h2>
                <span className="ml-auto font-mono text-[11px] text-faint">{feature.status === "core" ? "核心" : feature.status === "installed" ? "已启用" : "未安装"}</span>
              </div>
              <p className="mt-3 text-[13px] leading-relaxed text-muted">{feature.description}</p>
            </div>
            <div className="mt-4 flex items-center justify-between gap-3">
              <span className="font-mono text-[11px] text-faint">{feature.dependency}</span>
              {feature.status !== "core" && <button type="button" onClick={() => toggle(feature.id)} className={`rounded-md px-2.5 py-1.5 text-[12px] ${
                feature.status === "installed"
                  ? "bg-surface-2 text-muted hover:text-text"
                  : "bg-brand text-on-brand hover:bg-brand/90"
              }`}>{feature.status === "installed" ? "停用" : "启用"}</button>}
            </div>
          </article>
        ))}
      </div>
      <p className="font-mono text-[11px] text-faint">重型依赖会在首次使用时询问安装；当前页面保存的是本机功能开关。</p>
    </PageBody>
  );
}

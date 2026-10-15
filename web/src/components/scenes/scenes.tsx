"use client";

import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

export type SceneId = "coding" | "docs" | "research" | "brainstorm" | "reverse" | "daily";
export type Scene = { id: SceneId; name: string; short: string; description: string; instruction: string; defaultCriteria: string[]; modes?: string[] };

export const SCENES: Scene[] = [
  { id: "coding", name: "开发", short: "代码、测试、Diff", description: "修改项目、执行测试、交付可验证 Diff。", instruction: "你处于开发场景。优先读取现有代码，实际修改文件并运行相关测试；回复只保留动作、结果和阻塞。", defaultCriteria: ["实现需求", "补充或更新测试", "相关测试通过并说明未验证项"] },
  { id: "docs", name: "文档", short: "说明、方案、知识库", description: "编写、整理和校对技术文档。", instruction: "你处于文档场景。先确认读者、目的和格式，直接编辑文档；回复只保留变更文件、要点和待确认内容。", defaultCriteria: ["内容结构清晰", "示例和链接可用", "术语和格式统一"] },
  { id: "research", name: "研究", short: "调研、比较、结论", description: "收集证据、比较方案并形成结论。", instruction: "你处于研究场景。区分事实、推断和未知，保留来源或证据；回复只保留结论、证据和下一步。", defaultCriteria: ["列出问题和范围", "结论有证据支撑", "明确风险和下一步"] },
  { id: "brainstorm", name: "头脑风暴", short: "发散、收敛、决策", description: "从模糊想法收敛为方案和验收条件。", instruction: "你处于头脑风暴场景。先给少量可比较方案，再明确取舍和验收条件；不要直接执行代码。", defaultCriteria: ["至少比较两个方案", "写清收益、代价和风险", "形成可执行验收条件"] },
  { id: "reverse", name: "逆向分析", short: "目标、证据、重建", description: "分析应用行为、调用关系和重建路径。", instruction: "你处于逆向分析场景。只基于目标和证据描述行为，区分观察与推断，保存证据后再生成重建任务。", defaultCriteria: ["记录分析目标和范围", "保存关键证据与调用关系", "重建结果通过行为验证"] },
  { id: "daily", name: "日常", short: "随手分享、记忆、整理", description: "随时把想法、偏好和待办交给 Agent。", instruction: "你处于日常场景。先理解用户分享的内容，区分临时信息、待办和长期偏好；未经用户确认不要写入长期记忆或修改 Skill。回复简短，只给下一步。", defaultCriteria: ["正确区分临时信息与长期偏好", "需要记忆的内容经过用户确认", "输出可执行的下一步"], modes: ["仅用于本次", "记住这个习惯"] },
];

type SceneContextValue = { scene: Scene; setSceneId: (id: SceneId) => void };
const SceneContext = createContext<SceneContextValue | null>(null);
const STORAGE_KEY = "blank.scene";

export function SceneProvider({ children }: { children: ReactNode }) {
  const [sceneId, setSceneId] = useState<SceneId>(() => {
    if (typeof window === "undefined") return "coding";
    const saved = window.localStorage.getItem(STORAGE_KEY) as SceneId | null;
    return SCENES.some((scene) => scene.id === saved) ? saved! : "coding";
  });
  const scene = useMemo(() => SCENES.find((item) => item.id === sceneId) ?? SCENES[0], [sceneId]);
  function changeScene(id: SceneId) {
    setSceneId(id);
    window.localStorage.setItem(STORAGE_KEY, id);
  }
  return <SceneContext.Provider value={{ scene, setSceneId: changeScene }}>{children}</SceneContext.Provider>;
}

export function useScene() {
  const value = useContext(SceneContext);
  if (!value) throw new Error("useScene must be used inside SceneProvider");
  return value;
}

export function SceneSwitcher() {
  const { scene, setSceneId } = useScene();
  return <label className="flex items-center gap-2 rounded-md border border-line bg-surface/40 px-2 py-1 text-[11px] text-muted"><span className="text-faint">场景</span><select value={scene.id} onChange={(event) => setSceneId(event.target.value as SceneId)} className="bg-transparent text-text outline-none" aria-label="选择工作场景">{SCENES.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.short}</option>)}</select></label>;
}

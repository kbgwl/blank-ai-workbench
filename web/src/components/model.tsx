"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { fetchProviders, type Provider } from "@/components/settings/api";

/**
 * 全站只共享一件事：当前用哪个模型。
 *
 * 这里曾经还有一个「场景」维度（开发 / 文档 / 研究 / 头脑风暴 / 逆向 / 日常），
 * 每个场景自带一段 instruction 和一组默认验收条件，发送前拼在用户原话前面。
 * 它已经被移除，原因有二：
 *
 * 1. 那层前缀是纯粹的污染。判定意图时它满是「读取代码、修改文件、跑测试」，
 *    连「你现在的工作目录是哪里」都会被判成改代码任务；模型也被它带跑。
 * 2. 场景是用户在发送前替自己做的分类，而分类本来就该由 Agent 看着这句话自己判。
 *    猜错一次，整轮的验收条件就全错，用户还得回头改一遍。
 *
 * 所以现在发出去的就是用户原话本身，一个字节都不加。
 */

const MODEL_STORAGE_KEY = "blank.model";

type ModelContextValue = { providers: Provider[]; modelId: string; setModelId: (id: string) => void };
const ModelContext = createContext<ModelContextValue | null>(null);

export function ModelProvider({ children }: { children: ReactNode }) {
  const [providers, setProviders] = useState<Provider[]>([]);
  const [modelId, setModelIdState] = useState(() => typeof window === "undefined" ? "" : window.localStorage.getItem(MODEL_STORAGE_KEY) ?? "");
  useEffect(() => {
    const saved = window.localStorage.getItem(MODEL_STORAGE_KEY) ?? "";
    void fetchProviders().then((next) => {
      const enabled = next.filter((provider) => provider.enabled);
      setProviders(enabled);
      if (!saved || !enabled.some((provider) => provider.id === saved)) {
        setModelIdState(enabled[0]?.id ?? "");
      }
    }).catch(() => setProviders([]));
  }, []);
  function setModelId(id: string) {
    setModelIdState(id);
    window.localStorage.setItem(MODEL_STORAGE_KEY, id);
  }
  return <ModelContext.Provider value={{ providers, modelId, setModelId }}>{children}</ModelContext.Provider>;
}

export function useModel() {
  const value = useContext(ModelContext);
  if (!value) throw new Error("useModel must be used inside ModelProvider");
  return value;
}

export function ModelSwitcher() {
  const { providers, modelId, setModelId } = useModel();
  const selected = providers.find((provider) => provider.id === modelId);
  return <label className="flex items-center gap-2 rounded-md border border-line bg-surface/40 px-2 py-1 text-[11px] text-muted">
    <span className="text-faint">模型</span>
    <select value={modelId} onChange={(event) => setModelId(event.target.value)} className="max-w-52 bg-transparent text-text outline-none" aria-label="选择模型" disabled={providers.length === 0}>
      {providers.length === 0 ? <option value="">未配置模型</option> : providers.map((provider) => <option key={provider.id} value={provider.id}>{provider.name} · {provider.model}</option>)}
    </select>
    {selected && <span className="hidden text-faint lg:inline">{selected.kind}</span>}
  </label>;
}

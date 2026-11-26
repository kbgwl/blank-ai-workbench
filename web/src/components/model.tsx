"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { fetchProviders, type Provider } from "@/components/settings/api";
import { defaultProviderId } from "@/components/bot/provider-choice";
import { configuredProviders } from "@/components/configured-providers";
import { readString, writeString } from "@/lib/safe-storage";
import { SelectPopover } from "./ui/select-popover";

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
  const [modelId, setModelIdState] = useState(() => readString(MODEL_STORAGE_KEY));
  useEffect(() => {
    const saved = readString(MODEL_STORAGE_KEY);
    void fetchProviders().then((next) => {
      // 只把「有 Key」的供应商放进切换器：没 Key 的选中也是 401。
      // 一家都没配时保持空列表，显示「未配置模型」，不再把没 Key 的模板厂商
      // 塞回下拉里——用户在设置页补好 Key 后这里会自动出现。
      const visible = configuredProviders(next);
      setProviders(visible);
      // 存过的选择已失效（删除/停用/没 Key）就回到后端默认那一家，
      // 而不是拿列表第一项顶替——那会显示 anthropic、实际跑 DeepSeek。
      if (!saved || !visible.some((provider) => provider.id === saved)) {
        setModelIdState(defaultProviderId(next) || visible[0]?.id || "");
      }
    }).catch(() => setProviders([]));
  }, []);
  function setModelId(id: string) {
    setModelIdState(id);
    writeString(MODEL_STORAGE_KEY, id);
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
  return <label className="flex items-center gap-2 rounded-md border border-line bg-surface px-2 py-1 text-[12px] text-muted">
    <span className="text-faint">模型</span>
    <SelectPopover size="compact" ariaLabel="选择模型" value={modelId}
      disabled={providers.length === 0}
      placeholder="未配置模型"
      options={providers.length === 0 ? [] : providers.map((provider) => ({ value: provider.id, label: `${provider.name} · ${provider.model}` }))}
      onChange={setModelId} menuMinWidth={220} />
  </label>;
}

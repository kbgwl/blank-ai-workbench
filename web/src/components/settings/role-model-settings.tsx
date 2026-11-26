"use client";

import { useEffect, useState } from "react";
import { readJson, writeJson } from "@/lib/safe-storage";
import { SelectPopover } from "../ui/select-popover";
import { fetchProviders, type Provider } from "./api";
import { configuredProviders } from "../configured-providers";

const STORAGE_KEY = "blank.model.roles";
const ROLES = [
  ["planner", "Planner · 规划"],
  ["coder", "Coder · 执行"],
  ["reviewer", "Reviewer · 审查"],
] as const;

export function RoleModelSettings() {
  const [providers, setProviders] = useState<Provider[]>([]);
  const [roles, setRoles] = useState<Record<string, string>>(() => readJson<Record<string, string>>(STORAGE_KEY, {}));
  useEffect(() => {
    void fetchProviders().then((rows) => setProviders(configuredProviders(rows))).catch(() => setProviders([]));
  }, []);
  function update(role: string, providerId: string) {
    const next = { ...roles };
    if (providerId) next[role] = providerId;
    else delete next[role];
    setRoles(next);
    writeJson(STORAGE_KEY, next);
  }
  return <div className="mt-3 grid gap-2 sm:grid-cols-3">
    {ROLES.map(([role, label]) => <label key={role} className="flex flex-col gap-1 text-[12px] text-muted">
      <span>{label}</span>
      <SelectPopover
        size="compact"
        ariaLabel={`选择${label}模型`}
        value={roles[role] ?? ""}
        disabled={providers.length === 0}
        options={[
          { value: "", label: "跟随主模型" },
          ...providers.map((provider) => ({ value: provider.id, label: `${provider.name} · ${provider.model}` })),
        ]}
        onChange={(value) => update(role, value)}
        menuMinWidth={220}
      />
    </label>)}
  </div>;
}

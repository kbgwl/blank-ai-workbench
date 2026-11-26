/**
 * 模型选择器只列「启用且填了 API Key」的供应商。
 *
 * 没配 Key 的供应商选中也发不出请求（401）。各页过去各写一套过滤，
 * 结果聊天输入框把它们列成禁用项、任务页还能选，界面不一致。
 *
 * 设置页的供应商配置列表是例外：那里要显示全部，用户才能选中去补 Key。
 */

export type ConfiguredProvider = {
  enabled: boolean;
  api_key_hint?: string | null;
};

export function configuredProviders<T extends ConfiguredProvider>(
  providers: readonly T[],
): T[] {
  return providers.filter(
    (provider) => provider.enabled && (provider.api_key_hint ?? "") !== "",
  );
}

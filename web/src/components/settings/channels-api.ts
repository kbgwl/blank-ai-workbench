import { API_V1, request } from "@/lib/http";

/**
 * 消息通道（企业微信 / 钉钉 / 飞书 / 通用 Webhook）。
 *
 * 表单完全由后端的 catalog 生成：字段名、是否密钥、必填与否、按接法过滤，
 * 全部来自适配器的声明。前端不硬编码任何一家厂商的字段——硬编码的下场是
 * 后端加一个字段、界面上永远填不了，而客户只能改配置文件。
 */

export type ChannelField = {
  key: string;
  label: string;
  secret: boolean;
  required: boolean;
  placeholder: string;
  help: string;
  default: string;
  options: { value: string; label: string }[];
  /** 非空时只在列出的接法下显示 */
  only_modes: string[];
};

export type ChannelMode = {
  value: string;
  label: string;
  hint: string;
  push: boolean;
  receive: string[];
};

export type ChannelAdapter = {
  kind: string;
  label: string;
  vendor: string;
  summary: string;
  docs: string;
  modes: ChannelMode[];
  fields: ChannelField[];
  setup: string[];
};

export type ChannelReceipt = { mode: string; url: string; note: string };

export type Channel = {
  id: string;
  kind: string;
  kind_label: string;
  name: string;
  mode: string;
  status: "ok" | "failed" | "unverified" | string;
  status_note: string;
  checked_at: string;
  push: boolean;
  receive: boolean;
  receive_mode: string;
  project_id: string;
  thread_id: string;
  /** 密钥字段是掩码（****abcd），原样提交回来等于"没改" */
  config: Record<string, string>;
  receipt: ChannelReceipt;
  callback_token: string;
  last_inbound_at: string;
  last_inbound_error: string;
  capabilities: { push: boolean; receive: string[] };
};

export type ChannelCatalog = {
  adapters: ChannelAdapter[];
  public_base: string;
  receive_notes: { mode: string; label: string; note: string }[];
};

export type ChannelList = {
  channels: Channel[];
  public_base: string;
  triggers_enabled: boolean;
  im_events: boolean;
};

export type VerifyResult = {
  check: { ok: boolean; detail: string; claimed: boolean };
  channel: Channel;
};

export type TestResult = {
  send: { channel_id: string; name: string; ok: boolean; detail: string };
  channel: Channel;
};

export type ChannelDraft = {
  kind: string;
  name: string;
  mode: string;
  config: Record<string, string>;
  push: boolean;
  receive: boolean;
  receive_mode: string;
  project_id?: string;
};

export function fetchChannelCatalog(): Promise<ChannelCatalog> {
  return request<ChannelCatalog>(`${API_V1}/channels/catalog`);
}

export function fetchChannels(): Promise<ChannelList> {
  return request<ChannelList>(`${API_V1}/channels`);
}

export function createChannel(draft: ChannelDraft): Promise<{ channel: Channel }> {
  return request<{ channel: Channel }>(`${API_V1}/channels`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(draft),
  });
}

export function patchChannel(
  id: string,
  patch: Partial<Omit<ChannelDraft, "kind">>,
): Promise<{ channel: Channel }> {
  return request<{ channel: Channel }>(`${API_V1}/channels/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}

export function deleteChannel(id: string): Promise<void> {
  return request<void>(`${API_V1}/channels/${id}`, { method: "DELETE" });
}

export function verifyChannel(id: string): Promise<VerifyResult> {
  return request<VerifyResult>(`${API_V1}/channels/${id}/verify`, { method: "POST" });
}

export function testChannel(id: string): Promise<TestResult> {
  return request<TestResult>(`${API_V1}/channels/${id}/test`, { method: "POST" });
}

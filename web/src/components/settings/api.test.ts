import assert from "node:assert/strict";
import { test } from "node:test";

import { dockerReadout, type SafetyRuntime } from "./api.ts";

type Backend = SafetyRuntime["backend"];

const backend = (over: Partial<Backend>): Backend =>
  ({
    backend: "none",
    isolation: "none",
    label: "沙箱后端不可用：命令会被拒绝",
    detail: "",
    platform: "nt",
    docker_available: false,
    ...over,
  }) as Backend;

test("装了但守护进程没应答：不许写成「可用」，也不许写成「未安装」", () => {
  const readout = dockerReadout(
    backend({
      docker_available: false,
      docker_state: "daemon_unreachable",
      docker_detail: "failed to connect to the docker API at npipe:...",
    }),
  );
  assert.equal(readout.state, "daemon_unreachable");
  assert.ok(readout.text.includes("守护进程"), readout.text);
  assert.ok(!readout.text.includes("未安装"), readout.text);
  assert.equal(readout.warn, true);
});

test("真的能用才是不警告的那一档", () => {
  const readout = dockerReadout(
    backend({ docker_available: true, docker_state: "ready", docker_label: "容器后端可用（docker 守护进程有应答）" }),
  );
  assert.equal(readout.warn, false);
  assert.equal(readout.text, "Docker 容器后端可用（docker 守护进程有应答）");
});

test("后端给了文案就用后端的：同一句话不许在两处各写一遍", () => {
  const readout = dockerReadout(backend({ docker_state: "timeout", docker_label: "已安装，但探测超时" }));
  assert.equal(readout.text, "Docker 已安装，但探测超时");
});

test("旧后端没带 docker_state：退回布尔，但不许把 false 读成「可用」", () => {
  assert.equal(dockerReadout(backend({ docker_available: true })).state, "ready");
  const fallback = dockerReadout(backend({ docker_available: false }));
  assert.equal(fallback.state, "not_installed");
  assert.equal(fallback.warn, true);
});

test("缓存读数要报得出「几秒前探的」，没探过就不编一个时间", () => {
  assert.equal(
    dockerReadout(backend({ docker_state: "ready", docker_label: "容器后端可用", docker_age_seconds: 12.3 })).text,
    "Docker 容器后端可用｜12.3s 前探测",
  );
  const never = dockerReadout(backend({ docker_state: "pending", docker_age_seconds: null }));
  assert.ok(!never.text.includes("前探测"), never.text);
});

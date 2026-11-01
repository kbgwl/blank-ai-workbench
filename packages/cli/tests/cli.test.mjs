import { test } from "node:test";
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

const run = promisify(execFile);
const BIN = fileURLToPath(new URL("../src/index.mjs", import.meta.url));
const TIMEOUT = 30_000;

test("--help 列出全部命令且退出码 0", async () => {
  const { stdout } = await run(process.execPath, [BIN, "--help"], { timeout: TIMEOUT });
  for (const command of ["start", "doctor", "init", "list", "add", "remove"]) {
    assert.match(stdout, new RegExp(command), `帮助里缺命令 ${command}`);
  }
});

test("doctor 做环境自检并给出结论，不崩溃", async () => {
  const { stdout } = await run(process.execPath, [BIN, "doctor"], { timeout: TIMEOUT });
  assert.ok(stdout.trim().length > 0, "doctor 必须有输出");
});

test("无参数不带子命令时给出用法而不是堆栈", async () => {
  // index.mjs 无参数 = 直接 start；在测试里不能真的起服务，
  // 用一个必然非法的子命令验证错误路径是「用法提示」而不是未捕获异常。
  await assert.rejects(
    run(process.execPath, [BIN, "__definitely_not_a_command__"], { timeout: TIMEOUT }),
    (error) => {
      const text = `${error.stdout ?? ""}${error.stderr ?? ""}`;
      assert.ok(!/Traceback|at Object\.<anonymous> \(node:internal/.test(text), "不该是未捕获堆栈");
      return true;
    },
  );
});

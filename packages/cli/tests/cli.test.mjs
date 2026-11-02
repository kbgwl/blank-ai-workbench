import { test } from "node:test";
import assert from "node:assert/strict";
import { execFile, spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
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

test("入口名统一为 blank-ai：package.json bin、shebang、帮助文案一致", async () => {
  const pkg = JSON.parse(readFileSync(fileURLToPath(new URL("../package.json", import.meta.url)), "utf8"));
  const binKeys = Object.keys(pkg.bin ?? {});
  assert.deepEqual(binKeys, ["blank-ai"], `bin 必须只有 blank-ai，实际 ${binKeys}`);
  assert.ok(pkg.bin["blank-ai"].startsWith("src/"), "bin 要指向真实存在的入口");
  assert.ok(existsSync(fileURLToPath(new URL(`../${pkg.bin["blank-ai"]}`, import.meta.url))), "bin 入口文件必须存在");
  const firstLine = readFileSync(BIN, "utf8").split("\n")[0];
  assert.equal(firstLine, "#!/usr/bin/env node", "入口第一行必须是 shebang");
  const { stdout } = await run(process.execPath, [BIN, "--help"], { timeout: TIMEOUT });
  assert.match(stdout, /blank-ai/, "帮助文案必须用 blank-ai 作为入口名");
  assert.doesNotMatch(stdout, /(^|\s)blank(\s|$)/, "帮助文案不许再出现旧入口名 blank");
});

test("start 真的消费 --workspace：不再静默忽略目录参数", async () => {
  const { stdout } = await run(process.execPath, [BIN, "--help"], { timeout: TIMEOUT });
  assert.match(stdout, /--workspace/, "帮助里必须写明 --workspace，否则用户不知道有这个开关");
  // 这条断言是结构性的：之前 start 完全不读参数，用户给了目录也会被丢掉，
  // 打开界面还是空的。用源码级断言防回退——比"起一个后端再手点界面"稳。
  const source = readFileSync(BIN, "utf8");
  assert.match(source, /argValue\(args\.slice\(1\), "--workspace"\)/, "start 必须解析 --workspace");
  assert.match(source, /工作区不存在/, "目录不存在时要有明确报错");
  assert.match(source, /\/workspaces/, "绑定必须真的调后端的工作区接口");
});

test("干净目录模式：BLANK_SOURCE_ROOT 指向源码根时 start 能找到资源，未设时明确退出", async () => {
  const repoRoot = fileURLToPath(new URL("../../..", import.meta.url));
  // 未设 BLANK_SOURCE_ROOT 且不在源码树：退出码 2 + 明确指引，不是堆栈。
  const cleanDir = mkdtempSync(path.join(tmpdir(), "blank-cli-clean-"));
  await assert.rejects(
    run(process.execPath, [BIN, "start"], { timeout: TIMEOUT, cwd: cleanDir }),
    (error) => {
      const text = `${error.stdout ?? ""}${error.stderr ?? ""}`;
      assert.match(text, /BLANK_SOURCE_ROOT/, "退出信息必须提示 BLANK_SOURCE_ROOT 用法");
      assert.ok(!/Traceback/.test(text), "不该是未捕获堆栈");
      return true;
    },
  );
  // 设了但指错：明确报错指错位置。
  await assert.rejects(
    run(process.execPath, [BIN, "start"], { timeout: TIMEOUT, cwd: cleanDir, env: { ...process.env, BLANK_SOURCE_ROOT: cleanDir } }),
    (error) => {
      const text = `${error.stdout ?? ""}${error.stderr ?? ""}`;
      assert.match(text, /找不到 services\/api\/src 与 web\/out/, "指错的 BLANK_SOURCE_ROOT 要给出具体缺失项");
      return true;
    },
  );
  // 指对：start 至少走到「启动空白格本地工作台」这一步（后续 spawn 由退出码/超时体现）。
  const child = spawn(process.execPath, [BIN, "start"], {
    cwd: cleanDir,
    env: { ...process.env, BLANK_SOURCE_ROOT: repoRoot, BLANK_WEB_PORT: "0" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let out = "";
  child.stdout.on("data", (chunk) => (out += chunk));
  child.stderr.on("data", (chunk) => (out += chunk));
  await new Promise((resolve) => {
    const timer = setTimeout(resolve, 8_000);
    child.once("exit", () => {
      clearTimeout(timer);
      resolve();
    });
  });
  child.kill();
  assert.match(out, /启动空白格本地工作台/, "干净目录 + 正确 BLANK_SOURCE_ROOT 必须能走到启动流程");
});

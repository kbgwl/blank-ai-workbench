import { readFileSync } from "node:fs";
import path from "node:path";

const recordPath = path.resolve(process.cwd(), "docs/validation/acceptance-2026-11-21.md");
const required = "结论：通过：打开有东西，动作有变化，刷新还在";

try {
  const content = readFileSync(recordPath, "utf8");
  if (!content.includes(required)) {
    console.error(`[acceptance-record] missing conclusion: ${path.relative(process.cwd(), recordPath)}`);
    process.exit(1);
  }
  console.log(`[acceptance-record] ok: ${path.relative(process.cwd(), recordPath)}`);
} catch {
  console.error(`[acceptance-record] missing record: ${path.relative(process.cwd(), recordPath)}`);
  process.exit(1);
}

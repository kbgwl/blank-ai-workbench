// node --test 用的是原生 ESM 解析：不认 "./api" 这种省略扩展名的写法，也不认 tsconfig
// 里的 "@/" 别名。Next 构建时这些都没问题，但单元测就会在加载阶段直接
// ERR_MODULE_NOT_FOUND，于是"有测试文件"却从没跑过。这个钩子只补解析，不做转译。
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";

const SOURCES = ["", ".ts", ".tsx", "/index.ts", "/index.tsx"];

export async function resolve(specifier, context, next) {
  if (!specifier.startsWith("@/") && !/^\.{1,2}\//.test(specifier)) return next(specifier, context);
  const target = specifier.startsWith("@/")
    ? new URL(`src/${specifier.slice(2)}`, import.meta.url)
    : new URL(specifier, context.parentURL ?? import.meta.url);
  for (const suffix of SOURCES) {
    const url = suffix.startsWith("/") ? new URL(`.${suffix}`, target) : new URL(target.href + suffix);
    if (existsSync(fileURLToPath(url))) return next(url.href, context);
  }
  return next(specifier, context);
}

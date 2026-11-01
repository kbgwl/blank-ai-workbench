import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // Override default ignores of eslint-config-next.
  // 必须覆盖「带后缀」的构建缓存目录（如 .next-m80r、.next.prev-*、out.prev-*），
  // 否则 `eslint .` 会把成百上千个打包后的巨型 .js 拉进来逐行 lint，
  // 表现为卡死数分钟，在内存吃紧的机器上直接以 0xC0000409 崩溃。
  // （这些目录里有桌面打包流程的回滚备份，不能删，只能靠 ignore 排除。）
  globalIgnores([
    ".next/**",
    ".next*/**",
    "out/**",
    "out*/**",
    ".team-e2e-next/**",
    "build/**",
    "node_modules/**",
    "next-env.d.ts",
  ]),
]);

export default eslintConfig;

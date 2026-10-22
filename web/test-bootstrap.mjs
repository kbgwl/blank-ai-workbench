// 在测试文件被加载之前挂上解析钩子，见 test-resolver.mjs 里的说明。
import { register } from "node:module";

register(new URL("./test-resolver.mjs", import.meta.url));

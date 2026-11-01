#!/usr/bin/env node
/**
 * 生成 v157 §4.1 coding benchmark 任务集：
 *   evals/benchmarks/coding/tasks/*.json   55 个任务定义
 *   evals/benchmarks/coding/fixtures/<id>/ 每任务一个可判分工作区
 *
 * 设计原则：每个任务的 testCommand 必须真实可执行、结果可机判——
 * 不存在「判分靠人眼」的任务。fixture 全部是 Python/Node 小文件，
 * 判分命令只依赖 services/api/.venv 的 python 或系统 node。
 *
 * 用法：node evals/benchmarks/coding/gen-tasks.mjs [--force]
 */
import { mkdirSync, writeFileSync, existsSync } from "node:fs";
import path from "node:path";

const ROOT = path.resolve(import.meta.dirname);
const TASKS_DIR = path.join(ROOT, "tasks");
const FIXTURES_DIR = path.join(ROOT, "fixtures");
const FORCE = process.argv.includes("--force");

const tasks = [];
let seq = 0;
const id = (kind) => `coding-${String(++seq).padStart(3, "0")}`;

function add(task, files) {
  const dir = path.join(FIXTURES_DIR, task.id);
  if (existsSync(dir) && !FORCE) {
    // fixtures 一旦有人手工调过就不许静默覆盖
    console.error(`fixture 已存在（跳过，加 --force 重建）：${dir}`);
  } else {
    mkdirSync(dir, { recursive: true });
    for (const [name, raw] of Object.entries(files)) {
      const content = name.startsWith("test_") && name.endsWith(".py") ? wrapPytest(raw) : raw;
      const p = path.join(dir, name);
      mkdirSync(path.dirname(p), { recursive: true });
      writeFileSync(p, content, "utf8");
    }
  }
  tasks.push(task);
}

const PY = process.env.BLANK_BENCH_PYTHON || "python";

/**
 * 把脚本式断言统一包成 pytest 可收集的函数。
 * 后端 run_validation 在工作区自动跑 pytest：收集不到测试（exit 5）会被判
 * 「测试未通过」而整个任务 failed——所以 test_*.py 必须是真测试。
 */
function wrapPytest(body) {
  if (body.includes("def test_")) return body;
  return (
    "def test_bench():\n" +
    body
      .split("\n")
      .map((line) => (line.trim() ? "    " + line : line))
      .join("\n") +
    "\n"
  );
}

/* ── 1. 单文件 Bug 修复 ×10 ─────────────────────────────── */
const singleBugs = [
  {
    file: "salary.py",
    broken: "def weekly_pay(rate, hours):\n    return rate * hours\n",
    fixed: "def weekly_pay(rate, hours):\n    if hours > 40:\n        return rate * 40 + rate * 1.5 * (hours - 40)\n    return rate * hours\n",
    test: "from salary import weekly_pay\nassert weekly_pay(10, 40) == 400\nassert abs(weekly_pay(10, 45) - 475) < 1e-9\nassert weekly_pay(10, 30) == 300\nprint('ok')\n",
    goal: "给 weekly_pay 加加班费：每周超过 40 小时的部分按 1.5 倍时薪计算",
  },
  {
    file: "avg.py",
    broken: "def average(nums):\n    return sum(nums) / len(nums)\n",
    fixed: "def average(nums):\n    if not nums:\n        raise ValueError('empty')\n    return sum(nums) / len(nums)\n",
    test: "from avg import average\nassert average([1, 2, 3]) == 2\ntry:\n    average([])\n    raise SystemExit(1)\nexcept ValueError:\n    pass\nprint('ok')\n",
    goal: "average 对空列表要抛 ValueError，不能除零崩溃",
  },
  {
    file: "clamp.py",
    broken: "def clamp(value, lo, hi):\n    return max(lo, value)\n",
    fixed: "def clamp(value, lo, hi):\n    return min(hi, max(lo, value))\n",
    test: "from clamp import clamp\nassert clamp(5, 0, 10) == 5\nassert clamp(-3, 0, 10) == 0\nassert clamp(99, 0, 10) == 10\nprint('ok')\n",
    goal: "clamp 只钳了下界，把上界也钳上",
  },
  {
    file: "dedupe.py",
    broken: "def dedupe(items):\n    return list(set(items))\n",
    fixed: "def dedupe(items):\n    seen = []\n    for item in items:\n        if item not in seen:\n            seen.append(item)\n    return seen\n",
    test: "from dedupe import dedupe\nassert dedupe([1, 2, 1, 3]) == [1, 2, 3]\nassert dedupe(['b', 'a', 'b']) == ['b', 'a']\nprint('ok')\n",
    goal: "dedupe 去重后必须保持原顺序（set 会打乱）",
  },
  {
    file: "grade.py",
    broken: "def grade(score):\n    if score >= 60:\n        return 'D'\n    elif score >= 70:\n        return 'C'\n    elif score >= 80:\n        return 'B'\n    return 'A'\n",
    fixed: "def grade(score):\n    if score >= 90:\n        return 'A'\n    elif score >= 80:\n        return 'B'\n    elif score >= 70:\n        return 'C'\n    return 'D'\n",
    test: "from grade import grade\nassert grade(95) == 'A'\nassert grade(85) == 'B'\nassert grade(75) == 'C'\nassert grade(60) == 'D'\nprint('ok')\n",
    goal: "grade 的分支条件写反了，90 分以上是 A、60 分以下是 D",
  },
  {
    file: "count_words.py",
    broken: "def count_words(text):\n    return len(text)\n",
    fixed: "def count_words(text):\n    return len([w for w in text.split() if w])\n",
    test: "from count_words import count_words\nassert count_words('hello world') == 2\nassert count_words('  a  b ')== 2\nassert count_words('') == 0\nprint('ok')\n",
    goal: "count_words 数的是字符不是单词，改成数单词",
  },
  {
    file: "safe_get.py",
    broken: "def safe_get(d, key):\n    return d[key]\n",
    fixed: "def safe_get(d, key, default=None):\n    return d.get(key, default)\n",
    test: "from safe_get import safe_get\nassert safe_get({'a': 1}, 'a') == 1\nassert safe_get({}, 'a') is None\nassert safe_get({}, 'a', 5) == 5\nprint('ok')\n",
    goal: "safe_get 不能对缺键抛 KeyError，支持 default 参数",
  },
  {
    file: "fib.py",
    broken: "def fib(n):\n    if n <= 1:\n        return 1\n    return fib(n - 1) + fib(n - 2)\n",
    fixed: "def fib(n):\n    if n <= 1:\n        return n\n    a, b = 0, 1\n    for _ in range(n - 1):\n        a, b = b, a + b\n    return b\n",
    test: "from fib import fib\nassert fib(0) == 0\nassert fib(1) == 1\nassert fib(10) == 55\nassert fib(30) == 832040\nprint('ok')\n",
    goal: "fib(0) 应为 0 且大 n 不得指数级慢（改成迭代）",
  },
  {
    file: "parse_csv.py",
    broken: "def parse_csv(line):\n    return line.split(',')\n",
    fixed: "import csv, io\n\ndef parse_csv(line):\n    return next(csv.reader(io.StringIO(line)))\n",
    test: "from parse_csv import parse_csv\nassert parse_csv('a,b,c') == ['a', 'b', 'c']\nassert parse_csv('a,\"b,c\"') == ['a', 'b,c']\nprint('ok')\n",
    goal: "parse_csv 要支持带引号的字段（逗号在引号内不算分隔符）",
  },
  {
    file: "retry.py",
    broken: "def retry(fn, times):\n    for _ in range(times):\n        return fn()\n    raise RuntimeError('exhausted')\n",
    fixed: "def retry(fn, times):\n    last = None\n    for _ in range(times):\n        try:\n            return fn()\n        except Exception as error:\n            last = error\n    raise RuntimeError('exhausted') from last\n",
    test: "from retry import retry\nattempts = []\ndef flaky():\n    attempts.append(1)\n    if len(attempts) < 3:\n        raise ValueError('boom')\n    return 'done'\nassert retry(flaky, 5) == 'done'\ntry:\n    retry(lambda: 1 / 0, 2)\n    raise SystemExit(1)\nexcept RuntimeError:\n    pass\nprint('ok')\n",
    goal: "retry 只试了一次就 return，改成失败重试到上限、全败抛 RuntimeError",
  },
];
for (const bug of singleBugs) {
  add(
    {
      id: id("single-bug"),
      category: "single_file_bugfix",
      workspace: "fixture",
      goal: bug.goal,
      allowedTools: ["read_file", "apply_patch", "run_shell", "run_tests"],
      expectedFiles: [bug.file],
      testCommand: `${PY} -X utf8 -c "import sys; sys.path.insert(0, '.'); exec(open('test_check.py', encoding='utf-8').read())"`,
      acceptance: ["测试命令退出码为 0", "只修改目标文件"],
      expectedStatus: "succeeded",
    },
    {
      [bug.file]: bug.broken,
      "test_check.py": bug.test,
      "pytest.ini": "[pytest]\n",
    },
  );
}

/* ── 2. 多文件重构 ×10 ─────────────────────────────────── */
const multiRefactors = [
  {
    goal: "把散在 utils.py 里的 format_price 移到新文件 pricing.py，utils.py 改为 re-export，main.py 不需要改也能跑",
    files: {
      "utils.py": "def format_price(cents):\n    return f'{cents / 100:.2f} 元'\n",
      "test_refactor.py": "from utils import format_price\n\n\ndef test_format_price():\n    assert format_price(1250) == '12.50 元'\n",
    },
    expected: ["pricing.py", "utils.py"],
  },
  {
    goal: "models.py 里两个模型类的 save/load 重复实现，抽成 storage.py 里的公共函数，两个类都改为调用它",
    files: {
      "models.py": "class User:\n    def save(self):\n        with open(f'{self.name}.json', 'w', encoding='utf-8') as f:\n            f.write(self.data)\n\nclass Order:\n    def save(self):\n        with open(f'{self.oid}.json', 'w', encoding='utf-8') as f:\n            f.write(self.data)\n",
      "test_refactor.py": "import models\nu = models.User(); u.name = 'alice'; u.data = '{}'\nu.save()\nimport os\nassert os.path.exists('alice.json')\nprint('ok')\n",
    },
    expected: ["storage.py", "models.py"],
  },
  {
    goal: "config.py 的硬编码常量移到 constants.py，并让 config.py from constants import *，行为不变",
    files: {
      "config.py": "MAX_RETRIES = 3\nTIMEOUT = 30\n",
      "test_refactor.py": "def test_constants_visible():\n    import config\n\n    assert config.MAX_RETRIES == 3\n    assert config.TIMEOUT == 30\n",
    },
    // goal 明确要求改 config.py（移走常量后还要 from constants import *），
    // expectedFiles 必须把它算进去——漏掉会把 goal 要求的改动判成误修改。
    expected: ["constants.py", "config.py"],
  },
  {
    goal: "shapes.py 里 Circle 和 Square 都有自己的 area，新建 base.py 定义 area 接口协议，两个类显式继承",
    files: {
      "shapes.py": "class Circle:\n    def __init__(self, r):\n        self.r = r\n    def area(self):\n        return 3.14159 * self.r ** 2\n\nclass Square:\n    def __init__(self, s):\n        self.s = s\n    def area(self):\n        return self.s ** 2\n",
      "test_shapes.py": "from shapes import Circle, Square\nimport base\nassert issubclass(Circle, base.Shape)\nassert issubclass(Square, base.Shape)\nassert abs(Circle(1).area() - 3.14159) < 1e-5\nassert Square(2).area() == 4\nprint('ok')\n",
    },
    expected: ["base.py", "shapes.py"],
  },
  {
    goal: "report.py 生成文本报表的逻辑硬编码在函数里，拆成 templates.py 存模板、report.py 引用，输出不变",
    files: {
      "report.py": "def render(name, total):\n    return f'用户 {name} 合计 {total} 元'\n",
      "test_report.py": "from report import render\nassert render('alice', 30) == '用户 alice 合计 30 元'\nprint('ok')\n",
    },
    expected: ["templates.py", "report.py"],
  },
  {
    goal: "db.py 直接在函数里拼 SQL 字符串，抽到 queries.py 每条一个常量，db.py 引用，行为不变",
    files: {
      "db.py": "def user_sql():\n    return 'SELECT * FROM users'\n\ndef order_sql():\n    return 'SELECT * FROM orders'\n",
      "test_db.py": "import db\nassert db.user_sql() == 'SELECT * FROM users'\nassert db.order_sql() == 'SELECT * FROM orders'\nprint('ok')\n",
    },
    expected: ["queries.py", "db.py"],
  },
  {
    goal: "auth.py 的 hash 与 verify 两函数用了不同算法（md5 vs sha256），统一抽到 hashing.py 用 sha256，两处调用方不变",
    files: {
      "auth.py": "import hashlib\n\ndef make_token(pw):\n    return hashlib.md5(pw.encode()).hexdigest()\n\ndef check_token(pw, token):\n    return hashlib.sha256(pw.encode()).hexdigest() == token\n",
      "test_auth.py": "import auth\nassert len(auth.make_token('x')) == 64\nassert auth.check_token('x', auth.make_token('x'))\nprint('ok')\n",
    },
    expected: ["hashing.py", "auth.py"],
  },
  {
    goal: "logging_setup.py 与 api.py 各自配置了重复的 logger，抽成 log_factory.py 统一创建，两个模块改用",
    files: {
      "logging_setup.py": "import logging\n\ndef setup():\n    logging.basicConfig(level=logging.INFO)\n    return logging.getLogger('app')\n",
      "api.py": "import logging\n\nlogger = logging.getLogger('api')\n",
      "test_refactor.py": "def test_logger_available():\n    import importlib\n\n    import api\n\n    assert api.logger is not None\n    importlib.import_module(\"log_factory\")\n",
    },
    expected: ["log_factory.py", "api.py", "logging_setup.py"],
  },
  {
    goal: "main.py 一个函数干了校验+转换+输出三件事，拆成 validators.py / converters.py，main.py 只保留编排，输出不变",
    files: {
      "main.py": "def process(raw):\n    if not raw.isdigit():\n        raise ValueError('not a number')\n    value = int(raw) * 2\n    return f'value={value}'\n",
      "test_main.py": "from main import process\nassert process('21') == 'value=42'\ntry:\n    process('abc')\n    raise SystemExit(1)\nexcept ValueError:\n    pass\nprint('ok')\n",
    },
    expected: ["validators.py", "converters.py", "main.py"],
  },
  {
    goal: "cache.py 的 TTL 逻辑写死在 get 里，抽成 ttl.py 提供 is_expired 函数供 cache 调用，行为不变",
    files: {
      "cache.py": "import time\n\nSTORE = {}\n\ndef put(k, v, ttl):\n    STORE[k] = (v, time.time() + ttl)\n\ndef get(k):\n    v, exp = STORE.get(k, (None, 0))\n    if time.time() > exp:\n        return None\n    return v\n",
      "test_cache.py": "from cache import put, get\nput('a', 1, 10)\nassert get('a') == 1\nput('b', 2, -1)\nassert get('b') is None\nprint('ok')\n",
    },
    expected: ["ttl.py", "cache.py"],
  },
];
for (const task of multiRefactors) {
  const testFile = Object.keys(task.files).find((f) => f.startsWith("test_"));
  add(
    {
      id: id("multi-refactor"),
      category: "multi_file_refactor",
      workspace: "fixture",
      goal: task.goal,
      allowedTools: ["read_file", "apply_patch", "run_shell", "run_tests"],
      expectedFiles: task.expected,
      testCommand: testFile
        ? `${PY} -X utf8 ${testFile}`
        : `${PY} -X utf8 main.py`,
      acceptance: ["测试/主程序退出码为 0", `新建或修改了 ${task.expected.join("、")}`],
      expectedStatus: "succeeded",
    },
    { "pytest.ini": "[pytest]\n", ...task.files },
  );
}

/* ── 3. 前后端联动 ×5（用 Node 模拟 API 契约）────────────── */
const apiContract = [
  { field: "status", values: ["ok", "error"] },
  { field: "code", values: [0, 1] },
];
for (let i = 0; i < 5; i++) {
  const { field } = apiContract[i % apiContract.length];
  add(
    {
      id: id("api-contract"),
      category: "frontend_backend_contract",
      workspace: "fixture",
      goal: `后端 api.py 的 handle() 返回缺字段 ${field}，前端 client.py 的 render() 依赖它。修后端让它返回 ${field}，保证 client 的断言通过；不要改 client.py`,
      allowedTools: ["read_file", "apply_patch", "run_shell", "run_tests"],
      expectedFiles: ["api.py"],
      testCommand: `${PY} -X utf8 test_contract.py`,
      acceptance: ["契约测试退出码为 0", "client.py 未被修改"],
      expectedStatus: "succeeded",
    },
    {
      "api.py": `def handle(payload):\n    # 后端漏了 ${field} 字段，前端已经依赖它\n    return {"data": payload.get("q", "")}\n`,
      "client.py": `def render(resp):\n    assert resp["${field}"] in ("ok", "error", 0, 1), f'missing ${field}'\n    return resp["data"]\n`,
      "test_contract.py": `import api, client\nresp = api.handle({"q": "hi"})\nassert client.render(resp) == "hi"\nprint("ok")\n`,
      "pytest.ini": "[pytest]\n",
    },
  );
}

/* ── 4. 测试失败修复 ×5 ─────────────────────────────────── */
const testFixes = [
  { file: "money.py", code: "def to_cents(yuan):\n    return int(yuan)\n", goal: "to_cents(1.05) 应为 105 分而不是 1 分（浮点截断 bug），修 money.py 让挂掉的测试转绿" },
  { file: "slugify.py", code: "def slugify(text):\n    return text.lower()\n", goal: "slugify 没把空格换连字符，修 slugify.py 让测试通过" },
  { file: "merge.py", code: "def merge(a, b):\n    return {**a}\n", goal: "merge 丢了第二个字典，修 merge.py 让测试通过" },
  { file: "pad.py", code: "def pad(text, width):\n    return text\n", goal: "pad 没有补零，修 pad.py 让测试通过" },
  { file: "leap.py", code: "def is_leap(year):\n    return year % 4 == 0\n", goal: "is_leap 逢百年判断错误（2100 不是闰年），修 leap.py 让测试通过" },
];
const testFixBodies = {
  "money.py": "from money import to_cents\nassert to_cents(1.05) == 105\nassert to_cents(2) == 200\nprint('ok')\n",
  "slugify.py": "from slugify import slugify\nassert slugify('Hello World') == 'hello-world'\nassert slugify('A  B') == 'a-b'\nprint('ok')\n",
  "merge.py": "from merge import merge\nassert merge({'a': 1}, {'b': 2}) == {'a': 1, 'b': 2}\nassert merge({'a': 1}, {'a': 2}) == {'a': 2}\nprint('ok')\n",
  "pad.py": "from pad import pad\nassert pad('7', 3) == '007'\nassert pad('123', 3) == '123'\nprint('ok')\n",
  "leap.py": "from leap import is_leap\nassert is_leap(2024)\nassert is_leap(2000)\nassert not is_leap(2100)\nassert not is_leap(2023)\nprint('ok')\n",
};
for (const fix of testFixes) {
  const testFile = `test_${fix.file}`;
  add(
    {
      id: id("test-fix"),
      category: "test_failure_fix",
      workspace: "fixture",
      goal: fix.goal,
      allowedTools: ["read_file", "apply_patch", "run_shell", "run_tests"],
      expectedFiles: [fix.file],
      testCommand: `${PY} -X utf8 ${testFile}`,
      acceptance: ["测试命令退出码为 0", "不许改测试文件"],
      expectedStatus: "succeeded",
    },
    {
      [fix.file]: fix.code,
      [testFile]: testFixBodies[fix.file],
      "pytest.ini": "[pytest]\n",
    },
  );
}

/* ── 5. 类型错误修复 ×5（用 Python 注解 + 运行时检查代替重 tsc）── */
const typeFixes = [
  { file: "units.py", goal: "convert 的注解说返回 int 实际返回 str，修正实现（把结果转成 int），注解不动", broken: "def convert(text: str) -> int:\n    return text.strip()\n", test: "import units\nassert units.convert(' 42 ') == 42\nfrom typing import get_type_hints\nprint('ok')\n" },
  { file: "points.py", goal: "distance 对 None 参数崩了，按注解要求参数可空并返回 0.0", broken: "def distance(a: tuple, b: tuple | None = None) -> float:\n    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5\n", test: "from points import distance\nassert distance((0, 0), (3, 4)) == 5\nassert distance((1, 1), None) == 0.0\nprint('ok')\n" },
  { file: "bundles.py", goal: "combine 注解声明返回 list，实际返回 tuple，改实现成 list", broken: "def combine(items: list) -> list:\n    return tuple(items)\n", test: "from bundles import combine\nassert combine([1, 2]) == [1, 2]\nassert isinstance(combine([1]), list)\nprint('ok')\n" },
  { file: "rates.py", goal: "apply_tax 的 rate 注解是 float，传 int 会崩（做了字符串拼接），修实现让 int/float 都能用且返回 float", broken: "def apply_tax(amount: float, rate: float) -> float:\n    return amount + amount * rate if isinstance(rate, float) else amount + rate\n", test: "from rates import apply_tax\nassert apply_tax(100, 0.1) == 110.0\nassert apply_tax(100, 1) == 110.0\nassert isinstance(apply_tax(100, 1), float)\nprint('ok')\n" },
  { file: "profiles.py", goal: "rename 返回 dict 但把 key 写成了非 str（注解要求 dict[str, str]），修实现", broken: "def rename(mapping: dict) -> dict:\n    return {k.upper(): v for k, v in mapping.items()}\n", test: "from profiles import rename\nout = rename({'a': 'x'})\nassert out == {'A': 'x'}\nassert all(isinstance(k, str) for k in out)\nprint('ok')\n" },
];
for (const fix of typeFixes) {
  add(
    {
      id: id("type-fix"),
      category: "type_error_fix",
      workspace: "fixture",
      goal: fix.goal,
      allowedTools: ["read_file", "apply_patch", "run_shell", "run_tests"],
      expectedFiles: [fix.file],
      testCommand: `${PY} -X utf8 test_${fix.file}`,
      acceptance: ["测试命令退出码为 0", "函数注解保持不变"],
      expectedStatus: "succeeded",
    },
    {
      [fix.file]: fix.broken,
      [`test_${fix.file}`]: fix.test,
      "pytest.ini": "[pytest]\n",
    },
  );
}

/* ── 6. 数据库迁移 ×5（sqlite3 标准库可判分）────────────── */
const migrations = [
  { table: "users", addCol: "email", ddl: "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)", seed: "INSERT INTO users (name) VALUES ('alice')", goal: "给 users 表加 email TEXT 列（可为空），保留既有数据" },
  { table: "orders", addCol: "total_cents", ddl: "CREATE TABLE orders (id INTEGER PRIMARY KEY, item TEXT)", seed: "INSERT INTO orders (item) VALUES ('book')", goal: "给 orders 表加 total_cents INTEGER 列默认 0，保留既有数据" },
  { table: "tags", addCol: "slug", ddl: "CREATE TABLE tags (id INTEGER PRIMARY KEY, label TEXT)", seed: "INSERT INTO tags (label) VALUES ('ops')", goal: "给 tags 表加 slug TEXT UNIQUE 列，既有行 slug 用 label 小写回填" },
  { table: "logs", addCol: "created_at", ddl: "CREATE TABLE logs (id INTEGER PRIMARY KEY, msg TEXT)", seed: "INSERT INTO logs (msg) VALUES ('boot')", goal: "给 logs 表加 created_at TEXT 列，既有行回填 '1970-01-01T00:00:00Z'" },
  { table: "plans", addCol: "price", ddl: "CREATE TABLE plans (id INTEGER PRIMARY KEY, name TEXT)", seed: "INSERT INTO plans (name) VALUES ('pro')", goal: "给 plans 表加 price REAL 列默认 9.9，保留既有数据" },
];
for (const mig of migrations) {
  add(
    {
      id: id("migration"),
      category: "db_migration",
      workspace: "fixture",
      goal: `${mig.goal}。直接写迁移脚本 migrate.py（用 sqlite3 标准库改 data.db），不许重建表丢数据`,
      allowedTools: ["read_file", "apply_patch", "run_shell", "run_tests"],
      expectedFiles: ["migrate.py", "data.db"],
      testCommand: `${PY} -m pytest -q test_migration.py`,
      acceptance: ["迁移脚本幂等可重复执行", "verify_migration.py 退出码为 0"],
      expectedStatus: "succeeded",
    },
    {
      "migrate.py": "# TODO: 在这里实现迁移\n",
      "init_db.py": `import sqlite3\nconn = sqlite3.connect('data.db')\nconn.execute("${mig.ddl}")\nconn.execute("${mig.seed}")\nconn.commit()\nconn.close()\nprint('seeded')\n`,
      "verify_migration.py": `import sqlite3\nconn = sqlite3.connect('data.db')\ncols = [r[1] for r in conn.execute("PRAGMA table_info(${mig.table})")]\nassert "${mig.addCol}" in cols, f"缺列 ${mig.addCol}: {cols}"\nassert conn.execute("SELECT COUNT(*) FROM ${mig.table}").fetchone()[0] >= 1, "数据丢了"\nprint("ok")\n`,
      "test_migration.py": `import sqlite3\nconn = sqlite3.connect('data.db')\ncols = [r[1] for r in conn.execute("PRAGMA table_info(${mig.table})")]\nassert "${mig.addCol}" in cols, f"缺列 ${mig.addCol}: {cols}"\nassert conn.execute("SELECT COUNT(*) FROM ${mig.table}").fetchone()[0] >= 1, "数据丢了"\nprint("ok")\n`,
      "pytest.ini": "[pytest]\n",
    },
  );
}

/* ── 7. 文档任务 ×5 ────────────────────────────────────── */
const docTasks = [
  { file: "calculator.py", func: "def add(a, b):\n    return a + b\n", doc: "计算两数之和", extra: "参数 a、b 都支持 int/float" },
  { file: "timer.py", func: "def elapsed(start, end):\n    return end - start\n", doc: "返回两个时间戳之间的秒数", extra: "end 必须大于等于 start" },
  { file: "csvio.py", func: "def write_rows(path, rows):\n    import csv\n    with open(path, 'w', newline='', encoding='utf-8') as f:\n        csv.writer(f).writerows(rows)\n", doc: "把二维列表写成 CSV 文件", extra: "使用 utf-8 编码" },
  { file: "compress.py", func: "def zlib_size(text):\n    import zlib\n    return len(zlib.compress(text.encode('utf-8')))\n", doc: "返回文本 zlib 压缩后的字节数", extra: "编码固定 utf-8" },
  { file: "quota.py", func: "def within(limit, used):\n    return used <= limit\n", doc: "判断用量是否还在额度内", extra: "恰好等于额度也算在额度内" },
];
for (const doc of docTasks) {
  add(
    {
      id: id("docs"),
      category: "documentation",
      workspace: "fixture",
      goal: `给 ${doc.file} 里的函数补 Google 风格 docstring：说明（${doc.doc}）、Args、Returns、注意（${doc.extra}）。不改函数逻辑`,
      allowedTools: ["read_file", "apply_patch"],
      expectedFiles: [doc.file],
      testCommand: `${PY} -m pytest -q test_docstring.py`,
      acceptance: ["docstring 存在且含 Args/Returns 小节", "函数行为不变（原有断言仍过）"],
      expectedStatus: "succeeded",
    },
    {
      [doc.file]: doc.func,
      "test_docstring.py": `import ast\n\nSRC = open("${doc.file}", encoding="utf-8").read()\ntree = ast.parse(SRC)\nfunc = tree.body[0]\n\ndef test_docstring_present():\n    assert isinstance(func.body[0], ast.Expr), "第一句必须是 docstring"\n    doc = ast.get_docstring(func)\n    assert doc and "Args" in doc and "Returns" in doc, f"docstring 缺 Args/Returns: {doc}"\n`,
      "pytest.ini": "[pytest]\n",
    },
  );
}

/* ── 8. 逆向分析 ×5（只读 + 结构化回答）──────────────────── */
const reverseTasks = [
  {
    files: {
      "pipeline.py": "def run(items):\n    out = []\n    for item in items:\n        if item % 2 == 0:\n            out.append(item * 3)\n    return out\n",
    },
    question: "run([1,2,3,4,5,6]) 返回什么？过滤条件和变换规则分别是什么？",
    answerKeys: ["[6, 12, 18]", "偶数", "乘 3"],
  },
  {
    files: {
      "throttle.py": "import time\n_LAST = {}\n\ndef allow(key, limit_per_sec):\n    now = time.time()\n    last = _LAST.get(key, 0)\n    if now - last >= 1 / limit_per_sec:\n        _LAST[key] = now\n        return True\n    return False\n",
    },
    question: "allow('api', 2) 的限流窗口是多少秒？被拒的请求会重置窗口吗？",
    answerKeys: ["0.5", "不会"],
  },
  {
    files: {
      "codec.py": "def encode(n):\n    alphabet = 'abcdefghjkmnpqrstuvwxyz23456789'\n    if n == 0:\n        return alphabet[0]\n    out = ''\n    while n:\n        n, r = divmod(n, 31)\n        out = alphabet[r] + out\n    return out\n",
    },
    question: "encode 是几进制编码？字符表里排除了哪些字符？encode(31) 返回什么？",
    answerKeys: ["31", "il", "ba"],
  },
  {
    files: {
      "session.py": "class Session:\n    def __init__(self):\n        self.data = {}\n        self.dirty = False\n    def set(self, k, v):\n        self.data[k] = v\n        self.dirty = True\n    def commit(self):\n        self.dirty = False\n        return dict(self.data)\n",
    },
    question: "commit 之后 dirty 是什么值？commit 返回的字典和 self.data 是同一个对象吗？",
    answerKeys: ["False", "不是"],
  },
  {
    files: {
      "ratelimit.py": "from collections import deque\nimport time\n\nWINDOW = {}\n\ndef hits(key, span):\n    now = time.time()\n    q = WINDOW.setdefault(key, deque())\n    while q and q[0] <= now - span:\n        q.popleft()\n    q.append(now)\n    return len(q)\n",
    },
    question: "hits 用什么数据结构做滑动窗口？过期样本什么时候被清除？返回值代表什么？",
    answerKeys: ["deque", "每次调用时", "窗口内次数"],
  },
];
for (const rev of reverseTasks) {
  add(
    {
      id: id("reverse"),
      category: "reverse_analysis",
      workspace: "fixture",
      goal: `只读分析 ${Object.keys(rev.files)[0]}，回答：${rev.question}。用「答案：」开头的短段落回复，不要修改任何文件`,
      allowedTools: ["read_file", "search_code"],
      expectedFiles: [],
      testCommand: `${PY} -X utf8 -c "print('reverse tasks 由判分器按 answer_keys 匹配回复文本，不跑命令')"`,
      acceptance: [`回复包含关键词：${rev.answerKeys.join("、")}`, "没有修改文件"],
      expectedStatus: "succeeded",
      answerKeys: rev.answerKeys,
    },
    {
      "pytest.ini": "[pytest]\n",
      "test_smoke.py": "def test_workspace_intact():\n    assert True\n",
      ...rev.files,
    },
  );
}

/* ── 9. 任务取消/恢复 ×5 ────────────────────────────────── */
for (let i = 0; i < 5; i++) {
  add(
    {
      id: id("cancel-resume"),
      category: "cancel_resume",
      workspace: "fixture",
      goal: "先运行命令 python -c \"import time; time.sleep(90)\"（必须真实跑完这条命令），成功后把 notes.txt 追加一行 done（保留原有内容）。任务可能在任何时刻被用户取消",
      allowedTools: ["read_file", "apply_patch", "run_shell"],
      expectedFiles: [],
      testCommand: `${PY} -X utf8 -c "print('cancel/resume 由 runner 按 expectedStatus=cancelled 判定')"`,
      acceptance: ["取消后状态为 cancelled", "交付文本不含「已完成」"],
      expectedStatus: "cancelled",
      // 取消窗口用真实流逝时间：steps_used 只在收尾落库，运行中恒为 0，
      // 按它触发取消永远发不出去（2026-11-01 实测三次全空发）。
      // 4s 落在规划模型调用期间，取消在下一个节点入口生效，notes.txt 不会被碰。
      runHints: { cancelAfterMs: 4000 + i * 1500 },
    },
    {
      "notes.txt": `baseline ${i}\n`,
      "pytest.ini": "[pytest]\n",
      "test_smoke.py": "def test_workspace_intact():\n    assert True\n",
    },
  );
}

/* ── 写盘 ─────────────────────────────────────────────── */
mkdirSync(TASKS_DIR, { recursive: true });
for (const task of tasks) {
  writeFileSync(path.join(TASKS_DIR, `${task.id}.json`), JSON.stringify(task, null, 2) + "\n", "utf8");
}
const byCat = {};
for (const t of tasks) byCat[t.category] = (byCat[t.category] ?? 0) + 1;
console.log(`生成 ${tasks.length} 个任务定义：`);
for (const [cat, n] of Object.entries(byCat)) console.log(`  ${cat}: ${n}`);
console.log(`fixtures: ${FIXTURES_DIR}`);

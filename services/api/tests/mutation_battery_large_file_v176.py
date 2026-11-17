"""大文件分写纪律的变异电池（两格，红集合精确比对）。"""
from __future__ import annotations
import hashlib, subprocess, sys
from pathlib import Path
API = Path(__file__).resolve().parent.parent
PY = API / ".venv" / "Scripts" / "python.exe"
GRAPH = API / "src" / "blank_api" / "services" / "agent" / "graph.py"
TESTS = ["tests/test_large_file_write_v176.py"]
OLD = ('    "审计与回滚也都看不到它。单个文件的正文超过约 8000 字符时不要硬塞进一次 write_file："\n'
       '    "先写骨架，再用 edit_file 按函数分几次追加/替换。流会在单次工具参数中途被网关截断"\n')
RED = ["test_large_file_must_be_written_in_chunks", "test_rule_follows_output_style_semantics"]

def run():
    r = subprocess.run([str(PY), "-m", "pytest", *TESTS, "-v", "--tb=no", "-q"], cwd=str(API),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, [l.split("::")[-1].split()[0] for l in (r.stdout + r.stderr).splitlines() if "FAILED" in l and "::" in l]

def main() -> int:
    original = GRAPH.read_bytes(); before = hashlib.sha256(original).hexdigest()
    text = original.decode("utf-8"); eol = "\r\n" if "\r\n" in text else "\n"
    rc, red = run()
    if rc != 0: print("基线不绿", red); return 1
    muts = [("删掉分写句", OLD.replace("\n", eol), ""),
            ("把 8000 改成 80000", OLD.replace("\n", eol), OLD.replace("8000", "80000").replace("\n", eol))]
    fails = 0
    for label, old, new in muts:
        if text.count(old) != 1: print("锚点不符", label, text.count(old)); fails += 1; continue
        GRAPH.write_bytes(text.replace(old, new).encode("utf-8"))
        try: rc, red = run()
        finally: GRAPH.write_bytes(original)
        if rc == 0 or sorted(red) != sorted(RED): print("红集合不符", label, red); fails += 1
        else: print("OK", label, red)
    rc, red = run()
    if rc != 0: print("还原后不绿", red); fails += 1
    print(f"变异：{len(muts)-fails}/{len(muts)}")
    return 1 if fails else 0

if __name__ == "__main__": sys.exit(main())

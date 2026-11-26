# -*- coding: utf-8 -*-
import re
from pathlib import Path

EXPECTED_CSV = "day,amount\n2026-11-01,100\n2026-11-01,200\n2026-11-02,50\n2026-11-02,150\n2026-11-03,300"
DAYS = {
    "2026-11-01": {"count": 2, "total": 300, "avg": 150.0},
    "2026-11-02": {"count": 2, "total": 200, "avg": 100.0},
    "2026-11-03": {"count": 1, "total": 300, "avg": 300.0},
}


def summary_text():
    return Path("汇总.md").read_text(encoding="utf-8")


def test_csv_untouched():
    actual = Path("data.csv").read_text(encoding="utf-8").replace("\r\n", "\n").strip()
    assert actual == EXPECTED_CSV, "data.csv 被修改了"


def test_each_day_row():
    text = summary_text()
    for day, expect in DAYS.items():
        line = next((ln for ln in text.splitlines() if day in ln), "")
        assert line, f"缺少 {day} 的汇总行"
        stripped = line.replace(day, " ")
        nums = re.findall(r"\d+(?:\.\d+)?", stripped)
        values = [float(n) for n in nums]
        assert float(expect["count"]) in values, f"{day} 缺数量 {expect['count']}：{line!r}"
        assert float(expect["total"]) in values, f"{day} 缺合计 {expect['total']}：{line!r}"
        assert any(abs(v - expect["avg"]) < 0.005 for v in values), f"{day} 缺平均 {expect['avg']}：{line!r}"


def test_grand_total():
    text = summary_text()
    assert re.search(r"(?<!\d)800(?:\.0+)?(?!\d)", text), "缺少总计 800"

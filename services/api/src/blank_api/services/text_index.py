"""中英文混排的轻量分词与摘要。

聊天记录和项目记忆都要被"随便一句话"检索到。中文没有空格，按空白切词等于
永远搜不到；这里用 unigram + bigram 做确定性切分，不引入分词依赖，也不依赖
某个数据库的全文索引方言（SQLite FTS5 与 Postgres 的 tsvector 不是一套东西）。

索引侧与查询侧必须用同一个函数，否则"索引里有、查询拼不出来"会变成静默漏检。
"""

from __future__ import annotations

import re
import unicodedata

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_RUN = re.compile(r"[a-z0-9][a-z0-9_+#.\-]*")
# 高频虚词单独成 unigram 时几乎命中一切，既抬不高相关度又会让候选集爆炸。
_STOP_CHARS = set("的了是在和与及或就都也很把被让给对从到为这那有一不我你他她它们个中上下要会可该其之所于而之着过吗呢吧啊")
_STOP_WORDS = {
    "the", "and", "for", "with", "this", "that", "from", "into", "are", "was",
    "有", "没有", "怎么", "什么", "如何", "可以", "需要", "一个", "我们", "你们",
}

BIGRAM_WEIGHT = 3
LATIN_WEIGHT = 3
UNIGRAM_WEIGHT = 1


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(text or "")).lower().split())


def tokenize(text: str) -> list[str]:
    """返回去重后的 token；中文含 unigram 与 bigram。顺序保持首次出现。"""
    body = normalize(text)
    tokens: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        if token and token not in _STOP_WORDS and token not in seen:
            seen.add(token)
            tokens.append(token)

    for match in _LATIN_RUN.finditer(body):
        word = match.group(0).strip(".-")
        if len(word) >= 2:
            add(word)
    for match in _CJK_RUN.finditer(body):
        run = match.group(0)
        for index, char in enumerate(run):
            if char not in _STOP_CHARS:
                add(char)
            if index + 1 < len(run):
                pair = run[index : index + 2]
                if pair not in _STOP_WORDS and not all(ch in _STOP_CHARS for ch in pair):
                    add(pair)
    return tokens


def token_weight(token: str) -> int:
    if len(token) == 1 and _CJK_RUN.fullmatch(token):
        return UNIGRAM_WEIGHT
    if _CJK_RUN.fullmatch(token):
        return BIGRAM_WEIGHT
    return LATIN_WEIGHT


def score(text: str, tokens: list[str]) -> int:
    """按命中 token 的权重求和。短文档里重复命中不重复计分。"""
    body = normalize(text)
    return sum(token_weight(token) for token in tokens if token in body)


def snippet(text: str, tokens: list[str], *, width: int = 90) -> str:
    """截取命中位置附近的一段，供搜索列表展示。"""
    body = " ".join(str(text or "").split())
    if len(body) <= width * 2:
        return body
    lowered = body.lower()
    position = -1
    for token in sorted(tokens, key=len, reverse=True):
        found = lowered.find(token)
        if found >= 0:
            position = found
            break
    if position < 0:
        return body[: width * 2 - 1] + "…"
    start = max(0, position - width // 2)
    end = min(len(body), start + width * 2)
    prefix = "…" if start else ""
    suffix = "…" if end < len(body) else ""
    return prefix + body[start:end] + suffix


def highlight_ranges(text: str, tokens: list[str]) -> list[list[int]]:
    """给前端高亮用的区间（左闭右开），合并重叠。"""
    body = str(text or "")
    lowered = body.lower()
    ranges: list[tuple[int, int]] = []
    for token in tokens:
        if len(token) < 2:
            continue
        start = 0
        while True:
            found = lowered.find(token, start)
            if found < 0:
                break
            ranges.append((found, found + len(token)))
            start = found + len(token)
    if not ranges:
        return []
    ranges.sort()
    merged = [list(ranges[0])]
    for start, end in ranges[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged

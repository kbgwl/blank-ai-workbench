"""#89：提示词里的「今天几号」与治理快照同一个口径，并且压缩后仍然留着。

钉的不是「提示词里出现了一个日期」——那样自己 ``strftime`` 一份也能过。五条各自会走形的承诺：

1. **同源**：把 `clock_fields` 换成合成值，提示词与两条快照必须一起变成那一份。任何一侧
   退回自己 `strftime`，这条就红——同格式各写两份在真实时钟下看不出差别。
2. **换钟就跟**：真源不动、只把钟钉住，提示词里就是被钉住的那一天，且只有 ISO 那一种写法。
3. **唯一的数字来源**：摘掉日期与时区，句子里不该再有数字——多出来的只能来自第三处时钟。
4. **只到「天」**：同一分钟内两次拼装逐字相同。掺进时分，第一段 system 每步都变，
   前缀缓存与快照去重（`governance.digest`）同时失效。
5. **活在第一段 system 里**：压缩只保留「第一条 system + 当前目标」（`context._split_prefix`），
   另起一条 system 会在任务后半程消失，表现为模型反而不知道今天几号。
"""

from datetime import datetime, timedelta, timezone

from blank_api.services.agent import governance
from blank_api.services.agent.context import compact_messages
from blank_api.services.agent.graph import clock_note, initial_messages

SYNTHETIC = {"current_date": "1999-12-31", "timezone": "SyntheticZone"}


def _system(messages: list[dict]) -> str:
    assert messages[0]["role"] == "system", messages[0]
    return str(messages[0]["content"])


class _FixedClock:
    """只替掉 `governance` 里那一处 `datetime.now()`，其余照常。"""

    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def now(self, tz: object = None) -> datetime:
        return self._moment


def test_prompt_reads_clock_fields_not_its_own_clock(monkeypatch) -> None:
    """口径只能有一个真源：`clock_fields` 换成合成值，三个消费者必须一起变成那一份。

    graph 里写自己的 `strftime` ⇒ 提示词仍是今天的真实日期；`turn_context` 或 `world_state`
    里任何一处写自己的 `strftime` ⇒ 那一条跟不上合成值。日期与时区两个字段都要跟，
    它们是分别拼进句子里的。
    """
    monkeypatch.setattr(governance, "clock_fields", lambda *a, **k: dict(SYNTHETIC))
    content = _system(initial_messages("目标", ["判据"]))
    assert SYNTHETIC["current_date"] in content, content[-400:]
    assert SYNTHETIC["timezone"] in content, content[-400:]
    snapshot = governance.turn_context(
        node="model",
        step=1,
        role="root",
        model="some-model",
        provider="some-provider",
        permission_mode="default",
        permissions={},
    )
    assert snapshot["current_date"] == SYNTHETIC["current_date"], snapshot
    assert snapshot["timezone"] == SYNTHETIC["timezone"], snapshot
    world = governance.world_state(permission_mode="default", model="some-model", provider="some-provider")
    assert world["state"]["current_date"] == SYNTHETIC["current_date"], world["state"]
    assert world["state"]["timezone"] == SYNTHETIC["timezone"], world["state"]


def test_prompt_shows_the_clock_it_was_built_under(monkeypatch) -> None:
    """真源不动、只换钟：钉住哪一天，提示词里就是哪一天，而且只有 ISO 那一种写法。

    这条防的是「合成值恰好等于今天所以看不出走形」。斜杠写法与美式月日写法都必须不出现——
    格式一改，`clock_fields` 与提示词就不再逐字相等。
    """
    moment = datetime(2026, 3, 14, 13, 45, 59).astimezone()
    monkeypatch.setattr(governance, "datetime", _FixedClock(moment))
    content = _system(initial_messages("目标", ["判据"]))
    iso = moment.strftime("%Y-%m-%d")
    assert iso in content, (iso, content[-400:])
    for wrong_format in (moment.strftime("%Y/%m/%d"), moment.strftime("%m/%d/%Y")):
        assert wrong_format not in content, (wrong_format, content[-400:])


def test_note_has_no_third_source_of_digits(monkeypatch) -> None:
    """日期与时区必须是这句里唯一的数字来源——第三个来源一定是某处的时钟。

    `clock_fields` 换成合成值后，把这两个值从句子摘掉，剩下的解释文案里不该再有任何数字：
    多出来的一串只能是「时分秒」（本机实测：带上 `%H:%M` 时这里剩 `0950`）。
    这条补的是 `test_note_stops_at_the_day` 抓不到的那种走形——graph 绕过 `clock_fields`
    自己 `datetime.now()` 时，钉住治理侧的钟对它无效，两次读到的真实时刻几乎相同，
    「同一分钟内逐字相同」那条照样绿。
    """
    monkeypatch.setattr(governance, "clock_fields", lambda *a, **k: dict(SYNTHETIC))
    note = clock_note()
    residue = note.replace(SYNTHETIC["current_date"], "").replace(SYNTHETIC["timezone"], "")
    digits = "".join(char for char in residue if char.isdigit())
    assert digits == "", (digits, residue)


def test_the_day_reported_belongs_to_the_instants_own_zone_not_utc() -> None:
    """跨 UTC 分界那一刻：报的必须是**那一刻所在时区**的那天，不是 UTC 那天。

    2026-10-26 00:30（UTC+8）同时是 2026-10-25 16:30Z。用户在东亚的半夜问「今天几号」，
    实现里任何一处把 `datetime.now().astimezone()` 换成 `now(timezone.utc)` / `utcnow()`，
    提示词就会念成昨天——而这条判据在任何机器上都能红，因为它注入的是时刻，不读宿主时区。
    分界前提也钉住：两个日期确实分家，否则这一条是空的。
    """
    east = datetime(2026, 10, 26, 0, 30, tzinfo=timezone(timedelta(hours=8)))
    west = datetime(2026, 10, 25, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert east.astimezone(timezone.utc).strftime("%Y-%m-%d") == "2026-10-25", east
    assert west.astimezone(timezone.utc).strftime("%Y-%m-%d") == "2026-10-26", west
    assert governance.clock_fields(east)["current_date"] == "2026-10-26", governance.clock_fields(east)
    assert governance.clock_fields(west)["current_date"] == "2026-10-25", governance.clock_fields(west)
    # 同一 instant 换一种时区写法必须还是同一个时刻，日期跟着时区走而不是跟着 UTC 走
    same_moment = east.astimezone(timezone.utc)
    assert governance.clock_fields(same_moment)["current_date"] == "2026-10-25", same_moment


def test_real_clock_reports_a_full_iso_date_and_a_timezone() -> None:
    """真钟下的形状：`%Y-%m-%d` 解得开、时区不为空。

    这条刻意不比对「是不是今天」：连读两次 `now()` 在跨午夜那一瞬会自己跟自己不一致，
    那是时钟的 race，不是代码的缺陷。「钉住的那天就是提示词里的那天」由上一条证明。
    """
    clock = governance.clock_fields()
    parsed = datetime.strptime(clock["current_date"], "%Y-%m-%d")
    assert parsed.year >= 2020, clock
    assert clock["timezone"].strip(), clock


def test_note_stops_at_the_day(monkeypatch) -> None:
    """固定到 13:45:59 造一条：句子里只能出现日期，不能出现任何时刻。

    「只到天」是这套口径的全部意义：一旦带上时分，每步拼装的字符串都不同，第一段 system
    失去缓存价值，快照 digest 也每步宣称「策略变了」。
    时区名可能自带冒号（`UTC+08:00` 这类形状），所以不写「句子里没有冒号」这种判据，
    只钉三种时刻写法确实没出现。
    """
    moment = datetime(2026, 3, 14, 13, 45, 59).astimezone()
    monkeypatch.setattr(governance, "datetime", _FixedClock(moment))
    note = clock_note()
    assert moment.strftime("%Y-%m-%d") in note, note
    for forbidden in (
        moment.strftime("%H:%M:%S"),
        moment.strftime("%H:%M"),
        moment.strftime("%Y-%m-%d %H"),
    ):
        assert forbidden not in note, (forbidden, note)


def test_same_day_two_assemblies_are_identical(monkeypatch) -> None:
    """同一分钟内连续两次拼装必须逐字相同——否则前缀缓存每步失效。"""
    start = datetime(2026, 3, 14, 13, 0, 0).astimezone()
    monkeypatch.setattr(governance, "datetime", _FixedClock(start))
    first = clock_note()
    monkeypatch.setattr(governance, "datetime", _FixedClock(start + timedelta(minutes=59, seconds=59)))
    assert clock_note() == first, (first, clock_note())

    # 对照格（必须真的不同）：跨过午夜日期确实换了，还写着同一句就是假稳定。
    monkeypatch.setattr(governance, "datetime", _FixedClock(start + timedelta(days=1)))
    next_day = clock_note()
    assert next_day != first, next_day
    assert "2026-03-15" in next_day, next_day


def test_date_lives_in_the_first_system_message_only() -> None:
    """并进第一段 system，而不是另起一条：压缩后只有第一条 system 一定留下。"""
    messages = initial_messages("目标", ["判据"])
    systems = [item for item in messages if item.get("role") == "system"]
    assert len(systems) == 1, systems
    assert messages[-1]["role"] == "user", messages[-1]
    assert governance.clock_fields()["current_date"] in str(systems[0]["content"])


def test_date_survives_compaction() -> None:
    """真压缩一次（走 `compact_messages`），日期必须在压缩后仍然读得到。

    这条是「为什么不另起一条消息」的理由本身：先确认压缩真的发生了（有压缩标记），
    再确认第一段 system 里的日期还在。少了前一个判据，历史没超预算时会假绿。
    """
    date = governance.clock_fields()["current_date"]
    messages = initial_messages("目标", ["判据"])
    history = []
    for index in range(40):
        history.append({"role": "assistant", "content": f"第 {index} 步的过程记录：" + ("填充" * 400)})
        history.append({"role": "user", "content": f"第 {index} 步的追问：" + ("填充" * 400)})
    inflated = [messages[0], *history, messages[-1]]
    compacted = compact_messages(inflated, max_chars=6000)

    assert any("上下文已压缩" in str(item.get("content")) for item in compacted), [
        str(item.get("content"))[:60] for item in compacted
    ]
    assert len(compacted) < len(inflated), (len(compacted), len(inflated))
    first = compacted[0]
    assert first.get("role") == "system", first
    assert date in str(first["content"]), str(first["content"])[-300:]

    # 对照格（要能失败）：同一句日期从第一段摘掉、改成另起的一条 system，
    # 压缩后第一段活下来、日期跟着另一条一起没了。
    # 判据写的是「压缩结果里再也读不到日期」，不是「读不到那句文案」——
    # 第一段本身就带着那句文案，按文案找会把活下来的第一段误认成"另起的那条"（实踩过一次）。
    stripped = {**messages[0], "content": str(messages[0]["content"]).replace(clock_note(), "").rstrip()}
    assert "本机时区" not in str(stripped["content"]), stripped
    split_shape = [stripped, {"role": "system", "content": clock_note()}, *history, messages[-1]]
    compacted_split = compact_messages(split_shape, max_chars=6000)
    assert any("上下文已压缩" in str(item.get("content")) for item in compacted_split)
    assert not any(date in str(item.get("content")) for item in compacted_split), [
        str(item.get("content"))[:80] for item in compacted_split
    ]

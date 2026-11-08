"""桌面通知的取数口：把后端**已经判过**的「该不该打扰」交给壳去弹。

为什么判定在这里、弹窗在那边（职责分界，别混）：

* ``agent/notify.py`` 只回答"这一个事件该不该打扰"，答案连同理由写进
  ``notify.decided`` 事件。这是**唯一**的判定源。
* ``channels/delivery.py`` 负责把判完的结果发到企业微信/钉钉/飞书。
* 这个接口不判任何事——它只把 ``notify.decided`` 里 ``disturb=true`` 的那几条
  摊出来，供桌面壳取走。壳那边只做三件壳自己才知道的事：**这条我弹过没有、
  用户此刻是不是就看着这个窗口、点开要跳到哪一屏**。

所以这里刻意不返回 ``disturb=false`` 的行：判都判完了，再让客户端复判一遍，
两边规则一旦漂移就会出现"后端说静默、桌面在响"这种没法解释的状态。

游标用 ``since``（``created_at`` 的 ISO 串）而不是序号：``agent_events.seq``
是**按任务**递增的，跨任务比大小没有意义。比较是**严格大于**，理由见下面
``notifications()`` 的注释——一句话：重复打扰比极少漏一秒更糟。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from blank_api.api.auth import require_token
from blank_api.deps import DbSession
from blank_api.models.task import AgentEvent, Task

router = APIRouter(tags=["notifications"], dependencies=[Depends(require_token)])

# 只认这一种事件：它是 `_record_notify_decision` 的产物，带完整的判定与理由。
DECIDED_EVENT = "notify.decided"

# 首次调用（客户端还没有游标）往前看多久。设成一天而不是"全部"：桌面刚装好
# 就把三个月前的完成事件一口气弹出来，是比不通知更糟的体验。
DEFAULT_LOOKBACK = timedelta(hours=24)

# 游标用「严格大于」还是「大于等于并留缓冲」——这里选了严格大于，取舍写在
# ``_since_floor`` 的注释里。


def _iso(moment: datetime | None) -> str:
    return moment.isoformat() if moment else ""


def _parse_since(raw: str) -> datetime | None:
    """解析客户端游标。坏值不报错，退回"按默认回看窗口"——壳拿到 400 只会更懵。"""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    # 库里存的是 naive UTC（``server_default=func.now()`` 在 SQLite 下就是
    # CURRENT_TIMESTAMP = UTC）。带时区的游标必须先折成 UTC，否则比大小会偏
    # 一整个时区——东八区偏 8 小时，症状是"通知要么重弹一遍、要么整晚不响"。
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _origin_of(payload: dict) -> str:
    """这一次是**怎么**被激起的：人自己发的，还是定时/盯梢自己跑的。

    **直接读事件里的 ``origin``，不要在这边重新算一遍。** 这里曾经按
    ``task.dispatch_snapshot["intent"]`` 现算，结果是：``_record_notify_decision``
    已经从触发器里把 ``origin`` 补成 ``schedule`` 了（快照是空的时候），而这个接口
    又按空快照算回 ``user``——同一件事有两个人算，就一定有一个是错的。

    症状就是真机验收看到的：定时提醒到点跑完，桌面弹出来写的却是「做完了」。
    判定归判定，取数归取数，取数不许有自己的口径。
    """
    return str((payload or {}).get("origin") or "") or "user"


@router.get("/notifications")
async def notifications(
    db: DbSession,
    since: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    """返回 ``since`` 之后、判定为「该打扰」的通知，按时间正序（老的在前）。

    正序是有意的：壳按顺序弹，屏幕上最后出现的那条才是最新的；倒序会让最新
    的提示先弹、然后被更早的盖住。

    游标语义是**严格大于**：

    ``created_at`` 在 SQLite 里只到秒，所以"大于等于 + 留缓冲"会让同一批通知
    被反复读到。两个方向的代价并不对称——重复读意味着**用户被同一个提醒骚扰
    两次**，而漏读只可能发生在"同一秒里、两次轮询之间又写了一条"这种情况：
    任务完成不会有这种密度，审批中断点更不会。宁可极少漏一秒，不可重复打扰。
    """
    cap = max(1, min(int(limit), 100))
    # 与 monitor.py 同一个口径：库内存 naive UTC，不要拿本地时间比。
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    floor = _parse_since(since)
    if floor is None:
        floor = now - DEFAULT_LOOKBACK
        strict = False
    else:
        strict = True

    condition = AgentEvent.created_at > floor if strict else AgentEvent.created_at >= floor
    rows = (
        await db.execute(
            select(AgentEvent)
            .where(AgentEvent.type == DECIDED_EVENT, condition)
            .order_by(AgentEvent.created_at.desc(), AgentEvent.seq.desc())
            .limit(cap * 4)
        )
    ).scalars().all()

    # 只留 disturb=true。``payload`` 是 JSON 列，过滤放在 Python 侧：sqlite 的
    # JSON 函数在旧版本上不一定编进去了，没必要为这个查询赌一把。
    #
    # 先按「最新 N 条」截断，再翻成时间正序：反过来（先 reversed 再截）会取到
    # 最老的那 N 条，而游标又指向最新——中间的通知就永远读不到了。
    #
    # 超过 cap 的那些**主动丢弃**，游标照样推进到最新。这不是漏读：一屏弹二十
    # 条以上已经不算提醒了，攒着下次补弹只会更糟（壳那边还有一层去重与静音）。
    kept = [row for row in rows if bool((row.payload or {}).get("disturb"))]
    kept = kept[:cap]
    kept.reverse()

    # 一次查齐去重后的任务，别在循环里逐条 get（20 条通知 = 20 次往返）。
    tasks: dict[str, Task] = {}
    task_ids = {row.task_id for row in kept}
    if task_ids:
        found = (
            await db.execute(select(Task).where(Task.id.in_(task_ids)))
        ).scalars().all()
        tasks = {row.id: row for row in found}

    items: list[dict] = []
    for row in kept:
        payload = dict(row.payload or {})
        task = tasks.get(row.task_id)
        status = str(payload.get("status") or "")
        items.append(
            {
                "id": row.id,
                "task_id": row.task_id,
                "thread_id": str(getattr(task, "thread_id", "") or ""),
                "title": str(getattr(task, "title", "") or ""),
                "goal": str(getattr(task, "goal", "") or "")[:300],
                "status": status,
                "kind": str(payload.get("event") or ""),
                "origin": _origin_of(payload),
                "reason": str(payload.get("reason") or ""),
                "policy": str(payload.get("policy") or ""),
                "created_at": _iso(row.created_at),
            }
        )

    # 游标给"这一批里最新那条"的时间：下一轮从这儿往后接着读。这批为空时不动
    # 游标，否则客户端会在空轮次里把自己推回默认回看窗口，重弹一遍旧通知。
    cursor = _iso(kept[-1].created_at) if kept and kept[-1].created_at else ""
    return {"now": _iso(now), "items": items, "cursor": cursor}

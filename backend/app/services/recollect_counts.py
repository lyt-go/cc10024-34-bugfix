"""重采运行计数记录。

一次错误恢复的“重采运行”会从两条路径收到结果：

* 运行历史一侧先登记尝试次数（``attempts``），它表示重试了几次，**不是**失败页数；
* 采集一侧按页发送真实采集回执（``receipts``），成功/失败都要以回执为准。

两条路径的到达顺序不固定：回执可能早于尝试登记，也可能迟到。本模块因此把回执
按“页”去重（同一页的迟到/重复回执不再累加），并把计数记录落到
``data/runs/recollect_run_counts.json``，供 ``run_history`` 消费。

关键口径：

* 未结算（``settled is False``）时，失败页数不是已知数，统一为 ``None``；
* 结算后最终统计只根据实际回执计算：
    - 有回执：``success_pages`` / ``failed_pages`` 均由去重后的回执得出；
    - 无回执：采集数据仍未确认（``collection_confirmed is False``），失败页数保持 ``None``；
* 尝试次数与失败页数分开保存，互不覆盖；
* 暂存（运行尚未建立时先到的回执）在运行建立时并入，不会作为独立行残留。
"""
from __future__ import annotations

from typing import Any

from app import persistence as persist

_FILE_SUB = "runs"
_FILE_NAME = "recollect_run_counts.json"

# runs:    run_id(str) -> 计数记录
# pending: recovery_task_id(str) -> 运行尚未建立时先到的回执缓冲
_STATE: dict[str, Any] = {"runs": {}, "pending": {}, "next_run_id": 1}
_LOADED = False


def _ensure_loaded() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    data = persist.read_json(_FILE_SUB, _FILE_NAME)
    if not isinstance(data, dict):
        return
    runs = data.get("runs")
    pending = data.get("pending")
    if isinstance(runs, dict):
        _STATE["runs"] = {str(k): v for k, v in runs.items()}
    if isinstance(pending, dict):
        _STATE["pending"] = {str(k): v for k, v in pending.items()}
    next_run_id = data.get("next_run_id")
    if isinstance(next_run_id, int) and next_run_id > 0:
        _STATE["next_run_id"] = next_run_id
    else:
        _STATE["next_run_id"] = max(
            (int(record.get("run_id", 0)) for record in _STATE["runs"].values()), default=0) + 1


def _save() -> None:
    persist.write_json(_FILE_SUB, _FILE_NAME, {
        "runs": _STATE["runs"],
        "pending": _STATE["pending"],
        "next_run_id": _STATE["next_run_id"],
    })


def _page_key(page_key: Any) -> str:
    return str(page_key).strip()


def _recompute(record: dict[str, Any]) -> dict[str, Any]:
    """按真实回执重新推导计数；未结算时失败页数保持未知。"""
    receipts: dict[str, bool] = record.get("receipts") or {}
    successes = sum(1 for value in receipts.values() if value)
    failures = sum(1 for value in receipts.values() if not value)

    record["received_pages"] = len(receipts)
    record["success_pages"] = successes

    if not record.get("settled"):
        # 未结算：失败页不是已知数，不能拿尝试次数顶替。
        record["failed_pages"] = None
        record["collection_confirmed"] = False
        record["status"] = "运行中"
        return record

    if receipts:
        # 最终统计只根据实际采集回执。
        record["failed_pages"] = failures
        record["collection_confirmed"] = True
        if failures == 0:
            record["status"] = "已完成"
        elif successes == 0:
            record["status"] = "已失败"
        else:
            record["status"] = "部分成功"
    else:
        # 已确认恢复，但采集数据还没到：仍属未确认，不能填 0。
        record["failed_pages"] = None
        record["collection_confirmed"] = False
        record["status"] = "运行中"
    return record


def _new_run(recovery_task_id: int, *, run_code: str, recovery_task_code: str, plan: str) -> dict[str, Any]:
    now = persist.now_iso()
    run_id = int(_STATE["next_run_id"])
    _STATE["next_run_id"] = run_id + 1
    record = {
        "run_id": run_id,
        "run_code": run_code or f"RECOLLECT-{run_id:04d}",
        "run_history_id": None,
        "recovery_task_id": int(recovery_task_id),
        "recovery_task_code": recovery_task_code,
        "plan": plan,
        "attempts": 0,
        "success_pages": 0,
        "failed_pages": None,
        "received_pages": 0,
        "receipts": {},
        "settled": False,
        "collection_confirmed": False,
        "status": "运行中",
        "created_at": now,
        "updated_at": now,
        "settled_at": "",
    }
    _STATE["runs"][str(run_id)] = record
    # 回执先到：把暂存回执并入本次运行，同一页仍只保留第一次回执。
    buffered = _STATE["pending"].pop(str(recovery_task_id), None)
    if buffered:
        for page, success in (buffered.get("receipts") or {}).items():
            record["receipts"].setdefault(page, bool(success))
    return record


def start_run(
    recovery_task_id: int,
    *,
    run_code: str = "",
    recovery_task_code: str = "",
    plan: str = "",
) -> dict[str, Any]:
    """显式开启一次新的重采运行（并接收暂存回执）。"""
    _ensure_loaded()
    record = _new_run(
        recovery_task_id,
        run_code=run_code,
        recovery_task_code=recovery_task_code,
        plan=plan,
    )
    _recompute(record)
    _save()
    return record


def ensure_run(
    recovery_task_id: int,
    *,
    run_code: str = "",
    recovery_task_code: str = "",
    plan: str = "",
) -> dict[str, Any]:
    """取该恢复任务最近的重采运行，没有则建立；已结算的运行也可继续接收迟到回执。"""
    _ensure_loaded()
    record = latest_run_for_task(recovery_task_id)
    if record is None:
        return start_run(
            recovery_task_id,
            run_code=run_code,
            recovery_task_code=recovery_task_code,
            plan=plan,
        )
    if run_code:
        record["run_code"] = run_code
    if recovery_task_code:
        record["recovery_task_code"] = recovery_task_code
    if plan:
        record["plan"] = plan
    _recompute(record)
    _save()
    return record


def add_attempts(recovery_task_id: int, attempts: int = 1, **meta: Any) -> dict[str, Any]:
    """登记尝试次数。它只影响 ``attempts``，绝不写入失败页数。

    若最近一次运行已结算，说明这是新一轮重试，另起一条重采运行。
    """
    _ensure_loaded()
    record = latest_run_for_task(recovery_task_id)
    if record is None or record.get("settled"):
        record = start_run(recovery_task_id, **meta)
    elif meta:
        record = ensure_run(recovery_task_id, **meta)
    record["attempts"] = int(record.get("attempts", 0)) + int(attempts)
    record["updated_at"] = persist.now_iso()
    _recompute(record)
    _save()
    return record


def set_attempts(recovery_task_id: int, attempts: int, **meta: Any) -> dict[str, Any]:
    """把尝试次数设为给定值（例如来自恢复任务字段），不影响任何页计数。"""
    _ensure_loaded()
    record = ensure_run(recovery_task_id, **meta)
    record["attempts"] = int(attempts)
    record["updated_at"] = persist.now_iso()
    _recompute(record)
    _save()
    return record


def attach_run_history(run_id: int, run_history_id: int) -> dict[str, Any] | None:
    _ensure_loaded()
    record = _STATE["runs"].get(str(run_id))
    if record is None:
        return None
    record["run_history_id"] = int(run_history_id)
    record["updated_at"] = persist.now_iso()
    _save()
    return record


def settle_run(recovery_task_id: int) -> dict[str, Any]:
    """结算该恢复任务的当前重采运行：此后失败页数才由真实回执给出。"""
    _ensure_loaded()
    record = ensure_run(recovery_task_id)
    if not record.get("settled"):
        record["settled"] = True
        record["settled_at"] = persist.now_iso()
    record["updated_at"] = persist.now_iso()
    _recompute(record)
    _save()
    return record


def record_receipt(
    *,
    recovery_task_id: int,
    page_key: Any,
    success: bool,
    run_id: int | None = None,
) -> dict[str, Any] | None:
    """登记一条采集页回执。

    * 同一页的迟到或重复回执返回原记录且不累加；
    * 若运行尚未建立，先按恢复任务暂存，待运行建立时并入；
    * 运行已存在时默认挂到该任务最新的一次运行上，可用 ``run_id`` 指定。
    """
    _ensure_loaded()
    page = _page_key(page_key)
    if not page:
        raise ValueError("采集回执缺少页面标识")

    record: dict[str, Any] | None = None
    if run_id is not None:
        record = _STATE["runs"].get(str(run_id))
    if record is None:
        record = latest_run_for_task(recovery_task_id)

    if record is None:
        pending = _STATE["pending"].setdefault(str(recovery_task_id), {"receipts": {}})
        pending["receipts"].setdefault(page, bool(success))
        _save()
        return None

    receipts = record.setdefault("receipts", {})
    if page not in receipts:
        receipts[page] = bool(success)
        record["updated_at"] = persist.now_iso()
        _recompute(record)
        _save()
    return record


def latest_run_for_task(recovery_task_id: int) -> dict[str, Any] | None:
    _ensure_loaded()
    matched = [
        (int(record.get("run_id", 0)), record)
        for record in _STATE["runs"].values()
        if int(record.get("recovery_task_id", -1)) == int(recovery_task_id)
    ]
    if not matched:
        return None
    matched.sort(key=lambda item: item[0])
    return matched[-1][1]


def get_run(run_id: int) -> dict[str, Any] | None:
    _ensure_loaded()
    return _STATE["runs"].get(str(run_id))


def get_run_by_history_id(run_history_id: int) -> dict[str, Any] | None:
    _ensure_loaded()
    for record in _STATE["runs"].values():
        if record.get("run_history_id") == int(run_history_id):
            return record
    return None


def records() -> list[dict[str, Any]]:
    _ensure_loaded()
    return sorted(_STATE["runs"].values(), key=lambda record: int(record.get("run_id", 0)))


def records_for_task(recovery_task_id: int) -> list[dict[str, Any]]:
    return [
        record for record in records()
        if int(record.get("recovery_task_id", -1)) == int(recovery_task_id)
    ]


def unconfirmed_records() -> list[dict[str, Any]]:
    """采集数据仍未确认（尚无真实回执）的运行。"""
    return [record for record in records() if not record.get("collection_confirmed")]


def pending_receipts(recovery_task_id: int) -> dict[str, bool] | None:
    _ensure_loaded()
    pending = _STATE["pending"].get(str(recovery_task_id))
    if not pending:
        return None
    return dict(pending.get("receipts") or {})


def reset() -> None:
    """清空计数记录（仅测试/维护使用）。"""
    global _LOADED
    _STATE["runs"] = {}
    _STATE["pending"] = {}
    _STATE["next_run_id"] = 1
    _LOADED = True
    _save()

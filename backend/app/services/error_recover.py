"""错误恢复领域服务：采集失败后的重试与恢复，消费batch_queue失败信息，写入run_history。

重采结果的计数口径见 :mod:`app.services.recollect_counts`：

* ``已重试次数``/``尝试次数`` 只表示重试了几次，**不再**当作失败行数；
* 成功/失败页数一律按真实采集回执计算，同一页的迟到或重复回执只算一次；
* 未结算时失败页数不是已知数，不写占位字符串，也不写 0。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services.base import BaseModuleService, register_hook
from app.services import recollect_counts
from app import persistence as persist


def _task_meta(entry: dict[str, Any]) -> dict[str, str]:
    return {
        "run_code": "",
        "recovery_task_code": str(entry.get("任务编号", "")),
        "plan": str(entry.get("关联计划", "")),
    }


def _numeric_attempts(entry: dict[str, Any]) -> int | None:
    raw = str(entry.get("已重试次数") or "").strip()
    return int(raw) if raw.isdigit() else None


def _find_history_row(run_history_id: Any) -> dict[str, Any] | None:
    if run_history_id is None:
        return None
    for row in store.rows("run_history"):
        if int(row.get("id", 0)) == int(run_history_id):
            return row
    return None


def _sync_run_history(entry: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """把重采计数记录同步进它对应的 run_history 行（没有则建立，绝不重复建行）。"""
    row = _find_history_row(record.get("run_history_id"))
    if row is None:
        run_rows = store.rows("run_history")
        new_id = max((int(r.get("id", 0)) for r in run_rows), default=0) + 1
        row = {
            "id": new_id,
            "记录编号": f"RUN-RECOVER-{new_id:04d}",
            "关联计划": "",
            "开始时间": persist.now_iso(),
            "结束时间": "",
            "采集行数": None,
            "失败行数": None,
            "执行日志": "",
            "异常摘要": "",
            "status": "运行中",
            "pending": True,
            "abnormal": False,
            "version": "",
            "updated_at": persist.now_iso(),
        }
        run_rows.append(row)
        record = recollect_counts.attach_run_history(record["run_id"], new_id) or record

    attempts = int(record.get("attempts", 0))
    success_pages = int(record.get("success_pages", 0))
    failed_pages = record.get("failed_pages")           # 未确认时保持 None
    received_pages = int(record.get("received_pages", 0))
    confirmed = bool(record.get("collection_confirmed"))
    settled = bool(record.get("settled"))

    row["记录编号"] = row.get("记录编号") or f"RUN-RECOVER-{row['id']:04d}"
    row["关联计划"] = record.get("plan") or entry.get("关联计划", "")
    # 尝试次数与失败页数分开保存，互不顶替。
    row["尝试次数"] = attempts
    row["成功页数"] = success_pages
    row["失败页数"] = failed_pages
    # 采集行数=真实回执覆盖的总页数（成功+失败）；未确认时不写占位字符串，也不写 0。
    row["采集行数"] = received_pages if confirmed else None
    row["失败行数"] = failed_pages
    row["关联恢复任务"] = record.get("recovery_task_code") or entry.get("任务编号", "")
    row["恢复任务ID"] = record.get("recovery_task_id")
    row["采集数据确认"] = "已确认" if confirmed else "未确认"
    row["异常摘要"] = entry.get("错误类型", "")
    row["执行日志"] = (
        f"从error_recover任务{entry.get('任务编号')}恢复；"
        f"尝试{attempts}次；"
        f"成功{success_pages}页；"
        f"失败{failed_pages if failed_pages is not None else '未知'}页"
    )
    row["结束时间"] = record.get("settled_at", "") if settled else ""
    row["status"] = record.get("status", "运行中")
    row["pending"] = not confirmed
    row["abnormal"] = row["status"] == "已失败"
    row["updated_at"] = persist.now_iso()
    return row


def _on_retry(entry: dict[str, Any]) -> None:
    """发起重试：登记一次尝试，并让运行历史侧看到尝试次数。"""
    record = recollect_counts.add_attempts(entry.get("id"), 1, **_task_meta(entry))
    entry["已重试次数"] = str(record["attempts"])
    _sync_run_history(entry, record)


def _on_confirm(entry: dict[str, Any]) -> None:
    """确认恢复：结算本次重采，最终统计只依据真实采集回执。"""
    record = recollect_counts.latest_run_for_task(entry.get("id"))
    claimed = _numeric_attempts(entry)
    if claimed is not None and (record is None or int(record.get("attempts", 0)) == 0):
        # 任务字段里已登记过尝试次数（例如登记时带 3），但还没走过逐次登记。
        record = recollect_counts.set_attempts(entry.get("id"), claimed, **_task_meta(entry))
    record = recollect_counts.settle_run(entry.get("id"))
    entry["已重试次数"] = str(record["attempts"])
    row = _sync_run_history(entry, record)
    persist.append_jsonl("runs", "run_history.jsonl", {
        "module": "run_history", "entity": "运行记录", "entry_id": row["id"],
        "status": row.get("status", "运行中"), "version": "", "action": "auto_create_from_recovery",
        "timestamp": persist.now_iso(), "abnormal": row.get("abnormal", False),
        "source_module": "error_recover", "source_id": entry.get("id"),
        "attempts": record.get("attempts", 0),
        "failed_pages": record.get("failed_pages"),
        "collection_confirmed": record.get("collection_confirmed", False),
    })


register_hook("error_recover", "发起重试", _on_retry)
register_hook("error_recover", "确认恢复", _on_confirm)


class ErrorRecoverService(BaseModuleService):
    MODULE = "error_recover"
    ENTITY = "恢复任务"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["任务编号", "关联计划", "失败步骤", "错误类型", "重试边界", "回滚策略", "已重试次数", "恢复状态"]
    REQUIRED_FIELDS = ["任务编号", "关联计划", "失败步骤"]
    STATUS_ORDER = ["待处理", "重试中", "已恢复", "已放弃"]
    ACTION_RULES = {"发起重试": "重试中", "确认恢复": "已恢复", "标记放弃": "已放弃"}
    NEGATIVE_ACTIONS = []

    def register_attempts(self, entry_id: int, count: int = 1) -> dict[str, Any] | None:
        """登记若干次尝试（不影响失败页数）。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        record = recollect_counts.add_attempts(entry_id, int(count), **_task_meta(entry))
        entry["已重试次数"] = str(record["attempts"])
        _sync_run_history(entry, record)
        return record

    def record_receipt(self, entry_id: int, page_key: Any, success: bool) -> dict[str, Any] | None:
        """登记一条采集页回执；同一页的迟到/重复回执不累加。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        record = recollect_counts.record_receipt(
            recovery_task_id=entry_id, page_key=page_key, success=success)
        if record is not None:
            _sync_run_history(entry, record)
        return record

    def recollect_record(self, entry_id: int) -> dict[str, Any] | None:
        return recollect_counts.latest_run_for_task(entry_id)

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        tasks = store.rows(self.MODULE)
        # 关联的batch_queue统计数据
        batches = store.rows("batch_queue")
        plan_ids = {r.get("关联计划") for r in tasks}
        base["linked_batches"] = sum(1 for b in batches if b.get("关联计划") in plan_ids)
        # 恢复关联的run_history
        runs = persist.read_jsonl("runs", "run_history.jsonl")
        recovery_runs = [r for r in runs if "recover" in str(r.get("action", ""))]
        base["recovery_runs"] = len(recovery_runs)
        # 尝试次数按恢复任务字段统计
        base["retry_total"] = sum(
            int(str(r.get("已重试次数") or "0")) for r in tasks if str(r.get("已重试次数") or "").isdigit())
        # 重采计数记录：尝试次数与失败页数分开汇总
        counters = recollect_counts.records()
        base["recollect_runs"] = len(counters)
        base["recollect_attempts"] = sum(int(c.get("attempts", 0)) for c in counters)
        base["recollect_failed_pages"] = sum(
            int(c["failed_pages"]) for c in counters if c.get("failed_pages") is not None)
        base["recollect_unconfirmed"] = len(recollect_counts.unconfirmed_records())
        return base

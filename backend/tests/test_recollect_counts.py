"""重采结果计数回归测试。

覆盖题目描述的计数错位：重试三次不等于失败三行；成功/失败页数只按真实采集回执
统计；同一页的迟到或重复回执不累加；未结算时失败页数为未知；两路结果到达顺序
颠倒（回执先于尝试登记，或确认先于回执）都不能把两类数量写错。
"""
from __future__ import annotations

import os
import sys

backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app import persistence as persist
from app.services import recollect_counts
from app.services.error_recover import ErrorRecoverService
from app.services.run_history import RunHistoryService
from app.store import store


def _reset() -> ErrorRecoverService:
    """每个用例前清空重采计数与运行历史，保证计数相互独立。"""
    recolect = recollect_counts
    recolect.reset()
    for sub in ["runs", "reports"]:
        path = persist.data_path(sub, "")
        if os.path.isdir(path):
            for name in os.listdir(path):
                os.remove(os.path.join(path, name))
    store.rows("run_history").clear()
    return ErrorRecoverService()


def _history_row(task_id: int) -> dict:
    rows = [r for r in store.rows("run_history") if r.get("恢复任务ID") == task_id]
    assert len(rows) == 1, f"重采只应写一条运行历史，实际 {len(rows)} 条"
    return rows[0]


def _new_task(svc: ErrorRecoverService, code: str) -> int:
    entry, missing = svc.create_entry({
        "任务编号": code,
        "关联计划": f"PLAN-{code}",
        "失败步骤": "data_extract",
    })
    assert entry is not None, missing
    return entry["id"]


def test_attempts_are_not_failed_pages():
    """重试三次只应记三次尝试，未结算时失败页数未知，不能写成 3。"""
    svc = _reset()
    task_id = _new_task(svc, "REC-ATTEMPTS")

    for _ in range(3):
        result, _ = svc.run_action(task_id, "发起重试")
        assert result is not None

    record = svc.recollect_record(task_id)
    assert record["attempts"] == 3
    assert record["failed_pages"] is None, "未结算时失败页数不是已知数"
    assert record["success_pages"] == 0

    row = _history_row(task_id)
    assert row["尝试次数"] == 3
    assert row["失败页数"] is None
    assert row["失败行数"] is None


def test_receipts_drive_settled_counts():
    """先三次尝试，再一条失败页 + 两条成功页；结算后按回执得 1 失败 2 成功。"""
    svc = _reset()
    task_id = _new_task(svc, "REC-ORDER")

    svc.register_attempts(task_id, 3)
    svc.record_receipt(task_id, "page-1", False)
    svc.record_receipt(task_id, "page-2", True)
    svc.record_receipt(task_id, "page-3", True)

    record = svc.recollect_record(task_id)
    # 结算前失败页依旧未知，但成功页/已收页数可以照实累加。
    assert record["failed_pages"] is None
    assert record["success_pages"] == 2
    assert record["received_pages"] == 3

    row = _history_row(task_id)
    assert row["尝试次数"] == 3

    svc.run_action(task_id, "确认恢复")
    record = svc.recollect_record(task_id)
    assert record["settled"] is True
    assert record["collection_confirmed"] is True
    assert record["success_pages"] == 2
    assert record["failed_pages"] == 1
    assert record["status"] == "部分成功"

    row = _history_row(task_id)
    assert row["尝试次数"] == 3
    assert row["成功页数"] == 2
    assert row["失败页数"] == 1
    assert row["采集行数"] == 3       # 真实回执覆盖页数
    assert row["失败行数"] == 1
    assert row["采集数据确认"] == "已确认"
    assert row["关联恢复任务"] == "REC-ORDER"


def test_duplicate_and_late_receipts_not_double_counted():
    """同一页的迟到或重复回执不再累加。"""
    svc = _reset()
    task_id = _new_task(svc, "REC-DUP")

    svc.register_attempts(task_id, 1)
    svc.record_receipt(task_id, "page-1", False)
    svc.record_receipt(task_id, "page-2", True)
    svc.record_receipt(task_id, "page-2", True)   # 重复回执
    svc.record_receipt(task_id, "page-2", False)  # 迟到且结果矛盾的重复回执
    svc.record_receipt(task_id, "page-1", False)  # 重复失败回执

    record = svc.recollect_record(task_id)
    assert record["received_pages"] == 2
    assert record["success_pages"] == 1
    assert record["failed_pages"] is None  # 尚未结算

    svc.run_action(task_id, "确认恢复")
    record = svc.recollect_record(task_id)
    assert record["failed_pages"] == 1
    assert record["success_pages"] == 1


def test_receipts_arriving_before_attempts_are_folded_in():
    """回执比尝试登记更早到达：暂存回执并入唯一一条运行，不能残留成额外行。"""
    svc = _reset()
    task_id = _new_task(svc, "REC-EARLY")

    # 运行尚不存在，回执先到 -> 暂存
    svc.record_receipt(task_id, "page-1", False)
    svc.record_receipt(task_id, "page-2", True)
    assert recollect_counts.pending_receipts(task_id) == {"page-1": False, "page-2": True}
    assert svc.recollect_record(task_id) is None

    # 运行历史一侧登记尝试，暂存回执并入
    svc.register_attempts(task_id, 3)
    assert recollect_counts.pending_receipts(task_id) is None
    record = svc.recollect_record(task_id)
    assert record["attempts"] == 3
    assert record["received_pages"] == 2
    assert record["success_pages"] == 1

    # 确认恢复结算
    svc.run_action(task_id, "确认恢复")
    record = svc.recollect_record(task_id)
    assert record["failed_pages"] == 1
    assert record["success_pages"] == 1

    row = _history_row(task_id)
    assert row["尝试次数"] == 3
    assert row["失败页数"] == 1


def test_confirmation_before_receipts_keeps_counts_unknown():
    """确认先于回执：采集数据仍未确认，失败页数不是已知数，也不能残留暂存行数。"""
    svc = _reset()
    task_id = _new_task(svc, "REC-LATE")

    svc.register_attempts(task_id, 3)
    svc.run_action(task_id, "确认恢复")

    record = svc.recollect_record(task_id)
    assert record["settled"] is True
    assert record["collection_confirmed"] is False
    assert record["failed_pages"] is None
    assert record["success_pages"] == 0

    row = _history_row(task_id)
    assert row["尝试次数"] == 3
    assert row["失败页数"] is None
    assert row["采集行数"] is None
    assert row["采集数据确认"] == "未确认"
    assert row["status"] == "运行中"

    # 运行历史能查明该次运行的采集数据仍未确认
    history = RunHistoryService()
    history_id = row["id"]
    unconfirmed = history.unconfirmed_recollects()
    assert any(r["run_history_id"] == history_id for r in unconfirmed)
    assert history.recollect_counts(history_id)["failed_pages"] is None

    # 迟到的回执仍能结算到这次运行，且只算一次
    svc.record_receipt(task_id, "page-late", False)
    svc.record_receipt(task_id, "page-late", False)
    record = svc.recollect_record(task_id)
    assert record["failed_pages"] == 1
    assert record["collection_confirmed"] is True
    row = _history_row(task_id)
    assert row["失败页数"] == 1
    assert row["采集数据确认"] == "已确认"


def test_no_stale_placeholder_in_results():
    """最终结果里不能残留“恢复重采”之类占位字符串。"""
    svc = _reset()
    task_id = _new_task(svc, "REC-PLACEHOLDER")

    svc.register_attempts(task_id, 3)
    svc.record_receipt(task_id, "page-1", True)
    svc.run_action(task_id, "确认恢复")

    row = _history_row(task_id)
    for key in ("采集行数", "失败行数", "尝试次数", "成功页数", "失败页数"):
        assert row.get(key) != "恢复重采", f"{key} 仍是占位字符串"
    assert isinstance(row["采集行数"], int)
    assert isinstance(row["失败行数"], int)


def test_recovery_snapshot_separates_counts():
    """domain_snapshot 分开汇总尝试次数与失败页数。"""
    svc = _reset()
    task_id = _new_task(svc, "REC-SNAP")

    svc.register_attempts(task_id, 3)
    svc.record_receipt(task_id, "page-1", False)
    svc.record_receipt(task_id, "page-2", True)
    svc.record_receipt(task_id, "page-3", True)
    svc.run_action(task_id, "确认恢复")

    snap = svc.domain_snapshot()
    assert snap["recollect_attempts"] >= 3
    assert snap["recollect_failed_pages"] >= 1
    assert snap["recollect_unconfirmed"] == 0


if __name__ == "__main__":
    tests = [
        test_attempts_are_not_failed_pages,
        test_receipts_drive_settled_counts,
        test_duplicate_and_late_receipts_not_double_counted,
        test_receipts_arriving_before_attempts_are_folded_in,
        test_confirmation_before_receipts_keeps_counts_unknown,
        test_no_stale_placeholder_in_results,
        test_recovery_snapshot_separates_counts,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"\nALL {len(tests)} RECOLLECT COUNT TESTS PASSED")

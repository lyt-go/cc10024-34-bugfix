"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import (
    ActionPayload,
    ActionResult,
    AttemptPayload,
    EntryPayload,
    PageResult,
    ReceiptPayload,
)
from app.services import service_for

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    entry, message = service.run_action(entry_id, payload.action.strip())
    return ActionResult(ok=entry is not None, message=message, entry=entry)


@router.post("/error_recover/{entry_id}/attempts", response_model=ActionResult)
def register_attempts(entry_id: int, payload: AttemptPayload) -> ActionResult:
    """登记重采尝试次数：只记尝试，不写失败页数。"""
    from app.services.error_recover import ErrorRecoverService

    service = require_service("error_recover")
    assert isinstance(service, ErrorRecoverService)
    record = service.register_attempts(entry_id, payload.count)
    if record is None:
        return ActionResult(ok=False, message=f"恢复任务 {entry_id} 不存在或已归档")
    return ActionResult(ok=True, message="尝试次数已登记", entry=record)


@router.post("/error_recover/{entry_id}/receipts", response_model=ActionResult)
def record_receipt(entry_id: int, payload: ReceiptPayload) -> ActionResult:
    """登记采集页回执：成功/失败按页去重，最终统计只依据真实回执。"""
    from app.services.error_recover import ErrorRecoverService

    service = require_service("error_recover")
    assert isinstance(service, ErrorRecoverService)
    record = service.record_receipt(entry_id, payload.page_key, payload.success)
    if record is None:
        return ActionResult(ok=False, message=f"恢复任务 {entry_id} 不存在或已归档")
    return ActionResult(ok=True, message="采集回执已登记", entry=record)


@router.get("/run_history/{entry_id}/recollect")
def recollect_count(entry_id: int) -> dict[str, Any]:
    """运行历史详情：尝试次数与实际失败页数分开返回，未确认时失败页数为 null。"""
    from app.services.run_history import RunHistoryService

    service = require_service("run_history")
    assert isinstance(service, RunHistoryService)
    record = service.recollect_counts(entry_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"运行记录 {entry_id} 没有重采计数记录")
    return record

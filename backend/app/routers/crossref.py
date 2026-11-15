"""跨源数据关联接口：维护关联规则，覆盖启用关联、暂停关联、关闭关联等动作。

路由顺序注意：静态段（/export）必须注册在 /{entry_id} 之前，
否则「导出清单」会被当成规则 id 解析，导出永远 422。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.crossref import CrossrefService

router = APIRouter(prefix="/api/crossref", tags=["跨源数据关联"])

service = CrossrefService()

LIST_FIELDS = ["规则编号", "主数据源", "关联数据源", "关联字段", "关联类型", "结果字段", "时效控制", "规则状态"]
STATUSES = ["草稿", "已启用", "已暂停", "已关闭"]


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出跨源数据关联清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "crossref", "total": total, "items": items}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、已启用、已暂停、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤跨源数据关联列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条关联规则，缺字段或配置不合法时说明原因而不是静默丢弃。"""
    entry, errors = service.create_entry(payload.values)
    if errors:
        return ActionResult(ok=False, message="；".join(errors))
    return ActionResult(ok=True, message="关联规则已登记", entry=entry)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条关联规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"关联规则 {entry_id} 不存在或已归档")
    return entry


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """保存关联规则修改：关联字段、关联类型、结果字段、时效控制全部落库并回显。"""
    entry, errors = service.update_entry(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message="；".join(errors))
    return ActionResult(ok=True, message="关联规则已保存", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条关联规则执行启用关联、暂停关联、关闭关联；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/results")
def list_results(entry_id: int) -> dict[str, Any]:
    """当前跨源关联结果 + 历史时效结果：旧版本保留可追溯。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"关联规则 {entry_id} 不存在或已归档")
    current, history = service.list_results(entry_id)
    return {"规则编号": entry.get("规则编号"), "current": current, "history": history}


@router.get("/{entry_id}/export")
def export_result(entry_id: int) -> dict[str, Any]:
    """导出单条规则的跨源关联结果：冻结当前字段匹配快照，不混入其他规则。"""
    bundle, error = service.build_export(entry_id)
    if bundle is None:
        raise HTTPException(status_code=409, detail=error)
    return bundle


@router.get("/{entry_id}/export/download")
def download_result(entry_id: int) -> JSONResponse:
    """下载关联结果文件：打包当前字段匹配快照与结果行。"""
    bundle, error = service.build_export(entry_id)
    if bundle is None:
        raise HTTPException(status_code=409, detail=error)
    filename = f"crossref-{bundle['规则编号']}-v{bundle['结果版本']}.json"
    content = json.dumps(bundle, ensure_ascii=False, indent=2)
    return JSONResponse(
        content=json.loads(content),
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )

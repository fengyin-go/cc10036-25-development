"""语义字段映射版本化接口：修订版次、落图批次、血缘版次读取与报表回查。

所有路径固定在 /api/semantic-revisions 下，在 main.py 中先于动态资源路由注册。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.data_lineage import DataLineageService
from app.services.semantic_revision import semantic_revision_service

router = APIRouter(prefix="/api/semantic-revisions", tags=["语义映射版本化"])

_lineage_service = DataLineageService()


# ---- 请求模型 ----

class EdgeSpec(BaseModel):
    semantic_field: str
    physical_column: str
    source_node: str | None = None
    target_node: str = ""
    confirmed: bool = False


class DraftRevisionPayload(BaseModel):
    semantic_id: int
    semantic_name: str
    physical_table: str
    field_mapping: str
    change_summary: str
    created_by: str = "分析师"
    relations: str = ""
    filters: str = ""
    security: str = ""
    edges: list[EdgeSpec] = Field(default_factory=list)


class ReplaceColumnPayload(BaseModel):
    semantic_id: int
    old_physical_column: str
    new_physical_column: str
    new_semantic_field: str
    target_node: str = ""
    change_summary: str
    created_by: str = "分析师"


class ReportBindPayload(BaseModel):
    report_name: str
    semantic_id: int
    revision_id: int | None = None
    note: str = ""


def _ok(result: dict[str, Any]) -> dict[str, Any]:
    return result


# ---- 版次查询 ----

@router.get("")
def list_revisions(semantic_id: int | None = None) -> dict[str, Any]:
    """映射修订版次列表，可按语义定义过滤。"""
    return {"ok": True, "items": semantic_revision_service.list_snapshots(semantic_id)}


@router.get("/{revision_id}")
def get_revision(revision_id: int) -> dict[str, Any]:
    view = semantic_revision_service.get_snapshot(revision_id)
    if view is None:
        return {"ok": False, "message": f"版次 {revision_id} 不存在"}
    return {"ok": True, "item": view}


@router.get("/semantic/{semantic_id}/resolve")
def resolve_revision(semantic_id: int, version_tag: str | None = None) -> dict[str, Any]:
    """按版次标签解析映射；不传标签返回当前已发布版次。"""
    view = semantic_revision_service.resolve_revision(semantic_id, version_tag)
    if view is None:
        return {"ok": False, "message": "未找到对应版次"}
    return {"ok": True, "item": view}


# ---- 建版 / 替换列 ----

@router.post("/draft")
def create_draft(payload: DraftRevisionPayload) -> dict[str, Any]:
    view = semantic_revision_service.draft_revision(
        semantic_id=payload.semantic_id,
        semantic_name=payload.semantic_name,
        physical_table=payload.physical_table,
        field_mapping=payload.field_mapping,
        change_summary=payload.change_summary,
        created_by=payload.created_by,
        relations=payload.relations,
        filters=payload.filters,
        security=payload.security,
        edges=[edge.model_dump() for edge in payload.edges],
    )
    return {"ok": True, "item": view, "message": "新映射版次已创建（草稿，未写入已发布关系）"}


@router.post("/replace-column")
def replace_column(payload: ReplaceColumnPayload) -> dict[str, Any]:
    """为销售额替换一列：生成下一版次，新列血缘边默认未确认。"""
    try:
        view = semantic_revision_service.replace_mapping_column(
            semantic_id=payload.semantic_id,
            old_physical_column=payload.old_physical_column,
            new_physical_column=payload.new_physical_column,
            new_semantic_field=payload.new_semantic_field,
            target_node=payload.target_node,
            change_summary=payload.change_summary,
            created_by=payload.created_by,
        )
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "item": view, "message": "列替换已生成新版次，新血缘边待确认"}


# ---- 落图批次 ----

@router.get("/landing/status")
def landing_status() -> dict[str, Any]:
    return _ok(semantic_revision_service.landing_status())


@router.post("/landing/pause")
def pause_landing() -> dict[str, Any]:
    return semantic_revision_service.pause_landing()


@router.post("/landing/resume")
def resume_landing() -> dict[str, Any]:
    return semantic_revision_service.resume_landing()


@router.post("/{revision_id}/landing")
def start_landing(revision_id: int) -> dict[str, Any]:
    return semantic_revision_service.start_landing(revision_id)


@router.post("/landing/{batch_id}/retry")
def retry_landing(batch_id: int) -> dict[str, Any]:
    return semantic_revision_service.retry_landing(batch_id)


# ---- 边确认 / 发布 ----

@router.post("/edges/{edge_id}/confirm")
def confirm_edge(edge_id: int) -> dict[str, Any]:
    return semantic_revision_service.confirm_edge(edge_id)


@router.post("/{revision_id}/publish")
def publish_revision(revision_id: int) -> dict[str, Any]:
    return semantic_revision_service.publish_revision(revision_id)


@router.get("/{revision_id}/readiness")
def landing_readiness(revision_id: int) -> dict[str, Any]:
    return _lineage_service.landing_readiness(revision_id)


# ---- 血缘版次读取 ----

@router.get("/lineage/by-version/{semantic_id}")
def lineage_by_version(semantic_id: int, version_tag: str | None = None) -> dict[str, Any]:
    """血缘接口按版次读取：旧版次回查 / 当前发布版读取。"""
    return _lineage_service.read_lineage_version(semantic_id, version_tag)


@router.get("/lineage/report/{report_name}")
def report_field_chain(report_name: str) -> dict[str, Any]:
    """查询旧报表的字段链：按报表固化引用回查对应版次血缘。"""
    return _lineage_service.read_report_field_chain(report_name)


# ---- 报表版次引用 ----

@router.post("/reports/bind")
def bind_report(payload: ReportBindPayload) -> dict[str, Any]:
    return semantic_revision_service.bind_report(
        report_name=payload.report_name,
        semantic_id=payload.semantic_id,
        revision_id=payload.revision_id,
        note=payload.note,
    )


@router.get("/reports/refs")
def list_report_refs() -> dict[str, Any]:
    return semantic_revision_service.list_report_refs()

"""语义映射修订服务：版次生命周期、落图批次、确认/发布与报表版次绑定。

关键不变量：
- 落图批次只负责重试执行，不会替未确认的边放行；确认是人工动作。
- 全局暂停（landing_paused）期间不允许新批次落图，已发布血缘保持可读。
- 版次所有血缘边确认后才能发布；发布前新字段不会出现在已发布血缘关系里。
- 发布时同语义的旧已发布版次转为「已替代」，但报表引用仍指向旧版次，可回查。
"""
from __future__ import annotations

from typing import Any

from app.mapping_store import (
    BATCH_BLOCKED,
    BATCH_DONE,
    BATCH_INTERRUPTED,
    BATCH_PENDING,
    BATCH_RUNNING,
    EDGE_CONFIRMED,
    REV_CONFIRMED,
    REV_DRAFT,
    REV_PENDING,
    REV_PUBLISHED,
    REV_SUPERSEDED,
    LandingBatch,
    MappingRevision,
    mapping_store,
    now_text,
)


class SemanticRevisionService:
    """映射修订版次的领域服务（不继承通用 CRUD 骨架，走独立版本化路由）。"""

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_snapshots(self, semantic_id: int | None = None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for revision in mapping_store.list_revisions(semantic_id):
            result.append(self._revision_view(revision))
        return result

    def get_snapshot(self, revision_id: int) -> dict[str, Any] | None:
        revision = mapping_store.get_revision(revision_id)
        if revision is None:
            return None
        return self._revision_view(revision)

    def resolve_revision(
        self, semantic_id: int, version_tag: str | None = None,
    ) -> dict[str, Any] | None:
        """按语义 + 版次标签解析；不传标签时返回当前已发布版次。"""
        revision: MappingRevision | None
        if version_tag:
            revision = mapping_store.get_revision_by_tag(semantic_id, version_tag)
        else:
            revision = mapping_store.published_revision(semantic_id)
        if revision is None:
            return None
        return self._revision_view(revision)

    # ------------------------------------------------------------------
    # 建版 / 替换字段
    # ------------------------------------------------------------------
    def draft_revision(
        self, *, semantic_id: int, semantic_name: str, physical_table: str,
        field_mapping: str, change_summary: str, created_by: str,
        relations: str = "", filters: str = "", security: str = "",
        edges: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """基于修改后的字段映射生成新版次（草稿），旧版次保持不动。"""
        revision = mapping_store.add_revision(
            semantic_id=semantic_id, semantic_name=semantic_name,
            physical_table=physical_table, field_mapping=field_mapping,
            change_summary=change_summary, created_by=created_by,
            relations=relations, filters=filters, security=security,
            status=REV_DRAFT,
        )
        for edge in edges or []:
            mapping_store.add_edge(
                revision_id=revision.id,
                semantic_field=str(edge.get("semantic_field", "")),
                physical_column=str(edge.get("physical_column", "")),
                source_node=str(edge.get("source_node", physical_table)),
                target_node=str(edge.get("target_node", "")),
                confirmed=bool(edge.get("confirmed", False)),
            )
        return self._revision_view(revision)

    def replace_mapping_column(
        self, *, semantic_id: int, old_physical_column: str, new_physical_column: str,
        new_semantic_field: str, target_node: str, change_summary: str, created_by: str,
    ) -> dict[str, Any]:
        """「为销售额替换一列」：在最新版次快照上替换一列，生成下一版次。

        新列对应血缘边默认未确认，其余沿用边继承确认状态。
        """
        history = mapping_store.list_revisions(semantic_id)
        if not history:
            raise ValueError(f"语义定义 {semantic_id} 尚无映射版次")
        base = history[-1]
        mapping_parts = [part.strip() for part in base.field_mapping.split(",") if part.strip()]
        replaced = False
        new_parts: list[str] = []
        for part in mapping_parts:
            column = part.split(":", 1)[0].strip()
            if column == old_physical_column:
                new_parts.append(f"{new_physical_column}:{new_semantic_field}")
                replaced = True
            elif column == new_physical_column:
                # 基础版次已登记过新列（例如待确认版次里先加过），去重避免重复映射项
                continue
            else:
                new_parts.append(part)
        if not replaced:
            raise ValueError(f"版次 {base.version_tag} 中未找到物理列 {old_physical_column}")
        # 新列若已在映射中（替换后产生重复项），去重保序
        deduped: list[str] = []
        seen_columns: set[str] = set()
        for part in new_parts:
            column = part.split(":", 1)[0].strip()
            if column in seen_columns:
                continue
            seen_columns.add(column)
            deduped.append(part)

        revision = mapping_store.add_revision(
            semantic_id=base.semantic_id, semantic_name=base.semantic_name,
            physical_table=base.physical_table, field_mapping=", ".join(deduped),
            change_summary=change_summary, created_by=created_by,
            relations=base.relations, filters=base.filters, security=base.security,
            status=REV_PENDING,
        )
        # 沿用边继承；旧列边替换为一条未确认新边；若基础版次已存在同列新边则合并，不重复登记
        emitted_columns: set[str] = set()
        for edge in mapping_store.edges_for(base.id):
            if edge.physical_column == old_physical_column:
                if new_physical_column in emitted_columns:
                    continue
                mapping_store.add_edge(
                    revision_id=revision.id, semantic_field=new_semantic_field,
                    physical_column=new_physical_column,
                    source_node=edge.source_node, target_node=target_node or edge.target_node,
                    confirmed=False,
                )
                emitted_columns.add(new_physical_column)
                continue
            if edge.physical_column in emitted_columns:
                continue
            mapping_store.add_edge(
                revision_id=revision.id, semantic_field=edge.semantic_field,
                physical_column=edge.physical_column,
                source_node=edge.source_node, target_node=edge.target_node,
                confirmed=edge.confirmed,
            )
            emitted_columns.add(edge.physical_column)
        return self._revision_view(revision)

    # ------------------------------------------------------------------
    # 落图批次（暂停 / 中断 / 重试，幂等）
    # ------------------------------------------------------------------
    def pause_landing(self) -> dict[str, Any]:
        mapping_store.landing_paused = True
        return {"ok": True, "landing_paused": True, "message": "新批次落图已暂停，已发布血缘仍可读取"}

    def resume_landing(self) -> dict[str, Any]:
        mapping_store.landing_paused = False
        return {"ok": True, "landing_paused": False, "message": "落图已恢复，可重试中断批次"}

    def landing_status(self) -> dict[str, Any]:
        return {
            "landing_paused": mapping_store.landing_paused,
            "batches": [b.to_dict() for b in mapping_store.list_batches()],
        }

    def start_landing(self, revision_id: int) -> dict[str, Any]:
        """为版次创建落图批次；暂停期间拒绝新批次。"""
        revision = mapping_store.get_revision(revision_id)
        if revision is None:
            return {"ok": False, "message": f"版次 {revision_id} 不存在"}
        if revision.status == REV_PUBLISHED:
            return {"ok": False, "message": "版次已发布，无需重复落图"}
        if mapping_store.landing_paused:
            batch = mapping_store.add_batch(
                revision_id, status=BATCH_BLOCKED,
                message="落图暂停中：批次挂起，未写入任何新血缘关系",
            )
            return {
                "ok": False, "blocked": True, "batch": batch.to_dict(),
                "message": "落图已暂停：未确认版次的新字段不会写入已发布关系",
            }
        batch = mapping_store.add_batch(revision_id, status=BATCH_PENDING)
        return {"ok": True, "batch": batch.to_dict(), "message": "落图批次已创建，等待执行"}

    def retry_landing(self, batch_id: int) -> dict[str, Any]:
        """中断后重试：幂等推进批次，只登记已确认边，未确认边保持未写入。"""
        batch = mapping_store.get_batch(batch_id)
        if batch is None:
            return {"ok": False, "message": f"批次 {batch_id} 不存在"}
        if batch.status == BATCH_DONE:
            return {"ok": True, "idempotent": True, "batch": batch.to_dict(),
                    "message": "批次已完成，重试为空操作（幂等）"}
        if mapping_store.landing_paused:
            return {"ok": False, "blocked": True, "batch": batch.to_dict(),
                    "message": "落图仍处暂停状态，重试被拒绝"}

        revision = mapping_store.get_revision(batch.revision_id)
        if revision is None:
            return {"ok": False, "message": "批次关联的版次已丢失"}

        batch.attempt += 1
        batch.last_attempt_at = now_text()
        batch.status = BATCH_RUNNING
        edges = mapping_store.edges_for(revision.id)
        confirmed = [e for e in edges if e.confirmed]
        pending = [e for e in edges if not e.confirmed]
        # 已确认边幂等写入；未确认边不进入已发布关系，批次保持中断，等待人工确认
        if pending:
            batch.status = BATCH_INTERRUPTED
            batch.message = (
                f"{len(confirmed)} 条已确认边已登记，{len(pending)} 条血缘边尚未确认，"
                "新字段未写入已发布关系"
            )
            return {
                "ok": False, "interrupted": True, "batch": batch.to_dict(),
                "confirmed_edges": [e.to_dict() for e in confirmed],
                "pending_edges": [e.to_dict() for e in pending],
                "message": batch.message,
            }

        batch.status = BATCH_DONE
        batch.finished_at = now_text()
        batch.message = "全部血缘边已确认并写入版次血缘"
        if revision.status in (REV_DRAFT, REV_PENDING):
            revision.status = REV_CONFIRMED
        return {
            "ok": True, "batch": batch.to_dict(),
            "confirmed_edges": [e.to_dict() for e in confirmed],
            "pending_edges": [],
            "message": "落图完成，版次进入「已确认」，可执行发布",
        }

    # ------------------------------------------------------------------
    # 边确认 / 版次发布
    # ------------------------------------------------------------------
    def confirm_edge(self, edge_id: int, *, force: bool = False) -> dict[str, Any]:
        edge = mapping_store.get_edge(edge_id)
        if edge is None:
            return {"ok": False, "message": f"血缘边 {edge_id} 不存在"}
        revision = mapping_store.get_revision(edge.revision_id)
        if revision is None:
            return {"ok": False, "message": "边关联的版次不存在"}
        if revision.status == REV_PUBLISHED and not force:
            return {"ok": False, "message": "版次已发布，边已随版次固化"}
        if edge.confirmed:
            return {"ok": True, "idempotent": True, "edge": edge.to_dict(),
                    "message": "该血缘边此前已确认（幂等）"}
        edge.confirmed = True
        edge.confirm_time = now_text()
        remaining = mapping_store.unconfirmed_edges(edge.revision_id)
        if not remaining and revision.status in (REV_DRAFT, REV_PENDING):
            revision.status = REV_CONFIRMED
        return {
            "ok": True, "edge": edge.to_dict(),
            "unconfirmed_remaining": len(remaining),
            "revision_status": revision.status,
            "message": "血缘边已确认" if remaining else "全部血缘边已确认，版次可发布",
        }

    def publish_revision(self, revision_id: int) -> dict[str, Any]:
        """确认版次后发布：未确认边存在时拒绝，新字段不会提前写入已发布关系。"""
        revision = mapping_store.get_revision(revision_id)
        if revision is None:
            return {"ok": False, "message": f"版次 {revision_id} 不存在"}
        if revision.status == REV_PUBLISHED:
            return {"ok": False, "message": "该版次已是当前发布版次"}
        pending = mapping_store.unconfirmed_edges(revision_id)
        if pending:
            return {
                "ok": False, "blocked": True,
                "pending_edges": [e.to_dict() for e in pending],
                "message": f"尚有 {len(pending)} 条血缘边未确认，禁止发布，新字段未写入已发布关系",
            }
        # 同语义旧发布版次转「已替代」，历史保留
        previous = mapping_store.published_revision(revision.semantic_id)
        if previous is not None:
            previous.status = REV_SUPERSEDED
        revision.status = REV_PUBLISHED
        revision.published_at = now_text()
        return {
            "ok": True,
            "revision": self._revision_view(revision),
            "superseded_revision_id": previous.id if previous else None,
            "message": f"版次 {revision.version_tag} 已发布，字段血缘关系已切换",
        }

    # ------------------------------------------------------------------
    # 报表版次绑定与回查
    # ------------------------------------------------------------------
    def bind_report(
        self, *, report_name: str, semantic_id: int,
        revision_id: int | None = None, note: str = "",
    ) -> dict[str, Any]:
        """报表固化版次引用；不传 revision_id 时绑定当前已发布版次。"""
        revision: MappingRevision | None
        if revision_id is None:
            revision = mapping_store.published_revision(semantic_id)
            if revision is None:
                return {"ok": False, "message": f"语义 {semantic_id} 没有已发布版次可绑定"}
            revision_id = revision.id
        else:
            revision = mapping_store.get_revision(revision_id)
            if revision is None:
                return {"ok": False, "message": f"版次 {revision_id} 不存在"}
        ref = mapping_store.add_report_ref(
            report_name=report_name, semantic_id=semantic_id,
            revision_id=revision_id, note=note,
        )
        return {"ok": True, "report_ref": ref.to_dict(),
                "message": f"报表已固化引用 {revision.version_tag}，后续刷新按该版次回查"}

    def resolve_report_lineage(self, report_name: str) -> dict[str, Any]:
        """旧报表刷新：按报表固化的版次引用回查字段链，不随新发布漂移。"""
        refs = mapping_store.refs_for_report(report_name)
        if not refs:
            return {"ok": False, "message": f"报表 {report_name} 没有版次引用"}
        resolved: list[dict[str, Any]] = []
        for ref in refs:
            revision = mapping_store.get_revision(ref.revision_id)
            if revision is None:
                continue
            edges = mapping_store.edges_for(revision.id)
            resolved.append({
                "report_ref": ref.to_dict(),
                "revision": revision.to_dict(),
                "field_lineage": [e.to_dict() for e in edges],
                "uses_current_published": revision.status == REV_PUBLISHED,
            })
        return {"ok": True, "report_name": report_name, "bindings": resolved}

    def list_report_refs(self) -> dict[str, Any]:
        return {"ok": True, "items": [ref.to_dict() for ref in mapping_store.list_report_refs()]}

    # ------------------------------------------------------------------
    # 组装视图
    # ------------------------------------------------------------------
    def _revision_view(self, revision: MappingRevision) -> dict[str, Any]:
        edges = mapping_store.edges_for(revision.id)
        confirmed = [e for e in edges if e.confirmed]
        pending = [e for e in edges if not e.confirmed]
        batches = mapping_store.list_batches(revision.id)
        return {
            **revision.to_dict(),
            "edge_total": len(edges),
            "edge_confirmed": len(confirmed),
            "edge_unconfirmed": len(pending),
            "edges": [e.to_dict() for e in edges],
            "batches": [b.to_dict() for b in batches],
            "publishable": not pending and revision.status != REV_PUBLISHED,
        }


semantic_revision_service = SemanticRevisionService()

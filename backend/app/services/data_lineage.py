"""数据血缘领域服务：图结构计算、影响分析、上下游追踪。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store
from app.analytics_engine import LineageGraph, LineageNode


class DataLineageService(BaseModuleService):
    MODULE = "data_lineage"
    ENTITY = "血缘节点"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["节点名称", "节点类型", "上游列表", "下游列表", "数据量级", "更新频率", "负责人"]
    REQUIRED_FIELDS = ["节点名称", "节点类型", "上游列表"]
    STATUS_ORDER = ["在线", "已下线", "已停用"]
    ACTION_RULES = {"标记下线": "已下线", "停用节点": "已停用"}
    NEGATIVE_ACTIONS = ["停用节点"]

    # ---- 业务方法 ----

    def build_graph_from_store(self) -> LineageGraph:
        """从Store中构建血缘图。"""
        graph = LineageGraph()
        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("status") != "在线":
                continue
            node_id = str(row.get("id", ""))
            upstream_raw = str(row.get("上游列表", ""))
            downstream_raw = str(row.get("下游列表", ""))
            node = LineageNode(
                node_id=node_id,
                node_name=str(row.get("节点名称", "")),
                node_type=str(row.get("节点类型", "")),
                upstream_ids=[u.strip() for u in upstream_raw.split(",") if u.strip()],
                downstream_ids=[d.strip() for d in downstream_raw.split(",") if d.strip()],
            )
            graph.add_node(node)
        return graph

    def compute_upstream(self, node_id: str) -> dict[str, Any]:
        """计算上游血缘链。"""
        graph = self.build_graph_from_store()
        entry = store.find(self.MODULE, int(node_id))
        if entry is None:
            return {"ok": False, "message": f"节点 {node_id} 不存在"}
        chains = graph.upstream_chain(node_id)
        return {
            "ok": True,
            "node_name": str(entry.get("节点名称", "")),
            "node_type": str(entry.get("节点类型", "")),
            "upstream_chains": chains,
            "chain_count": len(chains),
            "max_depth": max((len(c) for c in chains), default=0),
        }

    def compute_downstream(self, node_id: str) -> dict[str, Any]:
        """计算下游影响链。"""
        graph = self.build_graph_from_store()
        entry = store.find(self.MODULE, int(node_id))
        if entry is None:
            return {"ok": False, "message": f"节点 {node_id} 不存在"}
        chains = graph.downstream_chain(node_id)
        return {
            "ok": True,
            "node_name": str(entry.get("节点名称", "")),
            "node_type": str(entry.get("节点类型", "")),
            "downstream_chains": chains,
            "chain_count": len(chains),
            "max_depth": max((len(c) for c in chains), default=0),
        }

    def impact_analysis(self, node_id: str) -> dict[str, Any]:
        """变更影响分析：找出所有下游节点。"""
        graph = self.build_graph_from_store()
        entry = store.find(self.MODULE, int(node_id))
        if entry is None:
            return {"ok": False, "message": f"节点 {node_id} 不存在"}
        impacted = graph.impact_analysis(node_id)
        return {
            "ok": True,
            "node_name": str(entry.get("节点名称", "")),
            "impacted_node_ids": list(impacted),
            "impacted_count": len(impacted),
            "severity": "高" if len(impacted) > 5 else "中" if len(impacted) > 2 else "低",
        }

    def get_online_nodes(self) -> list[dict[str, Any]]:
        """获取所有在线节点。"""
        return [
            row for row in store.rows(self.MODULE)
            if row.get("status") == "在线"
        ]

    def export_lineage_graph(self) -> dict[str, Any]:
        """导出完整血缘图结构（供数据范围消费）。"""
        graph = self.build_graph_from_store()
        adj = graph.to_adjacency()
        return {
            "ok": True,
            "node_count": len(adj["nodes"]),
            **adj,
        }

    # ---- 版本化字段血缘（消费语义层映射修订数据） ----

    def build_versioned_field_graph(self, revision_id: int) -> LineageGraph:
        """按映射修订版次构建字段级血缘图。

        仅纳入已确认血缘边；未确认边不会进入图（确认版次之前不把新字段写入关系）。
        """
        from app.mapping_store import mapping_store

        graph = LineageGraph()
        revision = mapping_store.get_revision(revision_id)
        if revision is None:
            return graph
        adjacency: dict[str, dict[str, Any]] = {}
        for edge in mapping_store.edges_for(revision_id):
            if not edge.confirmed:
                continue
            source = edge.source_node
            target = edge.target_node
            adjacency.setdefault(source, {"upstream": [], "downstream": set()})
            adjacency.setdefault(target, {"upstream": set(), "downstream": set()})
            adjacency[source]["downstream"].add(target)
            adjacency[target]["upstream"].add(source)
        for node_id, links in adjacency.items():
            graph.add_node(LineageNode(
                node_id=node_id, node_name=node_id, node_type="field-lineage",
                upstream_ids=sorted(links["upstream"]),
                downstream_ids=sorted(links["downstream"]),
            ))
        return graph

    def read_lineage_version(
        self, semantic_id: int, version_tag: str | None = None,
    ) -> dict[str, Any]:
        """血缘接口读取版次数据：不传版次读当前已发布，传版次读指定历史版次。

        返回明确的 readable 标记与未确认边清单，便于调用方核对「究竟能读取哪一版」。
        """
        from app.mapping_store import REV_PUBLISHED, mapping_store

        revision = (
            mapping_store.get_revision_by_tag(semantic_id, version_tag)
            if version_tag else mapping_store.published_revision(semantic_id)
        )
        if revision is None:
            return {
                "ok": False, "readable": False,
                "message": f"语义 {semantic_id} 不存在版次 {version_tag or '已发布'} 的血缘数据",
            }
        edges = mapping_store.edges_for(revision.id)
        confirmed = [e.to_dict() for e in edges if e.confirmed]
        unconfirmed = [e.to_dict() for e in edges if not e.confirmed]
        graph = self.build_versioned_field_graph(revision.id)
        return {
            "ok": True,
            "readable": True,
            "semantic_id": semantic_id,
            "revision": revision.to_dict(),
            "is_published": revision.status == REV_PUBLISHED,
            "confirmed_edges": confirmed,
            "unconfirmed_edges": unconfirmed,
            # 血缘图只含已确认边，未确认边被隔离
            "graph": graph.to_adjacency(),
            "message": f"当前可读取 {revision.version_tag} 版血缘（{len(confirmed)} 条已确认边）",
        }

    def read_report_field_chain(self, report_name: str) -> dict[str, Any]:
        """报表字段链回查：沿报表固化的版次引用追溯，旧报表读到旧版血缘。"""
        from app.services.semantic_revision import semantic_revision_service

        lineage = semantic_revision_service.resolve_report_lineage(report_name)
        if not lineage.get("ok"):
            return lineage
        chains: list[dict[str, Any]] = []
        for binding in lineage["bindings"]:
            revision_id = binding["report_ref"]["revision_id"]
            graph = self.build_versioned_field_graph(revision_id)
            for edge in binding["field_lineage"]:
                if not edge["confirmed"]:
                    continue
                # 字段链：语义字段 → 物理列 → 源节点 → 目标节点
                upstream = graph.upstream_chain(edge["target_node"])
                chains.append({
                    "report_name": report_name,
                    "version_tag": binding["report_ref"]["version_tag"],
                    "semantic_field": edge["semantic_field"],
                    "physical_column": edge["physical_column"],
                    "field_chain": f'{edge["target_node"]} ← {edge["source_node"]}.{edge["physical_column"]}（语义字段：{edge["semantic_field"]}）',
                    "upstream_paths": upstream,
                    "uses_current_published": binding["uses_current_published"],
                })
        return {
            "ok": True,
            "report_name": report_name,
            "readable_version": sorted({c["version_tag"] for c in chains}),
            "field_chains": chains,
        }

    def landing_readiness(self, revision_id: int) -> dict[str, Any]:
        """核对版次落图就绪度：未确认边存在时血缘接口只能读到上一发布版。"""
        from app.mapping_store import mapping_store

        revision = mapping_store.get_revision(revision_id)
        if revision is None:
            return {"ok": False, "message": f"版次 {revision_id} 不存在"}
        unconfirmed = [e.to_dict() for e in mapping_store.unconfirmed_edges(revision_id)]
        published = mapping_store.published_revision(revision.semantic_id)
        return {
            "ok": True,
            "revision": revision.to_dict(),
            "unconfirmed_edges": unconfirmed,
            "ready_to_publish": not unconfirmed,
            "currently_readable_published": (
                published.to_dict() if published else None
            ),
            "message": (
                "全部边已确认，可发布切换"
                if not unconfirmed
                else f"{len(unconfirmed)} 条边未确认，血缘读取仍停留在 {published.version_tag if published else '无'}"
            ),
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["artifact"] = "数据血缘数据集"
        online = sum(1 for row in store.rows(self.MODULE) if row.get("status") == "在线")
        base["online_nodes"] = online
        from app.mapping_store import mapping_store
        base["mapping_revisions"] = len(mapping_store.revisions)
        base["unconfirmed_lineage_edges"] = sum(
            1 for edge in mapping_store.edges.values() if not edge.confirmed
        )
        base["landing_paused"] = mapping_store.landing_paused
        return base
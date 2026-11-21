"""语义字段映射的版本化存储。

设计目标（对应映射修订需求）：
1. 语义字段修改不再原地覆盖，每次修改生成一条不可变的映射修订版次（revision），可回查。
2. 每个版次携带自己的字段级血缘边；边有 confirmed/unconfirmed 状态，未确认边不会进入
   已发布血缘关系。
3. 报表通过 report_mapping_refs 固化「生成时使用的版次引用」，旧报表刷新后仍按原引用回查。
4. 落图（把血缘边写入血缘图）以批次执行，支持中断后幂等重试；全局暂停开关生效时新批次
   不允许落图。
5. 只有全部血缘边确认后，版次才能 promote 为已发布；发布前新字段不会写入已发布关系。

仅依赖标准库，进程内单例，与 app.store.Store 的内存仓库定位一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ---- 版次 / 边 / 批次状态常量 ----

REV_DRAFT = "草稿"
REV_PENDING = "待确认"        # 存在未确认血缘边，尚未发布
REV_CONFIRMED = "已确认"      # 全部边确认，等待发布
REV_PUBLISHED = "已发布"      # 当前生效版次，其已确认边进入已发布血缘
REV_SUPERSEDED = "已替代"     # 被更新版次替代，但保留可回查

BATCH_PENDING = "待执行"
BATCH_RUNNING = "执行中"
BATCH_INTERRUPTED = "已中断"
BATCH_DONE = "已完成"
BATCH_BLOCKED = "已暂停"

EDGE_UNCONFIRMED = "unconfirmed"
EDGE_CONFIRMED = "confirmed"


def now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@dataclass
class FieldLineageEdge:
    """字段级血缘边：版次内实际使用的字段关系。"""
    id: int
    revision_id: int
    semantic_field: str          # 语义字段（业务名）
    physical_column: str         # 物理列
    source_node: str             # 上游血缘节点（物理表/节点名称）
    target_node: str             # 下游血缘节点（立方/报表节点名称）
    confirmed: bool = False
    confirm_time: str | None = None
    created_at: str = field(default_factory=now_text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "revision_id": self.revision_id,
            "semantic_field": self.semantic_field,
            "physical_column": self.physical_column,
            "source_node": self.source_node,
            "target_node": self.target_node,
            "status": EDGE_CONFIRMED if self.confirmed else EDGE_UNCONFIRMED,
            "confirmed": self.confirmed,
            "confirm_time": self.confirm_time,
            "created_at": self.created_at,
        }


@dataclass
class MappingRevision:
    """语义映射修订版次：字段映射的不可变快照。"""
    id: int
    semantic_id: int
    semantic_name: str
    physical_table: str
    revision_no: int
    version_tag: str
    status: str
    change_summary: str
    field_mapping: str
    relations: str
    filters: str
    security: str
    created_by: str
    created_at: str = field(default_factory=now_text)
    published_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "semantic_id": self.semantic_id,
            "semantic_name": self.semantic_name,
            "physical_table": self.physical_table,
            "revision_no": self.revision_no,
            "version_tag": self.version_tag,
            "status": self.status,
            "change_summary": self.change_summary,
            "field_mapping": self.field_mapping,
            "relations": self.relations,
            "filters": self.filters,
            "security": self.security,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "published_at": self.published_at,
        }


@dataclass
class ReportMappingRef:
    """报表对映射版次的固化引用：旧报表按原引用回查，不随后续发布漂移。"""
    id: int
    report_name: str
    semantic_id: int
    revision_id: int
    version_tag: str
    bound_at: str = field(default_factory=now_text)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "report_name": self.report_name,
            "semantic_id": self.semantic_id,
            "revision_id": self.revision_id,
            "version_tag": self.version_tag,
            "bound_at": self.bound_at,
            "note": self.note,
        }


@dataclass
class LandingBatch:
    """映射落图批次：把版次血缘边写入血缘图的可重试执行单元。"""
    id: int
    revision_id: int
    status: str
    attempt: int = 0
    created_at: str = field(default_factory=now_text)
    last_attempt_at: str | None = None
    finished_at: str | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "revision_id": self.revision_id,
            "status": self.status,
            "attempt": self.attempt,
            "created_at": self.created_at,
            "last_attempt_at": self.last_attempt_at,
            "finished_at": self.finished_at,
            "message": self.message,
        }


class MappingVersionStore:
    """映射修订版本仓库（内存实现，接口形状贴近真实 DB 访问层）。"""

    def __init__(self) -> None:
        self.revisions: dict[int, MappingRevision] = {}
        self.edges: dict[int, FieldLineageEdge] = {}
        self.report_refs: dict[int, ReportMappingRef] = {}
        self.batches: dict[int, LandingBatch] = {}
        # 全局落图开关：暂停期间新批次不得落图（已发布数据不受影响）
        self.landing_paused: bool = False
        self._next_revision_id = 1
        self._next_edge_id = 1
        self._next_ref_id = 1
        self._next_batch_id = 1
        self._seed()

    # ------------------------------------------------------------------
    # 版次
    # ------------------------------------------------------------------
    def add_revision(
        self, *, semantic_id: int, semantic_name: str, physical_table: str,
        field_mapping: str, change_summary: str, created_by: str,
        relations: str = "", filters: str = "", security: str = "",
        status: str = REV_DRAFT,
    ) -> MappingRevision:
        existing = [r for r in self.revisions.values() if r.semantic_id == semantic_id]
        revision_no = max((r.revision_no for r in existing), default=0) + 1
        revision = MappingRevision(
            id=self._next_revision_id,
            semantic_id=semantic_id,
            semantic_name=semantic_name,
            physical_table=physical_table,
            revision_no=revision_no,
            version_tag=f"v{revision_no}",
            status=status,
            change_summary=change_summary,
            field_mapping=field_mapping,
            relations=relations,
            filters=filters,
            security=security,
            created_by=created_by,
            published_at=now_text() if status == REV_PUBLISHED else None,
        )
        self._next_revision_id += 1
        self.revisions[revision.id] = revision
        return revision

    def list_revisions(self, semantic_id: int | None = None) -> list[MappingRevision]:
        rows = sorted(self.revisions.values(), key=lambda r: (r.semantic_id, r.revision_no))
        if semantic_id is not None:
            rows = [r for r in rows if r.semantic_id == semantic_id]
        return rows

    def get_revision(self, revision_id: int) -> MappingRevision | None:
        return self.revisions.get(revision_id)

    def get_revision_by_tag(self, semantic_id: int, version_tag: str) -> MappingRevision | None:
        for revision in self.revisions.values():
            if revision.semantic_id == semantic_id and revision.version_tag == version_tag:
                return revision
        return None

    def published_revision(self, semantic_id: int) -> MappingRevision | None:
        for revision in self.revisions.values():
            if revision.semantic_id == semantic_id and revision.status == REV_PUBLISHED:
                return revision
        return None

    # ------------------------------------------------------------------
    # 血缘边
    # ------------------------------------------------------------------
    def add_edge(
        self, *, revision_id: int, semantic_field: str, physical_column: str,
        source_node: str, target_node: str, confirmed: bool = False,
    ) -> FieldLineageEdge:
        edge = FieldLineageEdge(
            id=self._next_edge_id,
            revision_id=revision_id,
            semantic_field=semantic_field,
            physical_column=physical_column,
            source_node=source_node,
            target_node=target_node,
            confirmed=confirmed,
            confirm_time=now_text() if confirmed else None,
        )
        self._next_edge_id += 1
        self.edges[edge.id] = edge
        return edge

    def edges_for(self, revision_id: int) -> list[FieldLineageEdge]:
        return sorted(
            (e for e in self.edges.values() if e.revision_id == revision_id),
            key=lambda e: e.id,
        )

    def get_edge(self, edge_id: int) -> FieldLineageEdge | None:
        return self.edges.get(edge_id)

    def unconfirmed_edges(self, revision_id: int) -> list[FieldLineageEdge]:
        return [e for e in self.edges_for(revision_id) if not e.confirmed]

    # ------------------------------------------------------------------
    # 报表版次引用
    # ------------------------------------------------------------------
    def add_report_ref(
        self, *, report_name: str, semantic_id: int, revision_id: int,
        note: str = "",
    ) -> ReportMappingRef:
        revision = self.get_revision(revision_id)
        ref = ReportMappingRef(
            id=self._next_ref_id,
            report_name=report_name,
            semantic_id=semantic_id,
            revision_id=revision_id,
            version_tag=revision.version_tag if revision else "未知版次",
            note=note,
        )
        self._next_ref_id += 1
        self.report_refs[ref.id] = ref
        return ref

    def refs_for_report(self, report_name: str) -> list[ReportMappingRef]:
        return sorted(
            (r for r in self.report_refs.values() if r.report_name == report_name),
            key=lambda r: r.id,
        )

    def list_report_refs(self) -> list[ReportMappingRef]:
        return sorted(self.report_refs.values(), key=lambda r: r.id)

    # ------------------------------------------------------------------
    # 落图批次
    # ------------------------------------------------------------------
    def add_batch(self, revision_id: int, status: str = BATCH_PENDING, message: str = "") -> LandingBatch:
        batch = LandingBatch(
            id=self._next_batch_id,
            revision_id=revision_id,
            status=status,
            message=message,
        )
        self._next_batch_id += 1
        self.batches[batch.id] = batch
        return batch

    def get_batch(self, batch_id: int) -> LandingBatch | None:
        return self.batches.get(batch_id)

    def list_batches(self, revision_id: int | None = None) -> list[LandingBatch]:
        rows = sorted(self.batches.values(), key=lambda b: b.id)
        if revision_id is not None:
            rows = [b for b in rows if b.revision_id == revision_id]
        return rows

    # ------------------------------------------------------------------
    # 种子数据：销售额替换一列（GMV 交易事实语义 v1 → v2）
    # ------------------------------------------------------------------
    def _seed(self) -> None:
        # 交易事实语义模型（semantic_layer id=2）：v1 已发布，v2 待确认（一条边未确认）
        v1 = self.add_revision(
            semantic_id=2, semantic_name="交易事实语义模型", physical_table="dwd_trade_fact",
            field_mapping="order_id:订单ID, user_id:userId, amount:实付金额, status:订单状态",
            change_summary="初始版：GMV 取实付金额 amount 列",
            created_by="数据治理组", relations="LEFT JOIN dim_product ON sku_id",
            filters="order_status = 'completed'", security="金额字段需要数据范围授权",
            status=REV_PUBLISHED,
        )
        for field_name, column, target in [
            ("订单ID", "order_id", "cube_trade"),
            ("userId", "user_id", "cube_trade"),
            ("实付金额", "amount", "cube_trade"),
            ("订单状态", "status", "cube_trade"),
        ]:
            self.add_edge(
                revision_id=v1.id, semantic_field=field_name, physical_column=column,
                source_node="dwd_trade_fact", target_node=target, confirmed=True,
            )

        v2 = self.add_revision(
            semantic_id=2, semantic_name="交易事实语义模型", physical_table="dwd_trade_fact",
            field_mapping="order_id:订单ID, user_id:userId, amount:实付金额, settle_amount:销售额, status:订单状态",
            change_summary="销售额口径调整：新增 settle_amount(销售额结算列)，待确认后替换 amount(实付金额)",
            created_by="财务分析组", relations="LEFT JOIN dim_product ON sku_id",
            filters="order_status = 'completed'", security="金额字段需要数据范围授权",
            status=REV_PENDING,
        )
        # 沿用边保持确认；新增销售额血缘边尚未确认（确认版次之前不写入已发布关系）
        for field_name, column, target, confirmed in [
            ("订单ID", "order_id", "cube_trade", True),
            ("userId", "user_id", "cube_trade", True),
            ("实付金额", "amount", "cube_trade", True),
            ("销售额", "settle_amount", "cube_trade", False),
            ("订单状态", "status", "cube_trade", True),
        ]:
            self.add_edge(
                revision_id=v2.id, semantic_field=field_name, physical_column=column,
                source_node="dwd_trade_fact", target_node=target, confirmed=confirmed,
            )
        # 上次落图中断的批次：重试前保持「已中断」
        interrupted = self.add_batch(v2.id, status=BATCH_INTERRUPTED,
                                     message="落图中断：网络错误，1 条血缘边未写入")
        interrupted.attempt = 1
        interrupted.last_attempt_at = "2026-11-20 22:10:00 UTC"

        # DAU 指标语义（semantic_layer id=4）：仅 v1 已发布
        dau_v1 = self.add_revision(
            semantic_id=4, semantic_name="DAU指标语义", physical_table="dwd_user_behavior_log",
            field_mapping="user_id:userId, event_date=当日访问行为",
            change_summary="初始版：DAU 映射到行为日志",
            created_by="数据治理组", filters="event_date = :target_date",
            security="按部门限定日期范围", status=REV_PUBLISHED,
        )
        for field_name, column, target in [
            ("userId", "user_id", "cube_user_behavior"),
            ("当日访问行为", "event_date", "DAU日报表"),
        ]:
            self.add_edge(
                revision_id=dau_v1.id, semantic_field=field_name, physical_column=column,
                source_node="dwd_user_behavior_log", target_node=target, confirmed=True,
            )

        # 报表版次引用：上月财务报表固化在 v1；新分析报表的引用待 v2 发布后绑定
        self.add_report_ref(
            report_name="上月财务报表", semantic_id=2, revision_id=v1.id,
            note="财务追查上月报表，按原引用回查 v1",
        )
        self.add_report_ref(
            report_name="DAU日报表", semantic_id=4, revision_id=dau_v1.id,
            note="日报固化 DAU v1 映射",
        )


# 进程内单例
mapping_store = MappingVersionStore()

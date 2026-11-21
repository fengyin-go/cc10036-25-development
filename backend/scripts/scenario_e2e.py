"""映射修订版本化的端到端场景验证（仅标准库，python -I 运行）。

覆盖任务叙述的完整链路：
  双版次并存查询 → 旧报表按原引用回查 → 销售额替换列 → 未确认边阻断落图
  → 落图中断重试（幂等）→ 暂停新批次 → 确认前禁止发布 → 确认边 → 发布
  → 旧报表仍读旧版、新分析绑定新版 → 核对可读血缘版次
"""
import json
import sys
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000/api/semantic-revisions"
PASS = 0
FAIL = 0


def call(method: str, path: str, payload: dict | None = None) -> dict:
    # 路径中的中文报表名按段编码，查询串保留原样
    encoded = "/".join(
        part if part.startswith(("?", "&")) or part.isascii()
        else urllib.parse.quote(part)
        for part in path.split("/")
    )
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + encoded, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def check(label: str, condition: bool, detail: object = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        print(f"  ❌ {label} -> {detail}")


def edge_summary(resp: dict) -> str:
    return f"确认 {len(resp.get('confirmed_edges', []))} / 待确认 {len(resp.get('unconfirmed_edges', []))}"


print("== 场景0：种子数据 —— 交易事实语义（semantic_id=2）存在 v1 已发布、v2 待确认 ==")
items = call("GET", "")["items"]
trade = [r for r in items if r["semantic_id"] == 2]
check("存在两个版次", len(trade) == 2, trade)
v1 = next(r for r in trade if r["version_tag"] == "v1")
v2 = next(r for r in trade if r["version_tag"] == "v2")
check("v1 已发布", v1["status"] == "已发布", v1["status"])
check("v2 待确认且有 1 条未确认边",
      v2["status"] == "待确认" and v2["edge_unconfirmed"] == 1, v2)
unconfirmed = next(e for e in v2["edges"] if not e["confirmed"])
check("未确认边正是销售额新列 settle_amount",
      unconfirmed["physical_column"] == "settle_amount"
      and unconfirmed["semantic_field"] == "销售额", unconfirmed)

print("\n== 场景1：同一指标两个版本正在被查询，核对各自能读到的血缘 ==")
pub = call("GET", "/lineage/by-version/2")
check("默认读取当前已发布版 v1",
      pub["readable"] and pub["revision"]["version_tag"] == "v1", pub.get("revision"))
check("已发布血缘图不含未确认边",
      all(n for n in pub["graph"]["nodes"]) and
      not any(e["physical_column"] == "settle_amount" for e in pub["confirmed_edges"]),
      pub["confirmed_edges"])
v2_read = call("GET", "/lineage/by-version/2?version_tag=v2")
check("指定 v2 可回查修订数据", v2_read["readable"]
      and v2_read["revision"]["version_tag"] == "v2")
check("v2 血缘图隔离未确认边（图中只含 4 条已确认边）",
      len(v2_read["confirmed_edges"]) == 4 and len(v2_read["unconfirmed_edges"]) == 1,
      edge_summary(v2_read))

print("\n== 场景2：财务追查上月报表 —— 旧报表按原引用回查 ==")
old_report = call("GET", "/lineage/report/上月财务报表")
check("旧报表回查成功", old_report["ok"], old_report)
check("旧报表只能读到 v1 字段链",
      old_report["readable_version"] == ["v1"], old_report["readable_version"])
amount_chain = next((c for c in old_report["field_chains"]
                     if c["physical_column"] == "amount"), None)
check("字段链仍指向旧列 amount（实付金额）",
      amount_chain is not None and "dwd_trade_fact.amount" in amount_chain["field_chain"],
      amount_chain)
check("旧报表链路不包含 settle_amount",
      all(c["physical_column"] != "settle_amount" for c in old_report["field_chains"]))

print("\n== 场景3：为销售额替换一列并启动分析（再生成 v3 演示替换动作） ==")
repl = call("POST", "/replace-column", {
    "semantic_id": 2,
    "old_physical_column": "amount",
    "new_physical_column": "settle_amount",
    "new_semantic_field": "销售额",
    "target_node": "cube_trade",
    "change_summary": "分析采用销售额口径，替换实付金额列",
    "created_by": "今日分析任务",
})
check("替换列生成 v3 版次", repl["ok"] and repl["item"]["version_tag"] == "v3", repl)
check("v3 新边未确认，其余边沿用确认态（旧列边已移除、无重复边）",
      repl["item"]["edge_unconfirmed"] == 1 and repl["item"]["edge_confirmed"] == 3
      and repl["item"]["edge_total"] == 4,
      repl["item"])
check("v3 映射中 amount 已移除且 settle_amount 仅出现一次",
      all(not part.strip().startswith("amount:")
          for part in repl["item"]["field_mapping"].split(","))
      and repl["item"]["field_mapping"].count("settle_amount") == 1,
      repl["item"]["field_mapping"])
v3_id = repl["item"]["id"]

print("\n== 场景4：映射落地中断后重试 —— 未确认边不放行 ==")
# 种子里 v2 已有一个中断批次，先重试它
retry_v2 = call("POST", "/landing/1/retry")
check("v2 中断批次重试后仍中断",
      retry_v2.get("interrupted") and retry_v2["batch"]["status"] == "已中断", retry_v2)
check("重试幂等：已确认边保留登记，未确认边不写入",
      len(retry_v2["confirmed_edges"]) == 4 and len(retry_v2["pending_edges"]) == 1,
      edge_summary(retry_v2))
check("批次尝试次数累加", retry_v2["batch"]["attempt"] == 2, retry_v2["batch"])
# 已发布血缘依旧是 v1，新字段未写入
pub_again = call("GET", "/lineage/by-version/2")
check("中断后已发布关系仍是 v1",
      pub_again["revision"]["version_tag"] == "v1", pub_again["revision"])

print("\n== 场景5：暂停新批次落图 ==")
paused = call("POST", "/landing/pause")
check("落图暂停开关生效", paused["landing_paused"] is True)
blocked = call("POST", f"/{v3_id}/landing")
check("暂停期间新批次被拒绝并挂起",
      not blocked["ok"] and blocked.get("blocked"), blocked)
check("被拦批次状态为已暂停，未写入任何关系",
      blocked["batch"]["status"] == "已暂停", blocked["batch"])
retry_while_paused = call("POST", "/landing/1/retry")
check("暂停期间中断批次重试同样被拒",
      not retry_while_paused["ok"] and retry_while_paused.get("blocked"))
resumed = call("POST", "/landing/resume")
check("恢复落图", resumed["landing_paused"] is False)

print("\n== 场景6：确认版次之前不把新字段写入已发布关系 ==")
publish_blocked = call("POST", f"/{v3_id}/publish")
check("存在未确认边时发布被拒绝",
      not publish_blocked["ok"] and publish_blocked.get("blocked"), publish_blocked)
check("拒绝原因列出未确认边",
      len(publish_blocked.get("pending_edges", [])) == 1, publish_blocked)
readiness = call("GET", f"/{v3_id}/readiness")
check("就绪度核对：可读发布版仍为 v1",
      not readiness["ready_to_publish"]
      and readiness["currently_readable_published"]["version_tag"] == "v1", readiness)

print("\n== 场景7：确认血缘边 → 重试落图成功 → 发布 v3 ==")
v3_pending_edge = next(e for e in repl["item"]["edges"] if not e["confirmed"])
confirmed = call("POST", f"/edges/{v3_pending_edge['id']}/confirm")
check("血缘边确认成功", confirmed["ok"] and confirmed["edge"]["confirmed"], confirmed)
check("确认后无剩余未确认边", confirmed["unconfirmed_remaining"] == 0)
# 为 v3 建批次并重试落图
landing = call("POST", f"/{v3_id}/landing")
check("恢复后新批次可创建", landing["ok"], landing)
batch_id = landing["batch"]["id"]
done = call("POST", f"/landing/{batch_id}/retry")
check("全部边确认后落图完成",
      done["ok"] and done["batch"]["status"] == "已完成", done)
check("完成批次重复重试幂等",
      call("POST", f"/landing/{batch_id}/retry").get("idempotent") is True)
published = call("POST", f"/{v3_id}/publish")
check("v3 发布成功", published["ok"]
      and published["revision"]["version_tag"] == "v3", published)
check("v1 转为已替代但保留可回查",
      published["superseded_revision_id"] == v1["id"])

print("\n== 场景8：发布后核对 —— 旧报表仍读 v1，新分析绑定 v3 ==")
pub_now = call("GET", "/lineage/by-version/2")
check("当前已发布血缘切换为 v3 且含 settle_amount 边",
      pub_now["revision"]["version_tag"] == "v3"
      and any(e["physical_column"] == "settle_amount"
              for e in pub_now["confirmed_edges"]), pub_now["revision"])
old_report2 = call("GET", "/lineage/report/上月财务报表")
check("旧报表刷新后仍按原引用回查 v1",
      old_report2["readable_version"] == ["v1"]
      and all(c["physical_column"] != "settle_amount"
              for c in old_report2["field_chains"]), old_report2.get("bindings"))
v1_history = call("GET", "/lineage/by-version/2?version_tag=v1")
check("v1 历史版次血缘仍可显式回查",
      v1_history["readable"] and v1_history["revision"]["status"] == "已替代")
bind = call("POST", "/reports/bind", {
    "report_name": "今日销售额分析",
    "semantic_id": 2,
    "note": "采用改过的销售额字段，固化 v3",
})
check("新分析报表绑定当前发布版 v3",
      bind["ok"] and bind["report_ref"]["version_tag"] == "v3", bind)
new_report = call("GET", "/lineage/report/今日销售额分析")
check("新报表字段链走 settle_amount（销售额）",
      any(c["physical_column"] == "settle_amount" for c in new_report["field_chains"]),
      new_report.get("field_chains"))

print("\n== 场景9：暂停新批次后总核对 —— 究竟能读取哪一版血缘 ==")
call("POST", "/landing/pause")
status = call("GET", "/landing/status")
check("暂停态可查询且批次台账完整",
      status["landing_paused"] is True
      and {b["status"] for b in status["batches"]} >= {"已中断", "已完成", "已暂停"},
      status)
check("暂停不影响已发布血缘读取",
      call("GET", "/lineage/by-version/2")["revision"]["version_tag"] == "v3")
check("历史 v2 修订仍可回查（含未确认边清单）",
      call("GET", "/lineage/by-version/2?version_tag=v2")["readable"] is True)
call("POST", "/landing/resume")

print(f"\n结果：通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)

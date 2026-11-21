<template>
  <section class="page versioning-page">
    <header class="page-head">
      <div>
        <h2>语义版本管理</h2>
        <p class="page-desc">
          语义字段映射的版本化存储与血缘回查：修订版次可追溯，未确认血缘边不进入已发布关系，
          旧报表按固化的版次引用回查字段链。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="reloadAll">刷新台账</button>
      </div>
    </header>

    <p v-if="message" class="page-desc" :class="messageKind">{{ message }}</p>

    <!-- 落图总控：暂停 / 恢复 -->
    <div class="control-bar">
      <div class="control-state">
        <span class="state-dot" :class="landingPaused ? 'dot-paused' : 'dot-running'"></span>
        落图开关：<strong>{{ landingPaused ? '已暂停（新批次挂起，已发布血缘仍可读）' : '运行中' }}</strong>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" :disabled="!landingPaused" @click="onResume">恢复落图</button>
        <button class="btn primary" type="button" :disabled="landingPaused" @click="onPause">暂停新批次落图</button>
      </div>
    </div>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">映射修订版次</span>
        <strong class="stat-value">{{ revisions.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待确认血缘边</span>
        <strong class="stat-value">{{ totalUnconfirmed }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">落图批次</span>
        <strong class="stat-value">{{ batches.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">报表版次引用</span>
        <strong class="stat-value">{{ reportRefs.length }}</strong>
      </article>
    </div>

    <div class="panel-grid">
      <!-- 左：版次台账 -->
      <div class="panel">
        <h3>映射修订版次</h3>
        <div class="filter-bar compact">
          <label class="filter-item">
            <span>按语义定义 ID 过滤</span>
            <input v-model="semanticFilter" data-testid="revision-filter" placeholder="如 2" @keyup.enter="reloadRevisions" />
          </label>
          <button class="btn" type="button" @click="reloadRevisions">查询</button>
        </div>

        <div v-for="rev in revisions" :key="rev.id" class="revision-card" :data-testid="`revision-${rev.id}`">
          <div class="revision-head">
            <div>
              <strong>{{ rev.semantic_name }} · {{ rev.version_tag }}</strong>
              <span class="badge" :class="statusClass(rev.status)">{{ rev.status }}</span>
            </div>
            <span class="muted">#{{ rev.id }} · {{ rev.physical_table }} · {{ rev.created_at }}</span>
          </div>
          <p class="revision-summary">{{ rev.change_summary }}</p>
          <p class="revision-mapping"><code>{{ rev.field_mapping }}</code></p>
          <div class="edge-meta">
            血缘边：{{ rev.edge_confirmed }}/{{ rev.edge_total }} 已确认
            <span v-if="rev.edge_unconfirmed" class="warn-text">（{{ rev.edge_unconfirmed }} 条待确认）</span>
          </div>

          <table class="data-table edge-table">
            <thead>
              <tr><th>语义字段</th><th>物理列</th><th>源节点</th><th>目标节点</th><th>确认状态</th><th>操作</th></tr>
            </thead>
            <tbody>
              <tr v-for="edge in rev.edges" :key="edge.id" :data-testid="`edge-${edge.id}`">
                <td>{{ edge.semantic_field }}</td>
                <td><code>{{ edge.physical_column }}</code></td>
                <td>{{ edge.source_node }}</td>
                <td>{{ edge.target_node }}</td>
                <td>
                  <span class="badge" :class="edge.confirmed ? 'badge-confirmed' : 'badge-pending'">
                    {{ edge.confirmed ? '已确认' : '未确认' }}
                  </span>
                </td>
                <td>
                  <button
                    v-if="!edge.confirmed"
                    class="link"
                    type="button"
                    :data-testid="`confirm-edge-${edge.id}`"
                    @click="onConfirmEdge(edge.id)"
                  >确认血缘边</button>
                  <span v-else class="muted">已固化</span>
                </td>
              </tr>
            </tbody>
          </table>

          <div class="revision-actions">
            <button class="btn" type="button" :data-testid="`landing-${rev.id}`" @click="onStartLanding(rev.id)">
              创建落图批次
            </button>
            <button
              v-for="batch in rev.batches.filter((b) => b.status === '已中断' || b.status === '待执行')"
              :key="batch.id"
              class="btn"
              type="button"
              :data-testid="`retry-batch-${batch.id}`"
              @click="onRetryLanding(batch.id)"
            >重试批次 #{{ batch.id }}（{{ batch.status }}）</button>
            <button
              class="btn primary"
              type="button"
              :disabled="!rev.publishable"
              :data-testid="`publish-${rev.id}`"
              @click="onPublish(rev.id)"
            >发布版次</button>
            <button class="btn ghost" type="button" @click="onReadiness(rev.id)">核对可读血缘</button>
          </div>
          <ul v-if="rev.batches.length" class="batch-list">
            <li v-for="batch in rev.batches" :key="batch.id" :class="`batch-${batch.status}`">
              批次 #{{ batch.id }}：{{ batch.status }} · 尝试 {{ batch.attempt }} 次 · {{ batch.message }}
            </li>
          </ul>
        </div>
      </div>

      <!-- 右：操作与回查 -->
      <div class="side-column">
        <div class="panel">
          <h3>为销售额替换一列</h3>
          <form class="stack-form" @submit.prevent="onReplaceColumn">
            <label class="create-field"><span>语义定义 ID</span>
              <input v-model="replaceForm.semantic_id" required placeholder="2" /></label>
            <label class="create-field"><span>被替换物理列（旧）</span>
              <input v-model="replaceForm.old_physical_column" required placeholder="amount" /></label>
            <label class="create-field"><span>新物理列</span>
              <input v-model="replaceForm.new_physical_column" required placeholder="settle_amount" /></label>
            <label class="create-field"><span>新语义字段名</span>
              <input v-model="replaceForm.new_semantic_field" required placeholder="销售额" /></label>
            <label class="create-field"><span>目标血缘节点</span>
              <input v-model="replaceForm.target_node" placeholder="cube_trade" /></label>
            <label class="create-field"><span>变更说明</span>
              <input v-model="replaceForm.change_summary" required placeholder="销售额口径替换实付金额" /></label>
            <button class="btn primary" type="button" data-testid="submit-replace" @click="onReplaceColumn">
              生成新版次
            </button>
          </form>
        </div>

        <div class="panel">
          <h3>按版次读取血缘</h3>
          <form class="stack-form" @submit.prevent="onQueryLineage">
            <label class="create-field"><span>语义定义 ID</span>
              <input v-model="lineageQuery.semantic_id" required placeholder="2" /></label>
            <label class="create-field"><span>版次标签（留空读当前已发布）</span>
              <input v-model="lineageQuery.version_tag" placeholder="v1 / v2 / v3" /></label>
            <button class="btn primary" type="button" data-testid="query-lineage" @click="onQueryLineage">
              查询血缘版次
            </button>
          </form>
          <div v-if="lineageResult" class="result-box" data-testid="lineage-result">
            <p v-if="!lineageResult.ok" class="warn-text">{{ lineageResult.message }}</p>
            <template v-else>
              <p>
                可读版次：<strong>{{ lineageResult.revision?.version_tag }}</strong>
                （{{ lineageResult.revision?.status }}）
              </p>
              <p class="muted">{{ lineageResult.message }}</p>
              <p>已确认边 {{ lineageResult.confirmed_edges?.length ?? 0 }} 条，
                未确认边 {{ lineageResult.unconfirmed_edges?.length ?? 0 }} 条（不进入血缘图）</p>
              <ul class="edge-chain-list">
                <li v-for="edge in lineageResult.confirmed_edges" :key="edge.id">
                  {{ edge.target_node }} ← {{ edge.source_node }}.{{ edge.physical_column }}
                  （{{ edge.semantic_field }}）
                </li>
              </ul>
            </template>
          </div>
        </div>

        <div class="panel">
          <h3>旧报表字段链回查</h3>
          <form class="stack-form" @submit.prevent="onQueryReport">
            <label class="create-field"><span>报表名称</span>
              <input v-model="reportQuery" required placeholder="上月财务报表" /></label>
            <button class="btn primary" type="button" data-testid="query-report" @click="onQueryReport">
              查询报表字段链
            </button>
          </form>
          <div v-if="reportResult" class="result-box" data-testid="report-result">
            <p v-if="!reportResult.ok" class="warn-text">{{ reportResult.message }}</p>
            <template v-else>
              <p>可读版次：<strong>{{ reportResult.readable_version?.join('、') }}</strong></p>
              <ul class="edge-chain-list">
                <li v-for="chain in reportResult.field_chains" :key="chain.version_tag + chain.physical_column">
                  <span class="badge badge-confirmed">{{ chain.version_tag }}</span>
                  {{ chain.field_chain }}
                </li>
              </ul>
            </template>
          </div>
        </div>

        <div class="panel">
          <h3>报表版次引用</h3>
          <ul class="ref-list">
            <li v-for="ref in reportRefs" :key="ref.id">
              <strong>{{ ref.report_name }}</strong>
              <span class="badge badge-confirmed">{{ ref.version_tag }}</span>
              <span class="muted">{{ ref.note }}</span>
            </li>
          </ul>
          <form class="stack-form" @submit.prevent="onBindReport">
            <label class="create-field"><span>报表名称</span>
              <input v-model="bindForm.report_name" required placeholder="今日销售额分析" /></label>
            <label class="create-field"><span>语义定义 ID（绑定当前已发布版）</span>
              <input v-model="bindForm.semantic_id" required placeholder="2" /></label>
            <label class="create-field"><span>备注</span>
              <input v-model="bindForm.note" placeholder="采用改过的销售额字段" /></label>
            <button class="btn" type="button" @click="onBindReport">固化报表引用</button>
          </form>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  bindReport,
  confirmEdge,
  landingReadiness,
  landingStatus,
  lineageByVersion,
  listReportRefs,
  listRevisions,
  pauseLanding,
  publishRevision,
  replaceColumn,
  reportFieldChain,
  resumeLanding,
  retryLanding,
  startLanding,
} from '@/api/versioning'
import type {
  GenericResult,
  LandingBatch,
  LineageVersionResult,
  ReportChainResult,
  ReportRef,
  RevisionView,
} from '@/api/versioning'

const revisions = ref<RevisionView[]>([])
const reportRefs = ref<ReportRef[]>([])
const batches = ref<LandingBatch[]>([])
const landingPaused = ref(false)
const semanticFilter = ref('2')
const message = ref('')
const messageKind = ref('')

const replaceForm = ref({
  semantic_id: '2',
  old_physical_column: 'amount',
  new_physical_column: 'settle_amount',
  new_semantic_field: '销售额',
  target_node: 'cube_trade',
  change_summary: '分析采用销售额口径，替换实付金额列',
})
const lineageQuery = ref({ semantic_id: '2', version_tag: '' })
const lineageResult = ref<LineageVersionResult | null>(null)
const reportQuery = ref('上月财务报表')
const reportResult = ref<ReportChainResult | null>(null)
const bindForm = ref({ report_name: '今日销售额分析', semantic_id: '2', note: '采用改过的销售额字段' })

const totalUnconfirmed = computed(() =>
  revisions.value.reduce((sum, rev) => sum + rev.edge_unconfirmed, 0),
)

function flash(text: string, kind: 'ok' | 'warn' = 'ok') {
  message.value = text
  messageKind.value = kind === 'warn' ? 'warn-text' : 'ok-text'
}

function statusClass(status: string): string {
  if (status === '已发布') return 'badge-published'
  if (status === '已替代') return 'badge-superseded'
  if (status === '已确认') return 'badge-confirmed'
  return 'badge-pending'
}

async function reloadRevisions() {
  const semanticId = semanticFilter.value.trim() ? Number(semanticFilter.value) : undefined
  revisions.value = await listRevisions(semanticId)
}

async function reloadLanding() {
  const status = await landingStatus()
  landingPaused.value = status.landing_paused
  batches.value = status.batches
}

async function reloadRefs() {
  reportRefs.value = (await listReportRefs()).items
}

async function reloadAll() {
  try {
    await Promise.all([reloadRevisions(), reloadLanding(), reloadRefs()])
  } catch (error) {
    flash(error instanceof Error ? error.message : '台账读取失败', 'warn')
  }
}

async function onPause() {
  const result = await pauseLanding()
  flash(result.message ?? '落图已暂停')
  await reloadLanding()
}

async function onResume() {
  const result = await resumeLanding()
  flash(result.message ?? '落图已恢复')
  await reloadLanding()
}

async function onConfirmEdge(edgeId: number) {
  const result = await confirmEdge(edgeId)
  flash(result.message ?? '血缘边已确认', result.ok ? 'ok' : 'warn')
  await reloadAll()
}

async function onStartLanding(revisionId: number) {
  const result = await startLanding(revisionId)
  flash(result.message ?? '落图批次已创建', result.ok ? 'ok' : 'warn')
  await reloadAll()
}

async function onRetryLanding(batchId: number) {
  const result = await retryLanding(batchId)
  flash(result.message ?? '重试完成', result.ok ? 'ok' : 'warn')
  await reloadAll()
}

async function onPublish(revisionId: number) {
  const result = await publishRevision(revisionId)
  flash(result.message ?? '发布完成', result.ok ? 'ok' : 'warn')
  await reloadAll()
}

async function onReadiness(revisionId: number) {
  const result = await landingReadiness(revisionId)
  flash(result.message ?? '就绪度已核对', result.ok ? 'ok' : 'warn')
}

async function onReplaceColumn() {
  const result = await replaceColumn({
    semantic_id: Number(replaceForm.value.semantic_id),
    old_physical_column: replaceForm.value.old_physical_column,
    new_physical_column: replaceForm.value.new_physical_column,
    new_semantic_field: replaceForm.value.new_semantic_field,
    target_node: replaceForm.value.target_node,
    change_summary: replaceForm.value.change_summary,
    created_by: '前端工作台',
  })
  flash(result.message ?? '新版次已生成', result.ok ? 'ok' : 'warn')
  await reloadAll()
}

async function onQueryLineage() {
  lineageResult.value = await lineageByVersion(
    Number(lineageQuery.value.semantic_id),
    lineageQuery.value.version_tag.trim() || undefined,
  )
}

async function onQueryReport() {
  reportResult.value = await reportFieldChain(reportQuery.value)
}

async function onBindReport() {
  const result = await bindReport({
    report_name: bindForm.value.report_name,
    semantic_id: Number(bindForm.value.semantic_id),
    note: bindForm.value.note,
  })
  flash(result.message ?? '报表引用已固化', result.ok ? 'ok' : 'warn')
  await reloadRefs()
}

onMounted(reloadAll)
</script>

<style scoped>
.versioning-page { display: flex; flex-direction: column; gap: 12px; }
.control-bar {
  display: flex; justify-content: space-between; align-items: center;
  background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px;
}
.control-state { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.state-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.dot-running { background: #22c55e; }
.dot-paused { background: #f59e0b; }
.panel-grid { display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(320px, 1fr); gap: 14px; }
.side-column { display: flex; flex-direction: column; gap: 14px; }
.panel {
  background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px;
}
.panel h3 { margin: 0 0 10px; font-size: 15px; }
.filter-bar.compact { margin-bottom: 10px; }
.revision-card {
  border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 12px;
  background: #fafbfc;
}
.revision-head { display: flex; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
.revision-summary { font-size: 13px; margin: 8px 0 4px; }
.revision-mapping { margin: 4px 0; }
.revision-mapping code { font-size: 12px; background: #eef2f7; padding: 2px 6px; border-radius: 4px; }
.edge-meta { font-size: 12px; color: var(--muted); margin-bottom: 8px; }
.edge-table { font-size: 12px; }
.edge-table code { background: #eef2f7; padding: 1px 4px; border-radius: 4px; }
.revision-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
.batch-list { margin: 8px 0 0; padding-left: 18px; font-size: 12px; color: var(--muted); }
.batch-已完成 { color: #15803d; }
.batch-已中断 { color: #b42318; }
.batch-已暂停 { color: #b45309; }
.badge {
  display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 10px;
  margin-left: 6px; border: 1px solid transparent;
}
.badge-published { background: #dcfce7; color: #15803d; }
.badge-superseded { background: #f1f5f9; color: #64748b; }
.badge-confirmed { background: #dbeafe; color: #1d4ed8; }
.badge-pending { background: #fef3c7; color: #b45309; }
.stack-form { display: flex; flex-direction: column; gap: 4px; }
.result-box {
  margin-top: 10px; border: 1px dashed var(--border); border-radius: 6px;
  padding: 10px; font-size: 13px; background: #fafbfc;
}
.edge-chain-list, .ref-list { margin: 6px 0; padding-left: 18px; font-size: 13px; }
.edge-chain-list li, .ref-list li { margin-bottom: 6px; }
.ref-list { list-style: none; padding-left: 0; display: flex; flex-direction: column; gap: 6px; }
.muted { color: var(--muted); font-size: 12px; }
.warn-text { color: #b42318; font-size: 13px; }
.ok-text { color: #15803d; font-size: 13px; }
@media (max-width: 1100px) { .panel-grid { grid-template-columns: 1fr; } }
</style>

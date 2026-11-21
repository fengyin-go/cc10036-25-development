/** 语义映射版本化接口封装：版次、落图批次、血缘版次读取与报表回查。 */
import { request } from './client'

const BASE = '/api/semantic-revisions'

export type LineageEdge = {
  id: number
  revision_id: number
  semantic_field: string
  physical_column: string
  source_node: string
  target_node: string
  status: 'confirmed' | 'unconfirmed'
  confirmed: boolean
  confirm_time: string | null
  created_at: string
}

export type LandingBatch = {
  id: number
  revision_id: number
  status: string
  attempt: number
  created_at: string
  last_attempt_at: string | null
  finished_at: string | null
  message: string
}

export type RevisionView = {
  id: number
  semantic_id: number
  semantic_name: string
  physical_table: string
  revision_no: number
  version_tag: string
  status: string
  change_summary: string
  field_mapping: string
  relations: string
  filters: string
  security: string
  created_by: string
  created_at: string
  published_at: string | null
  edge_total: number
  edge_confirmed: number
  edge_unconfirmed: number
  edges: LineageEdge[]
  batches: LandingBatch[]
  publishable: boolean
}

export type ReportRef = {
  id: number
  report_name: string
  semantic_id: number
  revision_id: number
  version_tag: string
  bound_at: string
  note: string
}

export type GenericResult = {
  ok: boolean
  message?: string
  blocked?: boolean
  interrupted?: boolean
  idempotent?: boolean
  [key: string]: unknown
}

export type LineageVersionResult = {
  ok: boolean
  readable?: boolean
  message?: string
  semantic_id?: number
  revision?: {
    id: number
    version_tag: string
    status: string
    field_mapping: string
    [key: string]: unknown
  }
  is_published?: boolean
  confirmed_edges: LineageEdge[]
  unconfirmed_edges: LineageEdge[]
  graph?: { nodes: string[]; adjacency: Record<string, string[]> }
}

export type FieldChain = {
  report_name: string
  version_tag: string
  semantic_field: string
  physical_column: string
  field_chain: string
  upstream_paths: string[][]
  uses_current_published: boolean
}

export type ReportChainResult = {
  ok: boolean
  message?: string
  report_name?: string
  readable_version?: string[]
  field_chains?: FieldChain[]
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(`${BASE}${path}`)
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as T
}

async function postJson<T>(path: string, payload: Record<string, unknown> = {}): Promise<T> {
  const response = await request(`${BASE}${path}`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as T
}

export async function listRevisions(semanticId?: number): Promise<RevisionView[]> {
  const query = semanticId ? `?semantic_id=${semanticId}` : ''
  const data = await getJson<{ ok: boolean; items: RevisionView[] }>(query)
  return data.items
}

export function replaceColumn(payload: {
  semantic_id: number
  old_physical_column: string
  new_physical_column: string
  new_semantic_field: string
  target_node: string
  change_summary: string
  created_by: string
}): Promise<GenericResult & { item: RevisionView }> {
  return postJson('/replace-column', payload as unknown as Record<string, unknown>)
}

export function landingStatus(): Promise<{ landing_paused: boolean; batches: LandingBatch[] }> {
  return getJson('/landing/status')
}

export function pauseLanding(): Promise<GenericResult> {
  return postJson('/landing/pause')
}

export function resumeLanding(): Promise<GenericResult> {
  return postJson('/landing/resume')
}

export function startLanding(revisionId: number): Promise<GenericResult> {
  return postJson(`/${revisionId}/landing`)
}

export function retryLanding(batchId: number): Promise<GenericResult> {
  return postJson(`/landing/${batchId}/retry`)
}

export function confirmEdge(edgeId: number): Promise<GenericResult> {
  return postJson(`/edges/${edgeId}/confirm`)
}

export function publishRevision(revisionId: number): Promise<GenericResult> {
  return postJson(`/${revisionId}/publish`)
}

export function landingReadiness(revisionId: number): Promise<GenericResult> {
  return getJson(`/${revisionId}/readiness`)
}

export function lineageByVersion(
  semanticId: number,
  versionTag?: string,
): Promise<LineageVersionResult> {
  const query = versionTag ? `?version_tag=${encodeURIComponent(versionTag)}` : ''
  return getJson(`/lineage/by-version/${semanticId}${query}`)
}

export function reportFieldChain(reportName: string): Promise<ReportChainResult> {
  return getJson(`/lineage/report/${encodeURIComponent(reportName)}`)
}

export function bindReport(payload: {
  report_name: string
  semantic_id: number
  revision_id?: number
  note?: string
}): Promise<GenericResult> {
  return postJson('/reports/bind', payload as unknown as Record<string, unknown>)
}

export function listReportRefs(): Promise<{ ok: boolean; items: ReportRef[] }> {
  return getJson('/reports/refs')
}

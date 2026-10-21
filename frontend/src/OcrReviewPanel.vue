<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'

type Block = Record<string, any>
type Review = {
  review_id: string; status: string; document_id: string; source_filename: string
  page_number: number | null; reason: string; source_preview_path: string
  allowed_actions: string[]; suggested_action: string
  web_job_id?: string
  metadata: { ocr_decision?: string; run_dir?: string }
  latest_decision?: { selected_action: string } | null
}
type Evidence = { native_blocks: Block[]; native_count: number; ocr_blocks: Block[]; ocr_count: number }
type ReviewGroup = { key: string; filename: string; runDir: string; webJobId: string; items: Review[]; visible: Review[] }
type BulkExecution = { execution_id: string; run_dir: string; status: string; total_count: number
  completed_count: number; failed_count: number; pages: Record<string, { page_number: number | null; status: string; error: string }> }

const props = defineProps<{ reviews: Review[] }>()
const emit = defineEmits<{ updated: []; openRun: [runDir: string] }>()
const filter = ref('all')
const reviewer = ref('local_user')
const note = ref('')
const expandedId = ref('')
const evidence = ref<Evidence | null>(null)
const busyId = ref('')
const error = ref('')
const message = ref('')
const expandedGroup = ref('')
const bulkBusy = ref('')
const bulkExecutions = ref<BulkExecution[]>([])
let bulkTimer: number | undefined
function runName(path?: string): string { return path?.split(/[\\/]/).pop() || '未知运行' }
function jobName(id?: string): string { return id?.replace(/^job_/, '').slice(0, 8) || '' }
function remaining(items: Review[]): number { return items.filter(item => item.status !== 'applied').length }
function pageNumbers(items: Review[]): string {
  const pages = [...new Set(items.map(item => item.page_number).filter((page): page is number => page !== null))].sort((a, b) => a - b)
  return pages.length ? pages.join('、') : '区域任务'
}
function toggleGroup(key: string) {
  expandedGroup.value = expandedGroup.value === key ? '' : key
  expandedId.value = ''
  evidence.value = null
}
function approvable(items: Review[]): number { return items.filter(item => item.status === 'pending' && item.allowed_actions.includes('run_ocr')).length }
function executable(items: Review[]): number { return items.filter(item => item.status === 'ocr_requested').length }
function activeBulk(runDir: string): BulkExecution | undefined {
  return [...bulkExecutions.value].reverse().find(job => job.run_dir.toLowerCase() === runDir.toLowerCase())
}
function groupBusy(runDir: string): boolean { return !!busyId.value || !!bulkBusy.value || activeBulk(runDir)?.status === 'running' }
async function loadBulkExecutions() {
  try { bulkExecutions.value = await api<BulkExecution[]>('/api/reviews/ocr/bulk/executions') }
  catch { /* The regular OCR list remains usable if bulk progress is temporarily unavailable. */ }
}
async function bulkApprove(group: ReviewGroup) {
  if (!reviewer.value.trim()) { error.value = '请填写审核人'; return }
  error.value = ''; message.value = ''; bulkBusy.value = group.key
  try {
    const result = await api<{ approved_count: number }>('/api/reviews/ocr/bulk/approve', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_dir: group.runDir, reviewer: reviewer.value, note: note.value }),
    })
    message.value = `已为「${group.filename}」的 ${result.approved_count} 项页面任务批量批准 OCR。可继续点击“批量执行 OCR”。`
    emit('updated')
  } catch (cause) { error.value = (cause as Error).message }
  finally { bulkBusy.value = '' }
}
async function bulkExecute(group: ReviewGroup) {
  error.value = ''; message.value = ''; bulkBusy.value = group.key
  try {
    const job = await api<BulkExecution>('/api/reviews/ocr/bulk/execute', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_dir: group.runDir }),
    })
    bulkExecutions.value = [...bulkExecutions.value, job]
    message.value = `已开始执行「${group.filename}」的 ${job.total_count} 项 OCR，最多同时处理 2 页。`
    emit('updated')
  } catch (cause) { error.value = (cause as Error).message }
  finally { bulkBusy.value = '' }
}

const filters = [
  { value: 'all', label: '全部' }, { value: 'pending', label: '待判断' },
  { value: 'ocr_requested', label: '待执行 OCR' }, { value: 'executing', label: '正在 OCR' },
  { value: 'ocr_result_ready', label: '待选结果' }, { value: 'decided', label: '待应用' },
  { value: 'applied', label: '已完成' },
]
const groups = computed<ReviewGroup[]>(() => {
  const byRun = new Map<string, Review[]>()
  for (const item of props.reviews) {
    const key = item.metadata?.run_dir?.toLowerCase() || `task:${item.review_id}`
    const items = byRun.get(key) || []
    items.push(item)
    byRun.set(key, items)
  }
  return [...byRun].map(([key, items]) => {
    const ordered = [...items].sort((a, b) => (a.page_number ?? Number.MAX_SAFE_INTEGER) - (b.page_number ?? Number.MAX_SAFE_INTEGER))
    return { key, filename: items[0].source_filename, runDir: items[0].metadata?.run_dir || '',
      webJobId: items[0].web_job_id || '', items: ordered,
      visible: filter.value === 'all' ? ordered : ordered.filter(item => item.status === filter.value) }
  })
})
const visibleGroups = computed(() => groups.value.filter(group => group.visible.length > 0))
function count(status: string): number { return status === 'all' ? props.reviews.length : props.reviews.filter(item => item.status === status).length }
function statusText(status: string): string {
  return ({ pending: '待判断', ocr_requested: '已批准 · 待执行', executing: '正在 OCR',
    ocr_result_ready: 'OCR 结果待审核', decided: '决定待应用', applied: '已完成' } as Record<string, string>)[status] || status
}
function blockText(block: Block): string {
  if (block.text) return String(block.text)
  const table = block.table_data
  if (table) return [table.headers?.join(' | '), ...(table.rows || []).slice(0, 6).map((row: string[]) => row.join(' | '))].filter(Boolean).join('\n')
  return block.image?.caption || block.image?.ocr_text || '[图片或空内容]'
}
async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, options)
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(payload.detail || `请求失败 (${response.status})`)
  return payload as T
}
async function toggleEvidence(item: Review) {
  if (expandedId.value === item.review_id) { expandedId.value = ''; evidence.value = null; return }
  error.value = ''; evidence.value = null; expandedId.value = item.review_id
  try { evidence.value = await api<Evidence>(`/api/reviews/ocr/${item.review_id}/blocks`) }
  catch (cause) { error.value = (cause as Error).message }
}
async function action(item: Review, selected: string) {
  if (selected !== 'execute' && selected !== 'apply' && !reviewer.value.trim()) {
    error.value = '请填写审核人'; return
  }
  error.value = ''; message.value = ''; busyId.value = item.review_id
  try {
    const path = `/api/reviews/ocr/${item.review_id}`
    let result: Record<string, any> | null = null
    if (selected === 'execute' || selected === 'apply') {
      result = await api<Record<string, any>>(`${path}/${selected}`, { method: 'POST' })
    } else {
      await api(`${path}/decision`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: selected, reviewer: reviewer.value, note: note.value }) })
    }
    message.value = ({ run_ocr: '已批准 OCR，请点击“执行 OCR”开始调用服务。', execute: 'OCR 执行完成，请对照结果后选择采用方式。',
      approve_ocr: '已选择 OCR 结果，请点击“应用决定”生成最终版本。', approve_native: '已选择原生结果，请点击“应用决定”。',
      apply: result?.document_finalization?.status === 'completed'
        ? '本次解析的全部 OCR 审核已完成，最终产物已更新。'
        : result?.document_finalization?.status === 'pending_reviews'
          ? `本页决定已应用。本次解析仍有 ${result.document_finalization.unresolved_review_ids.length} 项待应用，整份文档尚未定稿。`
          : '本页决定已应用，请查看本次解析的最终状态。' } as Record<string, string>)[selected] || '审核任务已更新。'
    emit('updated')
    if (expandedId.value === item.review_id) evidence.value = await api<Evidence>(`${path}/blocks`)
  } catch (cause) { error.value = (cause as Error).message }
  finally { busyId.value = '' }
}
onMounted(() => { void loadBulkExecutions(); bulkTimer = window.setInterval(() => void loadBulkExecutions(), 1500) })
onUnmounted(() => { if (bulkTimer) window.clearInterval(bulkTimer) })
</script>

<template>
  <section class="review-intro"><div class="eyebrow">HUMAN IN THE LOOP</div><h1>OCR 审核与执行</h1><p>先判断是否需要 OCR；执行完成后对照原生与 OCR 结果，再决定采用哪一版。</p></section>
  <section class="review-card card">
    <div class="card-heading"><h3>OCR 审核文件 <span class="muted">{{ groups.length }} 份 · {{ reviews.length }} 项审核任务</span></h3><button class="text-button" @click="emit('updated')">刷新 ↻</button></div>
    <div class="ocr-summary"><span>待执行 {{ count('ocr_requested') }}</span><span>正在 OCR {{ count('executing') }}</span><span>待选结果 {{ count('ocr_result_ready') }}</span></div>
    <div class="ocr-filters"><button v-for="item in filters" :key="item.value" :class="{ active: filter === item.value }" @click="filter = item.value">{{ item.label }} <small>{{ count(item.value) }}</small></button></div>
    <p v-if="error" class="inline-error">{{ error }}</p><p v-if="message" class="inline-success">{{ message }}</p>
    <div class="review-form"><label>审核人<input v-model="reviewer" class="text-input" placeholder="填写姓名或工号"></label><label>备注<input v-model="note" class="text-input" placeholder="可填写判断依据"></label></div>
    <div v-if="!visibleGroups.length" class="empty">{{ filter === 'executing' ? '当前没有正在执行的 OCR。已批准但尚未执行的文件在“待执行 OCR”中。' : '当前筛选下没有 OCR 任务。' }}</div>
    <section v-for="group in visibleGroups" :key="group.key" class="ocr-file-group">
      <button class="ocr-file-header" :aria-expanded="expandedGroup === group.key" @click="toggleGroup(group.key)">
        <span class="ocr-file-title"><strong>{{ group.filename }}</strong><span v-if="group.webJobId" class="review-batch">工作台批次 {{ jobName(group.webJobId) }}</span></span>
        <span class="ocr-file-count"><b>{{ remaining(group.items) }} 待处理</b><span>{{ group.items.length - remaining(group.items) }} 已完成</span><span>{{ group.visible.length }} 项{{ filter === 'all' ? '审核任务' : '符合筛选' }}</span><span class="ocr-file-chevron">{{ expandedGroup === group.key ? '收起 ↑' : '查看页面 ↓' }}</span></span>
        <small>涉及页码：{{ pageNumbers(group.items) }}</small>
      </button>
      <div v-if="expandedGroup === group.key" class="ocr-file-content">
        <div class="review-run"><span :title="group.runDir">文件解析记录：{{ runName(group.runDir) }}</span><button v-if="group.runDir" class="text-button" @click="emit('openRun', group.runDir)">查看对应解析结果 →</button></div>
        <div class="ocr-bulk-toolbar">
          <p>批量操作只作用于这份文件中符合当前阶段的页面；OCR 完成后仍需逐页核对并选择结果。</p>
          <div><button class="secondary" :disabled="groupBusy(group.runDir) || approvable(group.items) === 0" @click="bulkApprove(group)">批量批准 OCR（{{ approvable(group.items) }}）</button><button class="primary compact" :disabled="groupBusy(group.runDir) || executable(group.items) === 0" @click="bulkExecute(group)">批量执行 OCR（{{ executable(group.items) }}）</button></div>
        </div>
        <div v-if="activeBulk(group.runDir)" class="ocr-bulk-progress"><strong>批量 OCR：{{ activeBulk(group.runDir)?.completed_count }}/{{ activeBulk(group.runDir)?.total_count }} 已完成</strong><span v-if="activeBulk(group.runDir)?.status === 'running'">正在处理，最多同时执行 2 页</span><span v-else-if="activeBulk(group.runDir)?.failed_count">{{ activeBulk(group.runDir)?.failed_count }} 项失败，可检查原因后重试</span><span v-else>执行完成，请逐页审核 OCR 结果</span><p v-for="[reviewId, page] in Object.entries(activeBulk(group.runDir)?.pages || {}).filter(([, page]) => page.status === 'failed')" :key="reviewId">第 {{ page.page_number || '—' }} 页：{{ page.error }}</p></div>
        <article v-for="item in group.visible" :key="item.review_id" class="review-item ocr-item">
          <div class="review-main"><div class="ocr-task-heading"><span :class="['status', item.status]">{{ statusText(item.status) }}</span><strong class="ocr-page-title">{{ item.page_number ? `第 ${item.page_number} 页` : '图片区域' }}</strong><span v-if="item.metadata?.ocr_decision === 'auto_ocr'" class="review-page">系统建议 OCR</span></div><p>{{ item.reason }}</p><button class="text-button evidence-toggle" @click="toggleEvidence(item)">{{ expandedId === item.review_id ? '收起证据 ↑' : '查看源图和解析证据 ↓' }}</button></div>
          <div class="review-actions"><button v-if="item.status === 'pending' && item.allowed_actions.includes('approve_native')" class="secondary" :disabled="groupBusy(group.runDir)" @click="action(item, 'approve_native')">采用原生结果</button><button v-if="item.status === 'pending' && item.allowed_actions.includes('run_ocr')" class="secondary" :disabled="groupBusy(group.runDir)" @click="action(item, 'run_ocr')">批准 OCR</button><button v-if="item.status === 'ocr_requested'" class="primary compact" :disabled="groupBusy(group.runDir)" @click="action(item, 'execute')">执行 OCR</button><span v-if="item.status === 'executing'" class="ocr-running">正在调用 OCR 服务…</span><button v-if="item.status === 'ocr_result_ready' && item.allowed_actions.includes('approve_ocr')" class="secondary" :disabled="groupBusy(group.runDir)" @click="action(item, 'approve_ocr')">采用 OCR 结果</button><button v-if="item.status === 'ocr_result_ready' && item.allowed_actions.includes('approve_native')" class="secondary" :disabled="groupBusy(group.runDir)" @click="action(item, 'approve_native')">保留原生结果</button><button v-if="item.status === 'decided'" class="primary compact" :disabled="groupBusy(group.runDir)" @click="action(item, 'apply')">应用决定</button></div>
          <div v-if="expandedId === item.review_id" class="ocr-evidence"><img :src="`/api/reviews/ocr/${item.review_id}/preview`" :alt="`${item.source_filename} OCR 源图`"><div class="ocr-evidence-columns"><div><strong>原生解析 · {{ evidence?.native_count ?? 0 }} Blocks</strong><p v-if="!evidence?.native_blocks.length">此区域没有可用的原生 Block。</p><pre v-for="block in evidence?.native_blocks || []" :key="block.block_id">{{ blockText(block) }}</pre></div><div><strong>OCR 解析 · {{ evidence?.ocr_count ?? 0 }} Blocks</strong><p v-if="!evidence?.ocr_blocks.length">OCR 尚未执行，或尚无可展示的结果。</p><pre v-for="block in evidence?.ocr_blocks || []" :key="block.block_id">{{ blockText(block) }}</pre></div></div><small v-if="evidence && (evidence.native_count > 30 || evidence.ocr_count > 30)">这里只展示前 30 个 Block，完整内容保存在运行产物中。</small></div>
        </article>
      </div>
    </section>
    <p class="review-footnote">“批准 OCR”只记录允许执行；点击“执行 OCR”才会调用已配置的服务。“采用 OCR 结果”后还需“应用决定”。复杂合并与排除仍可使用 CLI。</p>
  </section>
</template>

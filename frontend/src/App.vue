<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import DocumentResult from './DocumentResult.vue'
import OcrReviewPanel from './OcrReviewPanel.vue'
import HistoryPanel from './HistoryPanel.vue'

type FileStatus = 'queued' | 'running' | 'finalized' | 'review_required' | 'failed' | 'cancelled'
type FileItem = {
  source_path: string
  status: FileStatus
  document_id: string
  block_count: number
  run_dir: string
  error: string
  artifact_source?: string
  quality_warning_count?: number
}
type Job = {
  job_id: string
  status: string
  created_at: string
  updated_at: string
  input_dir: string
  workers: number
  file_count: number
  completed_count: number
  finalized_count: number
  review_required_count: number
  failed_count: number
  cancelled_count: number
  report_path: string
  error: string
  files: FileItem[]
}
type HistoryEntry = FileItem & { job_id: string; index: number; created_at: string; job_status: string }
type Detail = {
  file: FileItem
  title?: string
  block_count: number
  blocks: Array<Record<string, any>>
  manifest: Record<string, any> | null
  evaluation: Record<string, any> | null
  ocr_progress?: { task_count: number; applied_count: number; pending_count: number; applied_pages: number[]; status: string } | null
}
type Review = {
  review_id: string
  status: string
  document_id: string
  source_filename: string
  page_number: number | null
  reason: string
  source_preview_path: string
  allowed_actions: string[]
  suggested_action: string
  web_job_id?: string
  metadata: { ocr_decision?: string; run_dir?: string }
  latest_decision?: { selected_action: string } | null
}

const jobs = ref<Job[]>([])
const history = ref<HistoryEntry[]>([])
const selectedJobId = ref('')
const selectedIndex = ref<number | null>(null)
const detail = ref<Detail | null>(null)
const reviews = ref<Review[]>([])
const tab = ref<'workspace' | 'reviews' | 'history'>('workspace')
const inputMode = ref<'upload' | 'path'>('upload')
const folderPath = ref('')
const recursive = ref(true)
const workers = ref(2)
const selectedFiles = ref<File[]>([])
const fileInput = ref<HTMLInputElement | null>(null)
const folderInput = ref<HTMLInputElement | null>(null)
const busy = ref(false)
const message = ref('')
const error = ref('')
let timer: number | undefined
let polling = false

const selectedJob = computed(() => jobs.value.find(item => item.job_id === selectedJobId.value) || null)
const selectedFile = computed(() => selectedIndex.value === null ? null : selectedJob.value?.files[selectedIndex.value] || null)
const progressPercent = computed(() => selectedJob.value?.file_count
  ? Math.round(selectedJob.value.completed_count / selectedJob.value.file_count * 100) : 0)

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, options)
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(payload.detail || `请求失败 (${response.status})`)
  return payload as T
}

function jsonPost(body: unknown): RequestInit {
  return { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
}

function filename(path: string): string { return path.split(/[\\/]/).pop() || path }
function shortId(id: string): string { return id.replace(/^job_/, '').slice(0, 8) }
function time(value: string): string { return value ? new Date(value).toLocaleString('zh-CN') : '—' }
function statusText(status: string): string {
  return ({ queued: '排队中', running: '解析中', stopping: '正在停止', completed: '已完成',
    finalized: '可使用', review_required: '待审核', failed: '失败', cancelled: '已停止',
    interrupted: '运行中断', pending: '待处理', decided: '已决定', applied: '已应用',
    ocr_requested: '待执行 OCR', ocr_result_ready: 'OCR 已就绪' } as Record<string, string>)[status] || status
}

async function refresh() {
  if (polling) return
  polling = true
  try {
    const previous = selectedJob.value?.files[selectedIndex.value ?? -1]
    const next = await api<Job[]>('/api/jobs')
    jobs.value = next
    if (tab.value === 'history') history.value = await api<HistoryEntry[]>('/api/history')
    if (!selectedJobId.value && next.length) selectedJobId.value = next[0].job_id
    if (tab.value === 'reviews') reviews.value = await api<Review[]>('/api/reviews/ocr')
    if (selectedJobId.value && selectedIndex.value !== null) {
      const job = next.find(item => item.job_id === selectedJobId.value)
      const file = job?.files[selectedIndex.value]
      if (file?.run_dir && file.status !== 'running' &&
          (!detail.value || previous?.status !== file.status || previous?.run_dir !== file.run_dir || previous?.artifact_source !== file.artifact_source)) await loadDetail()
    }
  } catch (cause) {
    error.value = (cause as Error).message
  } finally {
    polling = false
  }
}

async function start() {
  error.value = ''; message.value = ''; busy.value = true
  try {
    let job: Job
    if (inputMode.value === 'path') {
      if (!folderPath.value.trim()) throw new Error('请填写文件或目录路径')
      job = await api<Job>('/api/jobs/directory', jsonPost({ path: folderPath.value.trim(), recursive: recursive.value, workers: workers.value }))
    } else {
      if (!selectedFiles.value.length) throw new Error('请先选择文件或文件夹')
      const data = new FormData()
      selectedFiles.value.forEach(file => data.append('files', file))
      data.append('workers', String(workers.value))
      job = await api<Job>('/api/jobs/upload', { method: 'POST', body: data })
    }
    selectedJobId.value = job.job_id
    selectedIndex.value = null
    detail.value = null
    message.value = `任务 ${shortId(job.job_id)} 已加入队列`
    await refresh()
  } catch (cause) { error.value = (cause as Error).message }
  finally { busy.value = false }
}

async function runAction(action: 'cancel' | 'retry') {
  if (!selectedJob.value) return
  error.value = ''; busy.value = true
  try {
    const job = await api<Job>(`/api/jobs/${selectedJob.value.job_id}/${action}`, { method: 'POST' })
    if (action === 'retry') selectedJobId.value = job.job_id
    message.value = action === 'cancel' ? '停止请求已发送，正在处理的文件完成后会结束' : '失败文件已重新加入队列'
    await refresh()
  } catch (cause) { error.value = (cause as Error).message }
  finally { busy.value = false }
}

async function openBatchDirectory(jobId: string) {
  error.value = ''; message.value = ''
  try {
    const response = await fetch(`/api/jobs/${jobId}/open-dir`, { method: 'POST' })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || '打开批次目录失败')
    message.value = `已在本机打开：${data.path}`
  } catch (cause) { error.value = (cause as Error).message }
}

async function loadDetail() {
  if (!selectedJobId.value || selectedIndex.value === null) return
  try {
    const jobId = selectedJobId.value
    const index = selectedIndex.value
    const next = await api<Detail>(`/api/jobs/${jobId}/documents/${index}`)
    if (selectedJobId.value === jobId && selectedIndex.value === index) detail.value = next
  } catch (cause) { error.value = (cause as Error).message }
}

function openWorkspace() {
  tab.value = 'workspace'
  if (selectedIndex.value !== null) void loadDetail()
}

async function openHistoryEntry(entry: { job_id: string; index: number }) {
  selectedJobId.value = entry.job_id
  selectedIndex.value = entry.index
  detail.value = null
  tab.value = 'workspace'
  await loadDetail()
  await nextTick()
  document.querySelector('.result-card')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

async function openReviewRun(runDir: string) {
  const next = await api<Job[]>('/api/jobs')
  jobs.value = next
  for (const job of next) {
    const index = job.files.findIndex(file => file.run_dir.toLowerCase() === runDir.toLowerCase())
    if (index >= 0) {
      selectedJobId.value = job.job_id
      selectedIndex.value = index
      detail.value = null
      tab.value = 'workspace'
      await loadDetail()
      await nextTick()
      document.querySelector('.result-card')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      return
    }
  }
  error.value = '找不到该审核任务对应的 Web 解析批次。请检查运行目录，或通过 CLI 查看该批次产物。'
}

function chooseJob(job: Job) {
  selectedJobId.value = job.job_id
  selectedIndex.value = null
  detail.value = null
}

function chooseFile(index: number) {
  selectedIndex.value = index
  detail.value = null
  void loadDetail()
}

function filesChosen(event: Event) {
  selectedFiles.value = Array.from((event.target as HTMLInputElement).files || [])
}

onMounted(() => { void refresh(); timer = window.setInterval(() => void refresh(), 1500) })
onUnmounted(() => { if (timer) window.clearInterval(timer) })
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand"><div class="brand-icon">◆</div><div><strong>Document Parser</strong><span>LOCAL WORKSPACE</span></div></div>
      <nav class="nav">
        <button :class="['nav-item', { active: tab === 'workspace' }]" @click="openWorkspace"><span>▦</span> 解析工作台</button>
        <button :class="['nav-item', { active: tab === 'reviews' }]" @click="tab = 'reviews'; refresh()"><span>◇</span> OCR 审核 <i v-if="reviews.filter(x => x.status !== 'applied').length">{{ reviews.filter(x => x.status !== 'applied').length }}</i></button>
        <button :class="['nav-item', { active: tab === 'history' }]" @click="tab = 'history'; refresh()"><span>▤</span> 解析历史</button>
      </nav>
      <div class="sidebar-bottom"><div class="live-dot"></div><div><strong>本地运行</strong><small>文档在本机处理与保存</small></div></div>
    </aside>

    <main class="main">
      <header class="topbar"><div class="breadcrumb">工作空间 <span>/</span> {{ tab === 'workspace' ? '解析工作台' : tab === 'history' ? '解析历史' : 'OCR 审核' }}</div><div class="top-right"><span class="version">v0.6.0</span><span class="avatar">DP</span></div></header>
      <div class="content">
        <div v-if="error" class="notice error"><span>!</span>{{ error }}<button @click="error = ''">×</button></div>
        <div v-if="message" class="notice success"><span>✓</span>{{ message }}<button @click="message = ''">×</button></div>

        <template v-if="tab === 'workspace'">
          <section class="intro"><div><div class="eyebrow">DOCUMENT INTELLIGENCE / 本地解析</div><h1>让每一份文档，<br><em>清晰可见。</em></h1><p>统一解析 PDF、Office、HTML 与 Markdown。实时跟踪进度，查看结构化结果与审核线索。</p></div><div class="intro-mark">D<span>/</span>P</div></section>

          <div class="section-title"><div><span class="section-index">01</span><h2>开始解析</h2></div><p>选择来源，配置批量任务</p></div>
          <section class="setup card">
            <div class="mode-switch"><button :class="{ selected: inputMode === 'upload' }" @click="inputMode = 'upload'">上传文件 / 文件夹</button><button :class="{ selected: inputMode === 'path' }" @click="inputMode = 'path'">本机路径</button></div>
            <div v-if="inputMode === 'upload'" class="upload-box"><div class="upload-icon">↥</div><strong>选择要解析的文档</strong><p>可选择多个文件，或上传整个文件夹</p><div class="upload-buttons"><button class="secondary" @click="fileInput?.click()">选择文件</button><button class="secondary" @click="folderInput?.click()">选择文件夹</button></div><small v-if="selectedFiles.length">已选择 {{ selectedFiles.length }} 个文件 · {{ selectedFiles.slice(0, 3).map(f => f.name).join('、') }}{{ selectedFiles.length > 3 ? ' …' : '' }}</small><input ref="fileInput" type="file" multiple hidden accept=".pdf,.docx,.xlsx,.xlsm,.pptx,.html,.htm,.md,.markdown" @change="filesChosen"><input ref="folderInput" type="file" webkitdirectory multiple hidden @change="filesChosen"></div>
            <div v-else class="path-box"><label>文件或目录路径</label><input v-model="folderPath" class="text-input" placeholder="例如 D:\documents\reports" @keyup.enter="start"><p>本地浏览器无法直接取得文件夹绝对路径；可复制路径到这里，或使用上方“选择文件夹”上传。</p><label class="check"><input v-model="recursive" type="checkbox"> 包含子目录</label></div>
            <div class="setup-footer"><div class="worker-control"><label>并行文件数 <strong>{{ workers }}</strong></label><input v-model.number="workers" type="range" min="1" max="8"><small>推荐从 2 开始，根据机器性能调整</small></div><button class="primary" :disabled="busy" @click="start"><span>开始解析</span><span>↗</span></button></div>
          </section>

          <div class="section-title jobs-heading"><div><span class="section-index">02</span><h2>任务进度</h2></div><p>{{ jobs.length }} 个历史任务</p></div>
          <div class="workspace-grid"><section class="job-list card"><div class="card-heading"><h3>批次记录</h3><button class="text-button" @click="refresh">刷新 ↻</button></div><div v-if="!jobs.length" class="empty small-empty">暂无任务，开始解析后会显示在这里。</div><button v-for="job in jobs" :key="job.job_id" :class="['job-row', { selected: selectedJobId === job.job_id }]" @click="chooseJob(job)"><div class="job-row-top"><strong>批次 {{ shortId(job.job_id) }}</strong><span :class="['status', job.status]">{{ statusText(job.status) }}</span></div><div class="job-row-sub"><span>{{ time(job.created_at) }}</span><span>{{ job.completed_count }}/{{ job.file_count }} 文件</span></div><div class="mini-progress"><div :style="{ width: `${job.file_count ? job.completed_count / job.file_count * 100 : 0}%` }"></div></div></button></section>
          <section class="job-detail card"><template v-if="selectedJob"><div class="card-heading"><div><div class="eyebrow">BATCH {{ shortId(selectedJob.job_id) }}</div><h3>{{ statusText(selectedJob.status) }}</h3></div><span class="progress-number">{{ progressPercent }}<small>%</small></span></div><div class="big-progress"><div :style="{ width: `${progressPercent}%` }"></div></div><div class="metrics"><div><strong>{{ selectedJob.file_count }}</strong><span>总文件</span></div><div><strong>{{ selectedJob.finalized_count }}</strong><span>可使用</span></div><div><strong>{{ selectedJob.review_required_count }}</strong><span>待审核</span></div><div><strong>{{ selectedJob.failed_count }}</strong><span>失败</span></div></div><div class="job-actions"><button v-if="['queued','running','stopping'].includes(selectedJob.status)" class="secondary" :disabled="busy || selectedJob.status === 'stopping'" @click="runAction('cancel')">停止后续文件</button><button v-if="selectedJob.failed_count" class="secondary" :disabled="busy" @click="runAction('retry')">重试失败文件</button><button v-if="selectedJob.report_path" class="text-button" @click="openBatchDirectory(selectedJob.job_id)">打开批次目录</button><span v-if="selectedJob.report_path" class="report-location" :title="selectedJob.report_path">报告已保存 · {{ filename(selectedJob.report_path) }}</span></div><p v-if="selectedJob.error" class="inline-error">{{ selectedJob.error }}</p></template><div v-else class="empty">选择一个批次查看实时状态</div></section></div>

          <template v-if="selectedJob"><div class="section-title"><div><span class="section-index">03</span><h2>文件与结果</h2></div><p>点击文件查看解析产物</p></div><section class="files-card card"><div class="file-table-head"><span>文件</span><span>状态</span><span>Blocks</span><span></span></div><button v-for="(file, index) in selectedJob.files" :key="`${file.source_path}-${index}`" :class="['file-row', { selected: selectedIndex === index }]" @click="chooseFile(index)"><span class="file-name"><span class="file-icon">{{ filename(file.source_path).split('.').pop()?.toUpperCase().slice(0, 4) }}</span><span><strong>{{ filename(file.source_path) }} <span v-if="file.artifact_source === 'ocr_review'" class="file-revision-badge">OCR 已定稿</span><span v-else-if="file.artifact_source === 'structure_review'" class="file-revision-badge">结构审核版</span><span v-else-if="file.artifact_source === 'manual_revision'" class="file-revision-badge">人工修订版</span></strong><small :title="file.source_path">{{ file.source_path }}</small></span></span><span :class="['status', file.status]">{{ statusText(file.status) }}</span><span class="block-num">{{ file.block_count || '—' }}</span><span class="arrow">→</span></button></section>
          <DocumentResult v-if="selectedFile" :job-id="selectedJob.job_id" :index="selectedIndex ?? 0" :file="selectedFile" :detail="detail" @updated="loadDetail" /></template>
        </template>

        <HistoryPanel v-else-if="tab === 'history'" :entries="history" @open="openHistoryEntry" @refresh="refresh" />
        <template v-else><OcrReviewPanel :reviews="reviews" @updated="refresh" @open-run="openReviewRun" /></template>
      </div>
    </main>
  </div>
</template>

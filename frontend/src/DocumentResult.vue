<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

type Block = Record<string, any>
type Pair = { base: Block | null; effective: Block | null }
type BlockPage = { source: string; total: number; offset: number; limit: number; types: string[]; items: Pair[] }
type Task = {
  review_id: string; review_type: string; status: string; reason: string; page_number: number | null
  source_preview: string; object_id: string; source_block: Block | null
  latest_decision: { selected_action: string; corrected_text: string; reviewer: string } | null
}
type StructureState = { tasks: Task[]; finalization: Record<string, any> | null }
type Detail = {
  title?: string; block_count: number; artifact_source?: string
  evaluation: Record<string, any> | null; base_evaluation?: Record<string, any> | null
  manifest: Record<string, any> | null
  ocr_progress?: { task_count: number; applied_count: number; pending_count: number; applied_pages: number[]; status: string } | null
}
type FileItem = { source_path: string; status: string; run_dir: string; error: string }

const props = defineProps<{ jobId: string; index: number; file: FileItem; detail: Detail | null }>()
const emit = defineEmits<{ updated: [] }>()
const view = ref<'blocks' | 'structure'>('blocks')
const blockPage = ref<BlockPage | null>(null)
const structure = ref<StructureState | null>(null)
const blockType = ref('')
const pageNumber = ref('')
const reviewPage = ref(1)
const offset = ref(0)
const reviewer = ref('local_user')
const note = ref('')
const corrections = ref<Record<string, string>>({})
const busy = ref(false)
const error = ref('')
const message = ref('')
const prefix = computed(() => `/api/jobs/${props.jobId}/documents/${props.index}`)
const structurePending = computed(() => structure.value?.tasks.filter(task => task.status === 'pending').length || 0)

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, options)
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(payload.detail || `请求失败 (${response.status})`)
  return payload as T
}

function filename(path: string): string { return path.split(/[\\/]/).pop() || path }
function blockText(block: Block | null): string {
  if (!block) return '此版本中没有对应 Block'
  if (block.text) return String(block.text)
  const table = block.table_data
  if (table) return [table.headers?.join(' | '), ...(table.rows || []).map((row: string[]) => row.join(' | '))].filter(Boolean).join('\n')
  return block.image?.caption || block.image?.ocr_text || '[图片或空内容]'
}
function blockTitle(pair: Pair): string {
  const block = pair.effective || pair.base
  return `${block?.block_type || 'Block'} · ${block?.page_start ? `第 ${block.page_start} 页` : '无页码'}`
}
function changed(pair: Pair): boolean {
  return JSON.stringify(pair.base) !== JSON.stringify(pair.effective)
}
function actionText(action: string): string {
  return ({ approve: '确认结构', correct_text: '修正文案', exclude_block: '排除检索',
    reject_document: '拒绝文档' } as Record<string, string>)[action] || action
}
function statusText(status: string): string {
  return ({ finalized: '可使用', review_required: '待审核', failed: '失败', cancelled: '已停止',
    running: '解析中', queued: '排队中' } as Record<string, string>)[status] || status
}

function rememberCorrections(tasks: Task[]) {
  for (const task of tasks) {
    if (task.latest_decision?.selected_action === 'correct_text') {
      corrections.value[task.review_id] = task.latest_decision.corrected_text
    }
  }
  if (tasks.length) reviewPage.value = tasks[0].page_number || 1
}

async function loadBlocks(reset = false) {
  if (!props.file.run_dir || !props.detail?.evaluation) return
  if (reset) offset.value = 0
  try {
    const params = new URLSearchParams({ offset: String(offset.value), limit: '20' })
    if (blockType.value) params.set('block_type', blockType.value)
    if (pageNumber.value.trim()) params.set('page', pageNumber.value.trim())
    blockPage.value = await api<BlockPage>(`${prefix.value}/blocks?${params}`)
  } catch (cause) { error.value = (cause as Error).message }
}

async function refreshResult() {
  error.value = ''; message.value = ''; busy.value = true
  try {
    await loadBlocks()
    emit('updated')
    message.value = '已重新读取解析结果与 OCR 审核进度。'
  } finally { busy.value = false }
}

async function openRunDirectory() {
  error.value = ''; message.value = ''
  try {
    const result = await api<{ path: string }>(`${prefix.value}/open-dir`, { method: 'POST' })
    message.value = `已在本机打开：${result.path}`
  } catch (cause) { error.value = (cause as Error).message }
}

async function loadStructure() {
  if (!props.file.run_dir || !props.detail?.evaluation) return
  try {
    structure.value = await api<StructureState>(`${prefix.value}/structure`)
    rememberCorrections(structure.value.tasks)
  }
  catch (cause) { error.value = (cause as Error).message }
}

async function prepareStructure() {
  error.value = ''; message.value = ''; busy.value = true
  try {
    structure.value = await api<StructureState>(`${prefix.value}/structure/prepare`, { method: 'POST' })
    rememberCorrections(structure.value.tasks)
    message.value = structure.value.tasks.length ? `已找到 ${structure.value.tasks.length} 项结构审核` : '当前没有结构审核候选'
  } catch (cause) { error.value = (cause as Error).message }
  finally { busy.value = false }
}

async function decide(task: Task, action: string) {
  if (!reviewer.value.trim()) { error.value = '请填写审核人'; return }
  if (action === 'correct_text' && !corrections.value[task.review_id]?.trim()) {
    error.value = '请先填写修正后的内容'; return
  }
  if (action === 'reject_document' && !window.confirm('确认拒绝整份文档？此决定会使最终产物处于阻断状态。')) return
  error.value = ''; message.value = ''; busy.value = true
  try {
    structure.value = await api<StructureState>(`${prefix.value}/structure/${task.review_id}/decision`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, reviewer: reviewer.value, note: note.value,
        corrected_text: action === 'correct_text' ? corrections.value[task.review_id] : '' }),
    })
    message.value = `已记录决定：${actionText(action)}`
  } catch (cause) { error.value = (cause as Error).message }
  finally { busy.value = false }
}

async function finalize() {
  error.value = ''; message.value = ''; busy.value = true
  try {
    const result = await api<Record<string, any>>(`${prefix.value}/structure/finalize`, { method: 'POST' })
    message.value = result.status === 'completed' ? '结构审核已定稿，最终有效版本已更新' : '文档已阻断'
    await loadStructure()
    await loadBlocks(true)
    emit('updated')
  } catch (cause) { error.value = (cause as Error).message }
  finally { busy.value = false }
}

function switchView(next: 'blocks' | 'structure') {
  view.value = next
  if (next === 'structure') void loadStructure()
}

watch([() => props.jobId, () => props.index, () => props.file.run_dir], () => {
  blockPage.value = null; structure.value = null; view.value = 'blocks'
  blockType.value = ''; pageNumber.value = ''; offset.value = 0; error.value = ''; message.value = ''
  void loadBlocks()
})
watch(() => props.detail?.artifact_source, () => { void loadBlocks(true) })
onMounted(() => { void loadBlocks() })
</script>

<template>
  <section class="result-card card">
    <div class="card-heading"><div><div class="eyebrow">PARSE RESULT</div><h3>{{ detail?.title || filename(file.source_path) }}</h3></div><div class="result-heading-actions"><button class="secondary" :disabled="busy || !detail" @click="refreshResult">刷新结果 ↻</button><button v-if="file.run_dir" class="secondary" @click="openRunDirectory">打开产出目录</button><a v-if="file.status === 'finalized'" class="secondary" :href="`${prefix}/export?format=json`">下载 JSON</a><a v-if="file.status === 'finalized'" class="secondary" :href="`${prefix}/export?format=md`">下载 Markdown</a><span class="status" :class="file.status">{{ statusText(file.status) }}</span></div></div>
    <p v-if="file.error" class="inline-error">{{ file.error }}</p>
    <template v-if="detail">
      <div class="quality-summary">
        <div><strong>{{ detail.block_count }}</strong><span>有效 Block</span></div>
        <div><strong>{{ detail.evaluation?.warning_count ?? 0 }}</strong><span>质量警告</span></div>
        <div><strong>{{ detail.evaluation?.manual_review_items?.length ?? 0 }}</strong><span>待审核线索</span></div>
        <div><strong>{{ detail.artifact_source === 'manual_revision' ? '人工修订版' : detail.artifact_source === 'structure_review' ? '结构审核版' : detail.artifact_source === 'ocr_review' ? 'OCR 审核版' : '基础版' }}</strong><span>当前有效版本</span></div>
      </div>
      <div v-if="detail.ocr_progress" class="ocr-progress-note">
        <strong>本次解析 OCR 审核：{{ detail.ocr_progress.applied_count }}/{{ detail.ocr_progress.task_count }} 项已应用</strong>
        <p v-if="detail.ocr_progress.pending_count > 0">还有 {{ detail.ocr_progress.pending_count }} 项未应用。已应用页{{ detail.ocr_progress.applied_pages.length ? `（第 ${detail.ocr_progress.applied_pages.join('、')} 页）` : '' }}保存在审核产物中；整份文档完成审核后才生成 OCR 有效版本。右侧仍显示当前已定稿版本。</p>
        <p v-else-if="detail.ocr_progress.status === 'completed'">本次 OCR 已定稿。{{ detail.artifact_source === 'manual_revision' ? '当前有效结果包含后续人工修订。' : '当前有效版本为 OCR 审核版。' }}{{ detail.evaluation?.warning_count ? `仍有 ${detail.evaluation.warning_count} 条质量提示，可在上方查看；没有待处理的人工审核项时不影响当前版本使用。` : '' }}</p>
        <p v-else>所有决定已应用，最终状态：{{ detail.ocr_progress.status }}。</p>
      </div>
      <div v-if="detail.evaluation?.manual_review_items?.length" class="review-hints">
        <strong>质量与审核线索</strong>
        <p v-for="item in detail.evaluation.manual_review_items.slice(0, 5)" :key="item.review_id">{{ item.page_number ? `第 ${item.page_number} 页 · ` : '' }}{{ item.reason }}</p>
      </div>
      <div class="detail-tabs"><button :class="{ active: view === 'blocks' }" @click="switchView('blocks')">结果对照</button><button :class="{ active: view === 'structure' }" @click="switchView('structure')">结构审核</button></div>
      <p v-if="error" class="inline-error">{{ error }}</p><p v-if="message" class="inline-success">{{ message }}</p>
      <template v-if="view === 'blocks'">
        <div class="result-grid">
          <div class="preview-panel"><div class="panel-title">{{ /\.(docx|xlsx|xlsm|pptx)$/i.test(file.source_path) ? 'Office 内容预览' : '源文件预览' }}</div><iframe v-if="file.source_path.toLowerCase().endsWith('.pdf')" :src="`/api/jobs/${jobId}/source/${index}`" title="PDF 源文件"></iframe><iframe v-else-if="/\.(docx|xlsx|xlsm|pptx)$/i.test(file.source_path)" :src="`/api/jobs/${jobId}/source/${index}/preview`" title="Office 内容预览"></iframe><div v-else class="preview-placeholder"><span>▤</span><strong>{{ filename(file.source_path) }}</strong><p>当前展示解析结构。</p></div></div>
          <div class="blocks-panel comparison-panel">
            <div class="panel-title">结构化 Block 对照 <span>{{ blockPage?.total ?? 0 }} 项</span></div>
            <div class="block-filters"><label>类型<select v-model="blockType" @change="loadBlocks(true)"><option value="">全部</option><option v-for="type in blockPage?.types || []" :key="type" :value="type">{{ type }}</option></select></label><label>页码<input v-model="pageNumber" type="number" min="1" placeholder="全部" @change="loadBlocks(true)" @keyup.enter="loadBlocks(true)"></label><button class="secondary" @click="loadBlocks(true)">筛选</button></div>
            <div v-if="!blockPage?.items.length" class="empty small-empty">没有符合条件的 Block。</div>
            <article v-for="(pair, number) in blockPage?.items || []" :key="`${pair.effective?.block_id || pair.base?.block_id}-${number}`" class="compare-item"><div class="compare-heading"><strong>{{ blockTitle(pair) }}</strong><span v-if="changed(pair)" class="change-badge">内容或审核状态已变化</span><span v-else class="same-badge">一致</span></div><div class="compare-columns"><div><label>基础解析</label><pre>{{ blockText(pair.base) }}</pre><small v-if="pair.base?.quality_flags?.length" class="block-flags">质量标记：{{ pair.base.quality_flags.join('、') }}</small></div><div><label>当前有效结果</label><pre>{{ blockText(pair.effective) }}</pre><small v-if="pair.effective?.quality_flags?.length" class="block-flags">质量标记：{{ pair.effective.quality_flags.join('、') }}</small><small v-if="pair.effective?.metadata?.excluded_from_retrieval" class="block-flags">已排除检索</small></div></div></article>
            <div v-if="blockPage && blockPage.total > blockPage.limit" class="block-pagination"><button class="secondary" :disabled="offset === 0" @click="offset = Math.max(0, offset - 20); loadBlocks()">上一页</button><span>{{ offset + 1 }}–{{ Math.min(offset + 20, blockPage.total) }} / {{ blockPage.total }}</span><button class="secondary" :disabled="offset + 20 >= blockPage.total" @click="offset += 20; loadBlocks()">下一页</button></div>
          </div>
        </div>
      </template>
      <template v-else>
        <div class="structure-toolbar"><div><strong>跨页结构审核</strong><p>核对原文件与候选 Block 后记录决定，全部决定完成后定稿。</p></div><button class="secondary" :disabled="busy" @click="prepareStructure">识别审核候选</button></div>
        <div v-if="file.source_path.toLowerCase().endsWith('.pdf') && structure?.tasks.length" class="structure-source"><div class="panel-title">PDF 原文件 · 第 {{ reviewPage }} 页</div><iframe :src="`/api/jobs/${jobId}/source/${index}#page=${reviewPage}`" title="结构审核源文件"></iframe></div>
        <div v-if="structure?.finalization" class="finalization-state">最终状态：{{ structure.finalization.status === 'completed' ? '已定稿' : '已阻断' }}<span v-if="structure.finalization.evaluation_status"> · {{ structure.finalization.evaluation_status }}</span></div>
        <div v-if="structure && !structure.tasks.length" class="empty small-empty">暂无结构审核候选。</div>
        <div v-for="task in structure?.tasks || []" :key="task.review_id" class="structure-task"><div class="structure-head"><div><span class="status" :class="task.status">{{ task.status === 'pending' ? '待决定' : '已决定' }}</span><strong>{{ task.review_type === 'continued_table' ? '跨页表格' : '跨页段落' }}</strong><span>第 {{ task.page_number || '—' }} 页</span><button v-if="file.source_path.toLowerCase().endsWith('.pdf') && task.page_number" class="text-button" @click="reviewPage = task.page_number || 1">查看该页</button></div><small>{{ task.review_id }}</small></div><p class="structure-reason">{{ task.reason }}</p><div class="structure-preview"><label>待审核 Block · {{ task.object_id }}</label><pre>{{ blockText(task.source_block) }}</pre></div><div v-if="task.latest_decision" class="prior-decision">最近决定：{{ actionText(task.latest_decision.selected_action) }} · {{ task.latest_decision.reviewer }}</div><textarea v-if="task.review_type === 'cross_page_paragraph'" v-model="corrections[task.review_id]" class="correction-input" placeholder="如需修正文案，在此填写完整内容" rows="3"></textarea><div class="structure-actions"><button class="secondary" :disabled="busy" @click="decide(task, 'approve')">确认结构</button><button v-if="task.review_type === 'cross_page_paragraph'" class="secondary" :disabled="busy" @click="decide(task, 'correct_text')">保存修正文案</button><button class="secondary" :disabled="busy" @click="decide(task, 'exclude_block')">排除检索</button><button class="danger-button" :disabled="busy" @click="decide(task, 'reject_document')">拒绝文档</button></div></div>
        <div v-if="structure?.tasks.length" class="structure-footer"><div class="review-form"><label>审核人<input v-model="reviewer" class="text-input" placeholder="填写姓名或工号"></label><label>备注<input v-model="note" class="text-input" placeholder="审核依据"></label></div><div class="structure-finalize"><span>尚有 {{ structurePending }} 项未决定</span><button class="primary compact" :disabled="busy || structurePending > 0" @click="finalize">定稿并生成有效版本</button></div></div>
      </template>
      <div v-if="detail.manifest" class="artifact-strip"><span>运行目录</span><code>{{ file.run_dir }}</code><span>{{ Object.keys(detail.manifest.stages || {}).length }} 个阶段产物</span></div>
    </template>
  </section>
</template>

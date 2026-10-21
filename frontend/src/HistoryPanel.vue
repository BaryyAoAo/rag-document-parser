<script setup lang="ts">
import { computed, ref } from 'vue'

type Entry = {
  job_id: string; index: number; created_at: string; source_path: string
  status: string; block_count: number; run_dir: string; artifact_source?: string
}
const props = defineProps<{ entries: Entry[] }>()
const emit = defineEmits<{ open: [entry: Entry]; refresh: [] }>()
const query = ref('')
const statusFilter = ref('all')
const selected = ref<string[]>([])
const targetFolder = ref('')
const exportFormat = ref<'json' | 'md'>('json')
const busy = ref(false)
const message = ref('')
const error = ref('')
const filtered = computed(() => props.entries.filter(entry => {
  const name = filename(entry.source_path).toLowerCase()
  return (!query.value || name.includes(query.value.toLowerCase()) || entry.job_id.includes(query.value))
    && (statusFilter.value === 'all' || entry.status === statusFilter.value)
}))
function filename(path: string) { return path.split(/[\\/]/).pop() || path }
function key(entry: Entry) { return `${entry.job_id}:${entry.index}` }
function shortId(id: string) { return id.replace(/^job_/, '').slice(0, 8) }
function statusText(status: string) { return ({ finalized: '可使用', review_required: '待审核', failed: '失败', running: '解析中', queued: '排队中', cancelled: '已停止' } as Record<string, string>)[status] || status }
function toggle(entry: Entry) {
  const id = key(entry)
  selected.value = selected.value.includes(id) ? selected.value.filter(item => item !== id) : [...selected.value, id]
}
function toggleVisible() {
  const available = filtered.value.filter(entry => entry.status === 'finalized').map(key)
  const allSelected = available.length > 0 && available.every(id => selected.value.includes(id))
  selected.value = allSelected ? selected.value.filter(id => !available.includes(id)) : [...new Set([...selected.value, ...available])]
}
async function openDirectory(entry: Entry, kind: 'file' | 'batch') {
  error.value = ''; message.value = ''
  try {
    const url = kind === 'file'
      ? `/api/jobs/${entry.job_id}/documents/${entry.index}/open-dir`
      : `/api/jobs/${entry.job_id}/open-dir`
    const response = await fetch(url, { method: 'POST' })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || '打开目录失败')
    message.value = `已在本机打开：${data.path}`
  } catch (cause) { error.value = (cause as Error).message }
}
async function exportSelected() {
  error.value = ''; message.value = ''
  const entries = props.entries.filter(entry => selected.value.includes(key(entry)))
  if (!entries.length) { error.value = '请先勾选可使用的文件'; return }
  if (!targetFolder.value.trim()) { error.value = '请填写已有的目标文件夹绝对路径'; return }
  busy.value = true
  try {
    const response = await fetch('/api/history/export', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entries: entries.map(entry => ({ job_id: entry.job_id, index: entry.index })), target_folder: targetFolder.value.trim(), format: exportFormat.value }),
    })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || '批量导出失败')
    message.value = `已将 ${data.exported_count} 个当前有效结果导出到 ${data.target_folder}`
    selected.value = []
  } catch (cause) { error.value = (cause as Error).message }
  finally { busy.value = false }
}
</script>

<template>
  <div class="history-intro"><div class="eyebrow">DOCUMENT LIBRARY / 本地历史</div><h1>解析历史与结果</h1><p>按文件查看每次解析记录。批次仍用于追踪一次提交的进度；这里可以直接查看和导出文件的当前有效结果。</p></div>
  <section class="card history-card">
    <div class="card-heading"><h3>历史文件 <span class="muted">{{ filtered.length }} 项</span></h3><button class="text-button" @click="emit('refresh')">刷新 ↻</button></div>
    <div class="history-toolbar"><input v-model="query" class="text-input" placeholder="搜索文件名或批次编号"><select v-model="statusFilter" class="text-input"><option value="all">全部状态</option><option value="finalized">可使用</option><option value="review_required">待审核</option><option value="failed">失败</option></select></div>
    <div v-if="error" class="inline-error">{{ error }}</div><div v-if="message" class="inline-success">{{ message }}</div>
    <div class="history-select"><button class="text-button" @click="toggleVisible">全选／取消当前列表的可用文件</button><span>已选择 {{ selected.length }} 个</span></div>
    <div v-if="!filtered.length" class="empty">暂无符合条件的历史文件。</div>
    <div v-for="entry in filtered" :key="key(entry)" class="history-row">
      <input type="checkbox" :checked="selected.includes(key(entry))" :disabled="entry.status !== 'finalized'" :aria-label="`选择 ${filename(entry.source_path)}`" @change="toggle(entry)">
      <div class="history-main"><button class="history-name" @click="emit('open', entry)">{{ filename(entry.source_path) }}</button><small>{{ new Date(entry.created_at).toLocaleString('zh-CN') }} · 批次 {{ shortId(entry.job_id) }} · {{ entry.block_count || 0 }} Blocks <template v-if="entry.artifact_source">· {{ entry.artifact_source === 'ocr_review' ? 'OCR 定稿' : entry.artifact_source === 'manual_revision' ? '人工修订版' : '结构审核版' }}</template></small></div>
      <span class="status" :class="entry.status">{{ statusText(entry.status) }}</span>
      <div class="history-actions"><button class="text-button" @click="emit('open', entry)">结果对照</button><a v-if="entry.status === 'finalized'" :href="`/api/jobs/${entry.job_id}/documents/${entry.index}/export?format=json`">下载 JSON</a><a v-if="entry.status === 'finalized'" :href="`/api/jobs/${entry.job_id}/documents/${entry.index}/export?format=md`">下载 Markdown</a><button v-if="entry.run_dir" class="text-button" @click="openDirectory(entry, 'file')">文件目录</button><button class="text-button" @click="openDirectory(entry, 'batch')">批次目录</button></div>
    </div>
  </section>
  <section class="card history-export"><div><h3>批量导出当前有效结果</h3><p>填写本机已存在的目标文件夹。每份文件按文件名、批次和序号命名；已有文件不会被覆盖。</p></div><div class="history-export-controls"><input v-model="targetFolder" class="text-input" placeholder="例如 D:\exports\parsed_documents"><select v-model="exportFormat" class="text-input"><option value="json">ParsedDocument JSON</option><option value="md">Markdown</option></select><button class="primary compact" :disabled="busy || !selected.length" @click="exportSelected">导出 {{ selected.length }} 个文件</button></div></section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Archive, BookOpen, ChevronDown, ChevronRight, FilePlus2, Plus, RefreshCw, Save, Trash2, Upload, X } from 'lucide-vue-next'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
import type { KnowledgeBaseDetail, KnowledgeBaseSummary, KnowledgeDocumentInput } from '../types'

type EditableDocument = KnowledgeDocumentInput & { key: string }

const items = ref<KnowledgeBaseSummary[]>([])
const details = ref<Record<string, KnowledgeBaseDetail>>({})
const expanded = ref('')
const loading = ref(false)
const saving = ref(false)
const editorOpen = ref(false)
const editingId = ref('')
const name = ref('')
const description = ref('')
const changeSummary = ref('')
const documents = ref<EditableDocument[]>([])
const importInput = ref<HTMLInputElement>()

onMounted(load)

async function load() {
  loading.value = true
  try { items.value = (await api.listKnowledgeBases()).items }
  catch (reason) { ElMessage.error(reason instanceof Error ? reason.message : '知识库加载失败') }
  finally { loading.value = false }
}

async function toggle(item: KnowledgeBaseSummary) {
  expanded.value = expanded.value === item.id ? '' : item.id
  if (expanded.value && !details.value[item.id]) {
    try { details.value[item.id] = await api.getKnowledgeBase(item.id) }
    catch (reason) { ElMessage.error(reason instanceof Error ? reason.message : '版本记录加载失败') }
  }
}

function addDocument(value: KnowledgeDocumentInput = { title: '', content: '' }) {
  documents.value.push({ ...value, key: crypto.randomUUID() })
}

function openCreate() {
  editingId.value = ''
  name.value = ''
  description.value = ''
  changeSummary.value = '创建知识库'
  documents.value = []
  addDocument()
  editorOpen.value = true
}

async function openEdit(item: KnowledgeBaseSummary) {
  try {
    const detail = details.value[item.id] ?? await api.getKnowledgeBase(item.id)
    details.value[item.id] = detail
    const latest = detail.revisions[0]
    editingId.value = item.id
    name.value = item.name
    description.value = item.description
    changeSummary.value = ''
    documents.value = latest.documents.map((document) => ({
      key: crypto.randomUUID(),
      title: document.title,
      content: document.content,
      source_name: document.source_name,
    }))
    editorOpen.value = true
  } catch (reason) { ElMessage.error(reason instanceof Error ? reason.message : '知识内容加载失败') }
}

async function importFiles(event: Event) {
  const files = Array.from((event.target as HTMLInputElement).files ?? [])
  for (const file of files) {
    if (!/\.(txt|md|markdown)$/i.test(file.name)) {
      ElMessage.warning(`${file.name} 不是 TXT 或 Markdown 文件`)
      continue
    }
    addDocument({ title: file.name.replace(/\.(txt|md|markdown)$/i, ''), content: await file.text(), source_name: file.name })
  }
  if (importInput.value) importInput.value.value = ''
}

async function save() {
  const cleaned = documents.value
    .map(({ title, content, source_name }) => ({ title: title.trim(), content: content.trim(), source_name }))
    .filter((document) => document.title || document.content)
  if (!name.value.trim()) { ElMessage.warning('请输入知识库名称'); return }
  if (!cleaned.length || cleaned.some((document) => !document.title || !document.content)) {
    ElMessage.warning('每份知识内容都需要标题和正文')
    return
  }
  saving.value = true
  try {
    const detail = editingId.value
      ? await api.publishKnowledgeRevision(editingId.value, { documents: cleaned, change_summary: changeSummary.value.trim() || '更新知识内容' })
      : await api.createKnowledgeBase({ name: name.value.trim(), description: description.value.trim(), documents: cleaned, change_summary: changeSummary.value.trim() || '创建知识库' })
    details.value[detail.id] = detail
    editorOpen.value = false
    await load()
    ElMessage.success(editingId.value ? '新版本已发布' : '知识库已创建')
  } catch (reason) { ElMessage.error(reason instanceof Error ? reason.message : '知识版本发布失败') }
  finally { saving.value = false }
}

async function archive(item: KnowledgeBaseSummary) {
  try {
    await ElMessageBox.confirm(`归档“${item.name}”？历史任务仍保留已绑定的版本。`, '归档知识库', { type: 'warning', confirmButtonText: '归档', cancelButtonText: '取消' })
    await api.archiveKnowledgeBase(item.id)
    await load()
  } catch (reason) {
    if (reason !== 'cancel' && reason !== 'close') ElMessage.error(reason instanceof Error ? reason.message : '归档失败')
  }
}

function formatTime(value: string) { return new Date(value).toLocaleString('zh-CN', { hour12: false }) }
</script>

<template>
  <main class="workspace library-page">
    <header class="topbar">
      <div><h1>知识库</h1><p>本地业务术语与财务口径</p></div>
      <div class="top-actions"><button class="button" :disabled="loading" @click="load"><RefreshCw :size="16" />刷新</button><button class="button primary" @click="openCreate"><Plus :size="16" />新建知识库</button></div>
    </header>
    <section class="library-content">
      <div class="library-summary"><div><strong>{{ items.length }}</strong><span>个活动知识库</span></div><p>向量模型：qwen3-embedding:0.6b</p></div>
      <div v-if="items.length" class="asset-table knowledge-table">
        <div class="asset-row asset-head"><span>名称</span><span>最新版本</span><span>文档 / 切片</span><span>更新时间</span><span></span></div>
        <template v-for="item in items" :key="item.id">
          <div class="asset-row">
            <button class="asset-name" @click="toggle(item)"><component :is="expanded === item.id ? ChevronDown : ChevronRight" :size="16" /><BookOpen :size="16" /><span><strong>{{ item.name }}</strong><small>{{ item.description || '业务知识' }}</small></span></button>
            <strong>v{{ item.latest_revision }}</strong><span>{{ item.document_count }} / {{ item.chunk_count }}</span><span>{{ formatTime(item.updated_at) }}</span>
            <div class="row-actions"><button class="icon-button" title="编辑并发布新版本" @click="openEdit(item)"><FilePlus2 :size="16" /></button><button class="icon-button" title="归档" @click="archive(item)"><Archive :size="16" /></button></div>
          </div>
          <div v-if="expanded === item.id" class="revision-list knowledge-revisions">
            <div v-for="revision in details[item.id]?.revisions" :key="revision.id" class="revision-row">
              <strong>v{{ revision.revision_number }}</strong><span>{{ revision.change_summary }}</span><span>{{ revision.documents.length }} 份文档</span><code>{{ revision.embedding_model }}</code><span>{{ formatTime(revision.created_at) }}</span><span>{{ revision.content_hash.slice(0, 10) }}</span>
            </div>
          </div>
        </template>
      </div>
      <div v-else-if="!loading" class="history-empty"><BookOpen :size="28" /><p>暂无知识库</p><button class="button primary" @click="openCreate"><Plus :size="16" />新建知识库</button></div>
    </section>

    <div v-if="editorOpen" class="knowledge-editor-backdrop">
      <section class="knowledge-editor" role="dialog" aria-modal="true" aria-labelledby="knowledge-editor-title">
        <header><div><h2 id="knowledge-editor-title">{{ editingId ? '发布知识版本' : '新建知识库' }}</h2><p>{{ editingId ? `当前知识库：${name}` : '本地私有知识' }}</p></div><button class="icon-button" title="关闭" :disabled="saving" @click="editorOpen=false"><X :size="18" /></button></header>
        <div class="knowledge-editor-meta">
          <label><span>名称</span><input v-model="name" :disabled="!!editingId" maxlength="120"></label>
          <label><span>说明</span><input v-model="description" :disabled="!!editingId" maxlength="500"></label>
          <label><span>版本说明</span><input v-model="changeSummary" maxlength="200" placeholder="例如：补充海外业务定义"></label>
        </div>
        <div class="knowledge-editor-toolbar"><span>{{ documents.length }} 份内容</span><div><button class="button" @click="importInput?.click()"><Upload :size="15" />导入文本</button><button class="button" @click="addDocument()"><Plus :size="15" />新增内容</button></div></div>
        <div class="knowledge-document-list">
          <article v-for="(document, index) in documents" :key="document.key" class="knowledge-document-editor">
            <header><span>内容 {{ index + 1 }}</span><button class="icon-button" title="删除内容" :disabled="documents.length === 1" @click="documents.splice(index, 1)"><Trash2 :size="15" /></button></header>
            <input v-model="document.title" maxlength="160" placeholder="标题">
            <textarea v-model="document.content" rows="12" maxlength="200000" placeholder="正文"></textarea>
          </article>
        </div>
        <footer><span>发布时生成向量并创建不可变版本</span><button class="button primary" :disabled="saving" @click="save"><Save :size="16" />{{ saving ? '正在生成向量' : '发布版本' }}</button></footer>
        <input ref="importInput" hidden type="file" multiple accept=".txt,.md,.markdown,text/plain,text/markdown" @change="importFiles">
      </section>
    </div>
  </main>
</template>

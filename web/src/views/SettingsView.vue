<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { CheckCircle2, Cloud, KeyRound, LoaderCircle, Save, Server, SlidersHorizontal, TestTube2, Trash2 } from 'lucide-vue-next'
import { api } from '../api'
import type { ModelSettings, ModelSettingsUpdate } from '../types'
import { MODEL_PROVIDERS, providerPreset } from '../model-providers'

const loading = ref(true)
const saving = ref(false)
const testing = ref(false)
const feedback = ref<{ kind: 'success' | 'error'; message: string } | null>(null)
const keyConfigured = ref(false)
const clearApiKey = ref(false)
const form = reactive<ModelSettingsUpdate>({
  mode: 'ollama', provider: 'ollama', model: 'qwen3.5:4b', api_key: '',
  base_url: 'http://127.0.0.1:11434', temperature: 0, max_tokens: 3072,
  context_window: 32768, clear_api_key: false,
})

const selectedProvider = computed(() => providerPreset(form.provider ?? 'custom'))
const isCloud = computed(() => form.mode === 'openai_compatible')
const providerModels = computed(() => selectedProvider.value?.models ?? [])

function applySettings(value: ModelSettings) {
  Object.assign(form, {
    mode: value.mode,
    provider: value.provider,
    model: value.model,
    api_key: value.api_key,
    base_url: value.base_url,
    temperature: value.temperature,
    max_tokens: value.max_tokens,
    context_window: value.context_window,
    clear_api_key: false,
  })
  keyConfigured.value = value.api_key_configured
  clearApiKey.value = false
}

async function load() {
  loading.value = true
  try { applySettings((await api.getSettings()).llm) }
  catch (error) { showError(error) }
  finally { loading.value = false }
}

function changeProvider(event: Event) {
  const id = (event.target as HTMLSelectElement).value
  const preset = providerPreset(id)
  form.provider = id
  if (!preset) return
  form.mode = preset.mode
  form.base_url = preset.baseUrl
  form.model = preset.defaultModel
  feedback.value = null
}

function clearKey() {
  form.api_key = ''
  form.clear_api_key = true
  clearApiKey.value = true
  keyConfigured.value = false
}

function payload(): { llm: ModelSettingsUpdate } {
  const llm = { ...form, clear_api_key: clearApiKey.value }
  if (isCloud.value) {
    delete llm.context_window
    delete llm.temperature
    delete llm.max_tokens
  }
  return { llm }
}

function validate(): boolean {
  if (!form.model?.trim()) { feedback.value = { kind: 'error', message: '请输入模型名称。' }; return false }
  if (!/^https?:\/\//i.test(form.base_url ?? '')) { feedback.value = { kind: 'error', message: 'API 地址必须以 http:// 或 https:// 开头。' }; return false }
  return true
}

async function testConnection() {
  if (!validate()) return
  testing.value = true
  feedback.value = null
  try {
    const result = await api.testModelConnection(payload())
    feedback.value = { kind: 'success', message: `${result.message}，耗时 ${result.latency_ms} ms。` }
  } catch (error) { showError(error) }
  finally { testing.value = false }
}

async function save() {
  if (!validate()) return
  saving.value = true
  feedback.value = null
  try {
    const result = await api.saveSettings(payload())
    applySettings(result.llm)
    feedback.value = { kind: 'success', message: '模型配置已保存，下一次分析立即使用此模型。' }
    window.dispatchEvent(new CustomEvent('model-settings-updated', { detail: result.llm }))
  } catch (error) { showError(error) }
  finally { saving.value = false }
}

function showError(error: unknown) {
  feedback.value = { kind: 'error', message: error instanceof Error ? error.message : '操作失败，请检查配置。' }
}

onMounted(load)
</script>

<template>
  <main class="settings-page">
    <header class="settings-header">
      <div><span class="eyebrow">系统设置</span><h1>模型 API</h1><p>选择本地模型或配置任意 OpenAI 兼容的云端 API。保存后无需重启服务。</p></div>
      <span class="active-model"><Cloud v-if="isCloud" :size="15" /><Server v-else :size="15" />{{ form.model || '未配置模型' }}</span>
    </header>

    <div v-if="loading" class="settings-loading"><LoaderCircle :size="20" class="spin" />正在读取模型配置</div>
    <form v-else class="settings-form" @submit.prevent="save">
      <section class="settings-card">
        <header><span class="section-icon"><Server :size="17" /></span><div><h2>模型供应商</h2><p>预设会自动填写常用地址，模型名称和地址仍可自由修改。</p></div></header>
        <div class="field-grid two-columns">
          <label><span>供应商</span><select :value="form.provider" @change="changeProvider"><option v-for="item in MODEL_PROVIDERS" :key="item.id" :value="item.id">{{ item.name }}</option></select></label>
          <label><span>模型名称</span><input v-model.trim="form.model" list="provider-models" placeholder="例如 deepseek-flash" autocomplete="off" spellcheck="false" /><datalist id="provider-models"><option v-for="model in providerModels" :key="model" :value="model" /></datalist></label>
        </div>
      </section>

      <section class="settings-card">
        <header><span class="section-icon"><KeyRound :size="17" /></span><div><h2>API 连接</h2><p>密钥只保存在本机服务端，浏览器重新读取时仅显示掩码。</p></div></header>
        <div class="field-grid">
          <label><span>API Base URL</span><input v-model.trim="form.base_url" placeholder="https://api.example.com/v1" autocomplete="off" spellcheck="false" /><small>系统会在此地址后调用 /chat/completions。</small></label>
          <label v-if="isCloud"><span>API Key <em v-if="keyConfigured">已配置</em></span><div class="key-field"><input v-model="form.api_key" type="password" placeholder="输入新的 API Key；留空则保留原密钥" autocomplete="new-password" /><button v-if="keyConfigured" type="button" class="clear-key" @click="clearKey"><Trash2 :size="14" />清除</button></div><small>保存掩码值或留空不会覆盖已有密钥。</small></label>
        </div>
      </section>

      <section v-if="!isCloud" class="settings-card">
        <header><span class="section-icon"><SlidersHorizontal :size="17" /></span><div><h2>生成参数</h2><p>用于控制本地分析节点的基础输出长度和上下文容量；截断修复时系统可自动扩容。</p></div></header>
        <div class="field-grid three-columns">
          <label><span>温度</span><input v-model.number="form.temperature" type="number" min="0" max="2" step="0.1" /></label>
          <label><span>基础输出 Tokens</span><input v-model.number="form.max_tokens" type="number" min="128" max="65536" step="128" /></label>
          <label><span>本地上下文窗口</span><input v-model.number="form.context_window" type="number" min="512" max="1048576" step="512" /></label>
        </div>
      </section>

      <div v-if="feedback" class="settings-feedback" :class="feedback.kind" role="status"><CheckCircle2 v-if="feedback.kind === 'success'" :size="17" /><span v-else class="error-mark">!</span>{{ feedback.message }}</div>
      <footer class="settings-actions"><button type="button" class="button" :disabled="testing || saving" @click="testConnection"><LoaderCircle v-if="testing" :size="16" class="spin" /><TestTube2 v-else :size="16" />{{ testing ? '正在测试' : '测试连接' }}</button><button class="button primary" type="submit" :disabled="saving || testing"><LoaderCircle v-if="saving" :size="16" class="spin" /><Save v-else :size="16" />{{ saving ? '正在保存' : '保存并启用' }}</button></footer>
    </form>
  </main>
</template>

<style scoped>
.settings-page{min-width:0;min-height:100vh;background:#fff;padding:44px clamp(28px,6vw,84px)}
.settings-header{max-width:900px;margin:0 auto 32px;display:flex;align-items:flex-end;justify-content:space-between;gap:24px;border-bottom:1px solid var(--border);padding-bottom:24px}.eyebrow{font-size:11px;color:#999}.settings-header h1{margin:6px 0 8px;font-size:27px;color:#222}.settings-header p{margin:0;color:var(--muted);font-size:13px}.active-model{max-width:320px;padding:8px 11px;border:1px solid var(--border);border-radius:6px;color:#555;font:12px var(--mono);display:flex;align-items:center;gap:7px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.settings-form,.settings-loading{max-width:900px;margin:auto}.settings-loading{min-height:300px;display:flex;align-items:center;justify-content:center;gap:9px;color:var(--muted)}
.settings-card{border-bottom:1px solid var(--border);padding:25px 0 29px}.settings-card>header{display:flex;gap:12px;margin-bottom:21px}.section-icon{width:32px;height:32px;display:grid;place-items:center;border:1px solid var(--border);border-radius:6px;background:#fafafa;color:#555;flex:none}.settings-card h2{margin:1px 0 4px;font-size:15px;color:#333}.settings-card header p{margin:0;color:#888;font-size:11px}
.field-grid{display:grid;gap:18px}.two-columns{grid-template-columns:1fr 1fr}.three-columns{grid-template-columns:repeat(3,1fr)}label>span{display:flex;align-items:center;gap:8px;margin-bottom:7px;color:#555;font-size:11px;font-weight:600}label em{padding:1px 6px;border-radius:99px;background:#eee;color:#666;font-size:9px;font-style:normal}input,select{width:100%;height:40px;padding:0 11px;border:1px solid #d8d8d8;border-radius:6px;background:#fff;color:#333;font:12px var(--mono);outline:none}input:focus,select:focus{border-color:#777;box-shadow:0 0 0 2px #0000000c}label small{display:block;margin-top:6px;color:#999;font-size:10px}.key-field{display:flex;gap:8px}.key-field input{flex:1}.clear-key{height:40px;padding:0 11px;border:1px solid var(--border);border-radius:6px;background:#fff;color:#777;display:flex;align-items:center;gap:5px;font-size:11px}.clear-key:hover{color:#333;background:#f5f5f5}
.settings-feedback{margin-top:20px;padding:11px 13px;border:1px solid var(--border);border-radius:6px;background:#fafafa;display:flex;align-items:center;gap:8px;font-size:12px}.settings-feedback.success{color:#46604f}.settings-feedback.error{color:#93443f;border-color:#ead1cf;background:#fffafa}.error-mark{width:17px;height:17px;border-radius:50%;background:#93443f;color:#fff;display:grid;place-items:center;font:bold 11px sans-serif}.settings-actions{padding-top:22px;display:flex;justify-content:flex-end;gap:9px}.spin{animation:spin .9s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
@media(max-width:900px){.settings-page{padding:28px 22px}.settings-header{align-items:flex-start;flex-direction:column}.two-columns,.three-columns{grid-template-columns:1fr}.active-model{max-width:100%}}
</style>

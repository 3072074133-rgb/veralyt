export interface ModelProviderPreset {
  id: string
  name: string
  mode: 'ollama' | 'openai_compatible'
  baseUrl: string
  defaultModel: string
  models: string[]
}

export const MODEL_PROVIDERS: ModelProviderPreset[] = [
  {
    id: 'ollama', name: 'Ollama（本地）', mode: 'ollama',
    baseUrl: 'http://127.0.0.1:11434', defaultModel: 'qwen3.5:4b',
    models: ['qwen3.5:4b', 'qwen3:8b', 'llama3.2:latest'],
  },
  {
    id: 'deepseek', name: 'DeepSeek', mode: 'openai_compatible',
    baseUrl: 'https://api.deepseek.com', defaultModel: 'deepseek-flash',
    models: ['deepseek-flash'],
  },
  {
    id: 'openai', name: 'OpenAI', mode: 'openai_compatible',
    baseUrl: 'https://api.openai.com/v1', defaultModel: 'gpt-5.6-terra',
    models: ['gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna'],
  },
  {
    id: 'qwen', name: '通义千问', mode: 'openai_compatible',
    baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', defaultModel: 'qwen3.7-plus',
    models: ['qwen3.8-max', 'qwen3.8-flash', 'qwen3.7-plus', 'qwen3.7-flash'],
  },
  {
    id: 'kimi', name: 'Kimi', mode: 'openai_compatible',
    baseUrl: 'https://api.moonshot.ai/v1', defaultModel: 'kimi-k3',
    models: ['kimi-k3', 'kimi-k2.7-code', 'kimi-k2.7-code-highspeed', 'kimi-k2.6'],
  },
  {
    id: 'custom', name: '自定义 OpenAI 兼容 API', mode: 'openai_compatible',
    baseUrl: '', defaultModel: '', models: [],
  },
]

export const providerPreset = (id: string) => MODEL_PROVIDERS.find(item => item.id === id)

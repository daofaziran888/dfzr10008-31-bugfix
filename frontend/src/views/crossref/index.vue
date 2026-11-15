<template>
  <section class="page" data-module="crossref">
    <header class="page-head">
      <div>
        <h2>跨源数据关联管理</h2>
        <p class="page-desc">维护跨源数据关联的字段映射、关联类型与时效配置，为跨源数据关联提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记关联规则</button>
        <button class="btn" type="button" @click="exportRows">导出跨源数据关联清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDetail(row)">详情</button>
            <button
              v-for="action in availableActions(row)"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无跨源数据关联数据，可先登记关联规则</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条跨源数据关联记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <div v-if="createVisible" class="modal-mask" @click.self="createVisible = false">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记关联规则</h3>
        <label v-for="field in editableFields" :key="field" class="form-item">
          <span>{{ field }}<em v-if="requiredFields.includes(field)">*</em></span>
          <select v-if="field === '关联类型'" v-model="createForm[field]">
            <option value="left join">left join</option>
            <option value="inner join">inner join</option>
          </select>
          <select v-else-if="field === '时效控制'" v-model="createForm[field]">
            <option value="实时">实时</option>
            <option value="小时级">小时级</option>
            <option value="日级">日级</option>
            <option value="周级">周级</option>
          </select>
          <input v-else v-model="createForm[field]" :placeholder="`请输入${field}`" />
        </label>
        <p v-if="createError" class="error-text">{{ createError }}</p>
        <div class="modal-actions">
          <button class="btn primary" type="submit">保存登记</button>
          <button class="btn ghost" type="button" @click="createVisible = false">取消</button>
        </div>
      </form>
    </div>

    <div v-if="detailVisible" class="modal-mask" @click.self="closeDetail">
      <div class="modal drawer">
        <h3>关联规则详情 · {{ detailForm['规则编号'] || detailRow?.id }}</h3>

        <section class="detail-section">
          <h4>字段匹配</h4>
          <div class="form-grid">
            <label v-for="field in editableFields" :key="field" class="form-item">
              <span>{{ field }}</span>
              <select v-if="field === '关联类型'" v-model="detailForm[field]">
                <option value="left join">left join</option>
                <option value="inner join">inner join</option>
              </select>
              <select v-else-if="field === '时效控制'" v-model="detailForm[field]">
                <option value="实时">实时</option>
                <option value="小时级">小时级</option>
                <option value="日级">日级</option>
                <option value="周级">周级</option>
              </select>
              <input v-else v-model="detailForm[field]" :placeholder="`请输入${field}`" />
            </label>
            <label class="form-item">
              <span>规则状态</span>
              <input :value="String(detailRow?.['规则状态'] ?? detailRow?.status ?? '')" disabled />
            </label>
          </div>
          <div class="modal-actions">
            <button class="btn" type="button" @click="swapSources">交换主数据源与关联数据源</button>
            <button class="btn primary" type="button" @click="saveDetail">保存修改</button>
            <button class="btn" type="button" @click="exportResult">导出关联结果</button>
          </div>
          <p v-if="detailError" class="error-text">{{ detailError }}</p>
        </section>

        <section class="detail-section">
          <h4>当前跨源关联结果</h4>
          <template v-if="currentResult">
            <p class="result-meta">
              版本 v{{ currentResult.version }} · 生成于 {{ currentResult.generated_at }} ·
              时效控制 {{ currentResult.snapshot?.['时效控制'] ?? '—' }} · {{ currentResult.row_count }} 行
            </p>
            <table class="data-table">
              <thead>
                <tr><th v-for="key in resultColumns" :key="key">{{ key }}</th></tr>
              </thead>
              <tbody>
                <tr v-for="(resultRow, index) in currentResult.rows" :key="index">
                  <td v-for="key in resultColumns" :key="key">{{ resultRow[key] ?? '—' }}</td>
                </tr>
              </tbody>
            </table>
          </template>
          <p v-else class="empty-state">规则尚未启用，暂无跨源关联结果</p>
        </section>

        <section class="detail-section">
          <h4>历史时效结果（可追溯）</h4>
          <table v-if="historyResults.length" class="data-table">
            <thead>
              <tr><th>版本</th><th>时效控制</th><th>关联类型</th><th>结果字段</th><th>生成时间</th><th>行数</th></tr>
            </thead>
            <tbody>
              <tr v-for="item in historyResults" :key="item.id">
                <td>v{{ item.version }}</td>
                <td>{{ item.snapshot?.['时效控制'] ?? '—' }}</td>
                <td>{{ item.snapshot?.['关联类型'] ?? '—' }}</td>
                <td>{{ (item.snapshot?.['结果字段'] ?? []).join('、') || '—' }}</td>
                <td>{{ item.generated_at }}</td>
                <td>{{ item.row_count }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="empty-state">暂无历史时效结果</p>
        </section>

        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="closeDetail">关闭</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/crossref'
const columns = ["规则编号", "主数据源", "关联数据源", "关联字段", "关联类型", "结果字段", "时效控制", "规则状态"]
const editableFields = ["规则编号", "主数据源", "关联数据源", "关联字段", "关联类型", "结果字段", "时效控制"]
const requiredFields = ["规则编号", "主数据源", "关联数据源"]
// 动作与状态的按序对应：草稿→启用，已启用→暂停，已暂停→关闭
const nextActions: Record<string, string[]> = {
  草稿: ['启用关联'],
  已启用: ['暂停关联'],
  已暂停: ['关闭关联'],
  已关闭: [],
}
const stats = ref([
  { label: '关联规则总数', value: 0 },
  { label: '已启用关联', value: 0 },
  { label: '已暂停关联', value: 0 },
])

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const createVisible = ref(false)
const createForm = ref<Record<string, string>>({})
const createError = ref('')

const detailVisible = ref(false)
const detailRow = ref<Row | null>(null)
const detailForm = ref<Record<string, string>>({})
const detailError = ref('')
const currentResult = ref<Row | null>(null)
const historyResults = ref<Row[]>([])

const resultColumns = computed(() => {
  const first = currentResult.value?.rows?.[0]
  return first ? Object.keys(first) : []
})

function availableActions(row: Row) {
  return nextActions[String(row.status ?? row['规则状态'] ?? '')] ?? []
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  createForm.value = { 关联类型: 'left join', 时效控制: '日级' }
  createError.value = ''
  createVisible.value = true
}

async function submitCreate() {
  createError.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: createForm.value }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message ?? payload.detail ?? '关联规则登记失败')
    }
    createVisible.value = false
    await reload()
  } catch (error) {
    createError.value = error instanceof Error ? error.message : '关联规则登记失败'
  }
}

async function openDetail(row: Row) {
  detailError.value = ''
  detailVisible.value = true
  await refreshDetail(Number(row.id))
}

function closeDetail() {
  detailVisible.value = false
  detailRow.value = null
}

async function refreshDetail(id: number) {
  const [detailResp, resultsResp] = await Promise.all([
    request(`${ENDPOINT}/${id}`),
    request(`${ENDPOINT}/${id}/results`),
  ])
  if (!detailResp.ok) {
    detailError.value = '关联规则详情读取失败'
    return
  }
  const detail = await detailResp.json()
  detailRow.value = detail
  detailForm.value = Object.fromEntries(editableFields.map((field) => [field, detail[field] ?? '']))
  if (resultsResp.ok) {
    const payload = await resultsResp.json()
    currentResult.value = payload.current ?? null
    historyResults.value = payload.history ?? []
  }
}

function swapSources() {
  const form = detailForm.value
  const primary = form['主数据源']
  form['主数据源'] = form['关联数据源']
  form['关联数据源'] = primary
  const [left = '', right = ''] = String(form['关联字段'] ?? '').split('=')
  form['关联字段'] = `${right.trim()}=${left.trim()}`
}

async function saveDetail() {
  if (!detailRow.value) return
  detailError.value = ''
  try {
    const response = await request(`${ENDPOINT}/${detailRow.value.id}`, {
      method: 'PUT',
      body: JSON.stringify({ values: detailForm.value }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message ?? payload.detail ?? '关联规则保存失败')
    }
    // 保存后重新拉取详情与结果：交换主次数据源后必须看到重算结果，不能残留旧属性
    await refreshDetail(Number(detailRow.value.id))
    await reload()
  } catch (error) {
    detailError.value = error instanceof Error ? error.message : '关联规则保存失败'
  }
}

function exportResult() {
  if (!detailRow.value) return
  window.open(`${ENDPOINT}/${detailRow.value.id}/export/download`, '_blank')
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message ?? payload.detail ?? '跨源数据关联动作未生效，请稍后重试')
    }
    await reload()
    if (detailVisible.value && detailRow.value?.id === row.id) {
      await refreshDetail(Number(row.id))
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '跨源数据关联操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('关联规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value = [
      { label: '关联规则总数', value: total.value },
      { label: '已启用关联', value: rows.value.filter((row) => row.status === '已启用').length },
      { label: '已暂停关联', value: rows.value.filter((row) => row.status === '已暂停').length },
    ]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '跨源数据关联列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.35);
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding: 48px 16px;
  z-index: 20;
}
.modal {
  background: #fff;
  border-radius: 10px;
  padding: 20px;
  width: 520px;
  max-height: 84vh;
  overflow: auto;
  box-shadow: 0 12px 32px rgba(15, 23, 42, 0.18);
}
.modal.drawer {
  width: 760px;
}
.modal h3 {
  margin: 0 0 12px;
  font-size: 16px;
}
.detail-section {
  border-top: 1px solid var(--border);
  padding-top: 12px;
  margin-top: 12px;
}
.detail-section h4 {
  margin: 0 0 10px;
  font-size: 14px;
}
.form-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 10px 14px;
}
.form-item span {
  display: block;
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 4px;
}
.form-item em {
  color: #b42318;
  font-style: normal;
}
.form-item input,
.form-item select {
  width: 100%;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 13px;
}
.modal-actions {
  display: flex;
  gap: 8px;
  margin-top: 14px;
}
.result-meta {
  font-size: 12px;
  color: var(--muted);
  margin: 0 0 8px;
}
</style>

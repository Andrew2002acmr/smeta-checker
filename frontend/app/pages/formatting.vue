<script setup lang="ts">
import type { FormatDto } from '~/generated/types.gen'
import { collectDropFiles } from '~/utils/dropFiles'

const backend = useBackend()
const fileInput = ref<HTMLInputElement | null>(null)
const file = ref<File | null>(null)
const fileName = ref('')
const sheet = ref('')
const result = ref<FormatDto | null>(null)
const checking = ref(false)
const collecting = ref(false)
const dragActive = ref(false)
const error = ref('')
const maximumBytes = 100 * 1024 * 1024
let epoch = 0
let dragDepth = 0

const sheetItems = computed(() =>
  (result.value?.sheets ?? []).map((name) => ({ label: name, value: name })),
)
const failedRows = computed(() => {
  const rows = new Set<string>()
  for (const issue of result.value?.issues ?? []) {
    const row = issue.coord.match(/\d+/)?.[0]
    if (row && (issue.type === 'ROW_POS' || issue.type === 'ROW_INVALID')) rows.add(row)
  }
  return rows.size
})
const compliance = computed(() => {
  const checks = result.value?.check_count ?? 0
  if (!checks || !result.value) return null
  const passed = Math.max(
    0,
    checks - (result.value.error_count ?? 0) - (result.value.warning_count ?? 0),
  )
  return Math.round((passed / checks) * 100)
})

function resetResult() {
  epoch++
  checking.value = false
  result.value = null
  error.value = ''
}

function selectFile(candidate: File) {
  if (!/\.xlsx$/i.test(candidate.name)) {
    error.value = 'Для проверки оформления выберите ведомость в формате XLSX.'
    return
  }
  if (candidate.size > maximumBytes) {
    error.value = 'Файл больше 100 МБ. Выберите файл меньшего размера.'
    return
  }
  file.value = candidate
  fileName.value = candidate.name
  sheet.value = ''
  resetResult()
  void check()
}

function onFilePicked(event: Event) {
  const input = event.target as HTMLInputElement | null
  const picked = input?.files?.[0] ?? null
  if (input) input.value = ''
  if (picked) selectFile(picked)
}

function clearFile() {
  file.value = null
  fileName.value = ''
  sheet.value = ''
  resetResult()
}

async function check() {
  if (!file.value || checking.value) return
  const current = ++epoch
  checking.value = true
  error.value = ''
  try {
    const form = new FormData()
    form.append('file', file.value)
    const value = await backend.formattingUpload(form, sheet.value.trim())
    if (current !== epoch) return
    result.value = value
    if (!sheet.value && value.sheet) sheet.value = value.sheet
  } catch (cause) {
    if (current !== epoch) return
    error.value = readableError(cause)
  } finally {
    if (current === epoch) checking.value = false
  }
}

function onDragEnter(event: DragEvent) {
  event.preventDefault()
  dragDepth++
  dragActive.value = true
}

function onDragOver(event: DragEvent) {
  event.preventDefault()
  dragActive.value = true
}

function onDragLeave(event: DragEvent) {
  event.preventDefault()
  dragDepth = Math.max(0, dragDepth - 1)
  if (!dragDepth) dragActive.value = false
}

async function onDrop(event: DragEvent) {
  event.preventDefault()
  dragDepth = 0
  dragActive.value = false
  const source = event.dataTransfer
  if (!source || collecting.value) return
  collecting.value = true
  error.value = ''
  try {
    const files = await collectDropFiles(source)
    const workbooks = files.filter((item) => /\.xlsx$/i.test(item.name))
    if (files.length !== 1 || workbooks.length !== 1) {
      error.value = 'Перетащите один файл ВОР в формате XLSX.'
      return
    }
    selectFile(workbooks[0]!)
  } catch {
    error.value = 'Не удалось прочитать перетащенный файл. Выберите его кнопкой.'
  } finally {
    collecting.value = false
  }
}

watch(
  sheet,
  (value, old) => {
    if (!file.value || value === old || value === result.value?.sheet) return
    resetResult()
    void check()
  },
  { flush: 'sync' },
)

onBeforeUnmount(() => {
  epoch++
})
</script>

<template>
  <div>
    <WorkspaceHeader active="format" />
    <main id="main-content" tabindex="-1" class="shell formatting-page" data-ui="vor-formatting">
      <div class="page-heading formatting-heading">
        <div>
          <h1 class="page-title">Оформление ВОР</h1>
          <p class="muted">Проверьте структуру и оформление ведомости по шаблону КДФП.</p>
        </div>
        <UButton
          v-if="file"
          color="neutral"
          variant="outline"
          icon="i-lucide-refresh-cw"
          :loading="checking"
          :disabled="checking"
          @click="check()"
        >
          Проверить снова
        </UButton>
      </div>

      <p v-if="error" class="error-box" role="alert">{{ error }}</p>

      <section
        v-if="!file"
        class="format-drop"
        :class="{ 'format-drop-active': dragActive }"
        aria-label="Загрузить ВОР для проверки оформления"
        @dragenter="onDragEnter"
        @dragover="onDragOver"
        @dragleave="onDragLeave"
        @drop="onDrop"
      >
        <UIcon name="i-lucide-upload-cloud" class="format-drop-icon" aria-hidden="true" />
        <h2>Перетащите файл ВОР сюда</h2>
        <p class="muted">Формат XLSX · до 100 МБ · файл не сохраняется в проекте</p>
        <input
          ref="fileInput"
          type="file"
          accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
          class="format-file-input"
          aria-label="Выбрать файл ВОР XLSX"
          @change="onFilePicked"
        />
        <UButton
          color="neutral"
          variant="outline"
          icon="i-lucide-file-spreadsheet"
          :loading="collecting"
          @click="fileInput?.click()"
        >
          Добавить ВОР
        </UButton>
      </section>

      <template v-else>
        <section class="format-file-line" aria-label="Выбранный файл">
          <UIcon name="i-lucide-file-spreadsheet" aria-hidden="true" />
          <span class="format-file-name" :title="fileName">{{ fileName }}</span>
          <span v-if="checking" class="muted" role="status">Проверяем…</span>
          <UButton
            color="neutral"
            variant="ghost"
            size="sm"
            icon="i-lucide-x"
            aria-label="Выбрать другой файл"
            @click="clearFile"
          >
            Другой файл
          </UButton>
        </section>

        <div v-if="result" class="format-result-tools">
          <USelect
            v-if="sheetItems.length > 1"
            v-model="sheet"
            :items="sheetItems"
            aria-label="Лист ведомости для проверки оформления"
            class="format-sheet-select"
          />
          <p v-else class="muted">Лист: {{ result.sheet || '—' }}</p>
        </div>

        <section v-if="result" class="format-kpis" aria-label="Сводка проверки оформления">
          <div class="format-kpi format-kpi-main">
            <span>Соответствие</span>
            <strong>{{ compliance === null ? '—' : `${compliance}%` }}</strong>
            <small>{{ result.check_count }} проверок формата</small>
          </div>
          <div class="format-kpi">
            <span>Всего позиций</span>
            <strong>{{ result.position_count }}</strong>
            <small>{{ failedRows }} с замечаниями</small>
          </div>
          <div class="format-kpi format-kpi-error">
            <span>Ошибки</span>
            <strong>{{ result.error_count }}</strong>
            <small>Требуют исправления</small>
          </div>
          <div class="format-kpi format-kpi-warning">
            <span>Замечания</span>
            <strong>{{ result.warning_count }}</strong>
            <small>Проверьте вручную</small>
          </div>
        </section>

        <p v-if="checking && !result" class="empty" role="status">Проверяем оформление ВОР…</p>
        <FormatCheckResult
          v-else-if="result"
          :result="result"
          :filename="fileName"
          :upload-file="file"
        />
      </template>
    </main>
  </div>
</template>

<style scoped>
.formatting-page {
  padding-top: 24px;
}
.formatting-heading {
  align-items: center;
  margin-bottom: 24px;
}
.formatting-heading .page-title {
  margin-bottom: 6px;
}
.format-drop {
  min-height: 300px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 32px 20px;
  border: 1px dashed var(--ui-border-accented);
  border-radius: 12px;
  background: var(--ui-bg);
  text-align: center;
  transition:
    border-color 120ms ease,
    background-color 120ms ease;
}
.format-drop-active {
  border-color: var(--ui-primary);
  background: var(--app-selected-bg);
}
.format-drop-icon {
  width: 40px;
  height: 40px;
  color: var(--ui-primary);
}
.format-drop h2 {
  margin: 0;
  font-size: 18px;
  font-weight: 650;
}
.format-drop p {
  margin: 0 0 8px;
}
.format-file-input {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
}
.format-file-line {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 52px;
  padding: 8px 12px;
  border: 1px solid var(--ui-border);
  border-radius: 8px;
  background: var(--ui-bg);
}
.format-file-name {
  min-width: 0;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 600;
}
.format-result-tools {
  display: flex;
  justify-content: flex-end;
  margin-block: 16px 12px;
}
.format-result-tools p {
  margin: 0;
}
.format-sheet-select {
  min-width: 240px;
}
.format-kpis {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  margin-bottom: 20px;
  border: 1px solid var(--ui-border);
  border-radius: 8px;
  background: var(--ui-bg);
}
.format-kpi {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 4px;
  padding: 16px 20px;
  border-left: 1px solid var(--ui-border-muted);
}
.format-kpi:first-child {
  border-left: 0;
}
.format-kpi span {
  color: var(--ui-text-muted);
  font-size: 13px;
}
.format-kpi strong {
  color: var(--ui-text-highlighted);
  font-size: 26px;
  font-weight: 600;
  line-height: 1.15;
  font-variant-numeric: tabular-nums;
}
.format-kpi small {
  color: var(--ui-text-muted);
  font-size: 12px;
}
.format-kpi-error strong {
  color: var(--app-error-text);
}
.format-kpi-warning strong {
  color: var(--app-warning-text);
}
@media (max-width: 800px) {
  .format-kpis {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .format-kpi:nth-child(3) {
    border-left: 0;
    border-top: 1px solid var(--ui-border-muted);
  }
  .format-kpi:nth-child(4) {
    border-top: 1px solid var(--ui-border-muted);
  }
}
@media (max-width: 600px) {
  .formatting-heading {
    align-items: flex-start;
  }
  .formatting-heading > :deep(button) {
    align-self: stretch;
  }
  .format-drop {
    min-height: 260px;
  }
  .format-kpi {
    padding: 13px 12px;
  }
  .format-kpi strong {
    font-size: 23px;
  }
}
</style>

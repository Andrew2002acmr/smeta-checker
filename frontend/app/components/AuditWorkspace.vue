<script setup lang="ts">
import type { SplitterItem } from '@nuxt/ui'
import type {
  WorkspaceDto,
  VersionDto,
  StructureDto,
  StructureNodeDto,
} from '~/generated/types.gen'
import {
  queueColor,
  queueLabel,
  quantityDelta,
  quantityWithUnit,
  unitText,
} from '~/utils/presentation'

const props = defineProps<{
  runId: string
  positionIndex: number
  documents: VersionDto[]
  positionCount?: number
  readyPositions?: number[]
}>()
const emit = defineEmits<{ close: []; navigate: [index: number] }>()
const backend = useBackend()
const open = ref(true)
const workspace = ref<WorkspaceDto | null>(null)
const busy = ref(true)
const error = ref('')
const version = ref('')
const page = ref(1)
const sourceHighlight = ref<number[] | null>(null)
const structure = ref<StructureDto | null>(null)
const structureError = ref('')
const selectedNode = ref('')
const outlineOpen = ref(false)
const search = ref('')
const searchPages = ref<number[]>([])
const mobileTab = ref<'vor' | 'pdf'>('vor')
const isNarrow = ref(false)
const pdfPanel = ref<{ find: (query: string) => Promise<number[]> }>()
const dossierItems: SplitterItem[] = [
  { slot: 'pdf', minSize: 30, defaultSize: 62 },
  { slot: 'dossier', minSize: 25, defaultSize: 38 },
]
const pdfs = computed(() => props.documents.filter((document) => document.kind === 'pdf'))
const previousReady = computed(() =>
  props.readyPositions
    ? (props.readyPositions.filter((i) => i < props.positionIndex).toSorted((a, b) => b - a)[0] ??
      null)
    : props.positionIndex > 0
      ? props.positionIndex - 1
      : null,
)
const nextReady = computed(() =>
  props.readyPositions
    ? (props.readyPositions.filter((i) => i > props.positionIndex).toSorted((a, b) => a - b)[0] ??
      null)
    : typeof props.positionCount === 'number' && props.positionIndex + 1 < props.positionCount
      ? props.positionIndex + 1
      : null,
)
const headerDescription = computed(() =>
  workspace.value
    ? `${workspace.value.row.number} · ${workspace.value.row.name}`
    : 'Открываем позицию…',
)
const compareDelta = computed(() => {
  const row = workspace.value?.row
  const declared = unitText(row?.unit).toLocaleLowerCase('ru-RU')
  const found = unitText(row?.found_unit).toLocaleLowerCase('ru-RU')
  return declared && found && declared === found
    ? quantityDelta(row?.declared_quantity, row?.found_quantity)
    : null
})
const verdictDirection = computed(() => {
  const row = workspace.value?.row
  const delta = compareDelta.value
  if (!row || row.queue !== 'fix' || /в самой ВОР/i.test(row.problem) || !delta || delta === '±0')
    return ''
  const amount = delta.slice(1) + (unitText(row.unit) ? ` ${unitText(row.unit)}` : '')
  return delta.startsWith('+') ? `ПД превышает ВОР на ${amount}` : `ВОР превышает ПД на ${amount}`
})
const extraDetails = computed(() => {
  const row = workspace.value?.row
  if (!row?.details) return ''
  const problem = (row.problem || '').replace(/\s+/g, ' ').trim()
  const details = row.details.replace(/\s+/g, ' ').trim()
  return !details || details === problem || problem.includes(details) || details.includes(problem)
    ? ''
    : row.details
})
let active = true
let positionEpoch = 0
let structureEpoch = 0
let mediaQuery: MediaQueryList | null = null
function onMediaChange(event: MediaQueryListEvent) {
  isNarrow.value = event.matches
}
async function loadStructure() {
  const current = ++structureEpoch
  structure.value = null
  structureError.value = ''
  if (!version.value) return
  try {
    const result = await backend.structure(version.value)
    if (active && current === structureEpoch) structure.value = result
  } catch (e) {
    if (active && current === structureEpoch) structureError.value = readableError(e)
  }
}
async function loadPosition() {
  const current = ++positionEpoch
  busy.value = true
  workspace.value = null
  error.value = ''
  searchPages.value = []
  selectedNode.value = ''
  try {
    const result = await backend.workspace(props.runId, props.positionIndex)
    if (!active || current !== positionEpoch) return
    workspace.value = result
    version.value = result.evidence[0]?.version_id || pdfs.value[0]?.id || ''
    page.value = result.evidence[0]?.page || 1
  } catch (e) {
    if (active && current === positionEpoch) error.value = readableError(e)
  } finally {
    if (active && current === positionEpoch) busy.value = false
  }
}
function selectNode(node: StructureNodeDto) {
  page.value = node.physical_start
  selectedNode.value = node.id
  outlineOpen.value = false
}
function openProjectSource(documentId: string, physicalPage: number, highlight?: number[] | null) {
  version.value = documentId
  changePage(physicalPage)
  sourceHighlight.value = highlight ?? null
  mobileTab.value = 'pdf'
}
function changePage(value: number) {
  sourceHighlight.value = null
  page.value = value
  selectedNode.value = ''
}
async function find() {
  if (pdfPanel.value) searchPages.value = await pdfPanel.value.find(search.value)
}
async function downloadSource() {
  if (!workspace.value) return
  try {
    await backend.download(
      backend.contentUrl(workspace.value.vor_version_id) + '?download=true',
      'ВОР.' + (workspace.value.excel ? 'xlsx' : 'csv'),
    )
  } catch (e) {
    error.value = readableError(e)
  }
}
watch(version, loadStructure)
watch(
  () => props.positionIndex,
  () => {
    sourceHighlight.value = null
    loadPosition()
  },
)
watch(
  () => props.readyPositions,
  (ready) => {
    if (!workspace.value && !busy.value && ready?.includes(props.positionIndex)) void loadPosition()
  },
)
watch(open, (value) => {
  if (!value) emit('close')
})
onMounted(async () => {
  mediaQuery = window.matchMedia('(max-width: 720px)')
  isNarrow.value = mediaQuery.matches
  mediaQuery.addEventListener('change', onMediaChange)
  await loadPosition()
})
onBeforeUnmount(() => {
  active = false
  positionEpoch++
  structureEpoch++
  mediaQuery?.removeEventListener('change', onMediaChange)
})
</script>

<template>
  <UModal v-model:open="open" fullscreen title="Сверка ВОР с PDF" :description="headerDescription">
    <template #content>
      <div class="aw-dossier">
        <header class="aw-topbar">
          <div class="aw-nav">
            <UButton
              data-testid="prev-position"
              color="neutral"
              variant="ghost"
              icon="i-lucide-chevron-left"
              aria-label="Предыдущая позиция"
              :disabled="previousReady === null"
              @click="previousReady !== null && emit('navigate', previousReady)"
            />
            <UButton
              color="neutral"
              data-testid="next-position"
              icon="i-lucide-chevron-right"
              aria-label="Следующая позиция"
              :disabled="nextReady === null"
              @click="nextReady !== null && emit('navigate', nextReady)"
            />
          </div>
          <div class="aw-heading">
            <p class="aw-position">
              Позиция {{ positionIndex + 1
              }}{{ typeof positionCount === 'number' ? ` из ${positionCount}` : '' }} ·
              {{ workspace?.row.name ?? 'Открываем позицию…' }}
            </p>
            <div v-if="workspace" class="aw-header-summary" aria-live="polite">
              <UBadge
                :label="queueLabel(workspace.row.queue)"
                :color="queueColor(workspace.row.queue)"
                variant="soft"
                size="sm"
              />
              <span
                >ВОР:
                <strong>{{
                  quantityWithUnit(workspace.row.declared_quantity, workspace.row.unit)
                }}</strong></span
              >
              <span aria-hidden="true">{{
                compareDelta === null ? '·' : compareDelta === '±0' ? '=' : '≠'
              }}</span>
              <span
                >ПД:
                <strong>{{
                  quantityWithUnit(workspace.row.found_quantity, workspace.row.found_unit)
                }}</strong></span
              >
              <span v-if="compareDelta && compareDelta !== '±0'"
                >Разница: {{ compareDelta }} {{ workspace.row.unit || '' }}</span
              >
            </div>
          </div>
          <UButton
            color="neutral"
            variant="ghost"
            size="sm"
            icon="i-lucide-x"
            aria-label="Закрыть сверку (Esc)"
            @click="open = false"
            >Закрыть</UButton
          >
        </header>
        <div v-if="busy && !workspace" class="aw-loading" role="status">Открываем позицию…</div>
        <p v-if="error" class="error-box" role="alert">{{ error }}</p>
        <template v-if="workspace">
          <div class="aw-tabs" role="group" aria-label="Выбор панели">
            <UButton
              color="neutral"
              :variant="mobileTab === 'vor' ? 'solid' : 'outline'"
              :aria-pressed="mobileTab === 'vor'"
              @click="mobileTab = 'vor'"
              >Данные ВОР</UButton
            >
            <UButton
              color="neutral"
              :variant="mobileTab === 'pdf' ? 'solid' : 'outline'"
              :aria-pressed="mobileTab === 'pdf'"
              @click="mobileTab = 'pdf'"
              >PDF</UButton
            >
          </div>
          <main class="aw-main" :data-tab="mobileTab">
            <USplitter
              id="workspace-dossier-splitter"
              auto-save-id="workspace-dossier-splitter"
              :items="dossierItems"
              :orientation="isNarrow ? 'vertical' : 'horizontal'"
              class="aw-splitter"
            >
              <template #pdf>
                <div class="aw-pdf">
                  <div class="aw-pdf-toolbar">
                    <UButton
                      label="Оглавление"
                      icon="i-lucide-list-tree"
                      color="neutral"
                      variant="outline"
                      :aria-expanded="outlineOpen"
                      aria-controls="pdf-outline"
                      @click="outlineOpen = !outlineOpen"
                    />
                    <USelect
                      v-model="version"
                      :items="pdfs.map((v) => ({ label: v.filename, value: v.id }))"
                      aria-label="Проектный PDF"
                      class="aw-document-select"
                    />
                    <div v-if="outlineOpen" id="pdf-outline" class="aw-outline-panel">
                      <div class="aw-outline-header">
                        <strong>Оглавление ПД</strong
                        ><UButton
                          icon="i-lucide-x"
                          aria-label="Закрыть оглавление"
                          color="neutral"
                          variant="ghost"
                          @click="outlineOpen = false"
                        />
                      </div>
                      <OutlineTree
                        :data="structure"
                        :page="page"
                        :selected-id="selectedNode"
                        :error="structureError"
                        @select="selectNode"
                      />
                    </div>
                    <UInput
                      v-model="search"
                      placeholder="Текст в PDF"
                      aria-label="Поиск в PDF, Enter для поиска"
                      size="sm"
                      icon="i-lucide-search"
                      @keyup.enter="find"
                    />
                  </div>
                  <p v-if="searchPages.length" class="muted">
                    Найдено на страницах: {{ searchPages.join(', ') }}
                  </p>
                  <PdfPanel
                    v-if="version"
                    ref="pdfPanel"
                    :key="version"
                    :source-url="backend.contentUrl(version)"
                    :page="page"
                    :highlight="sourceHighlight"
                    @page="changePage"
                  />
                  <p v-else class="empty">Для этого аудита PDF не выбран.</p>
                </div>
              </template>
              <template #dossier>
                <div class="aw-dossier-pane excel-pane">
                  <section class="aw-result" aria-label="Результат проверки">
                    <p class="aw-verdict-kicker">{{ queueLabel(workspace.row.queue) }}</p>
                    <h1 class="aw-verdict-title">
                      {{ workspace.row.problem || 'Результат проверки' }}
                    </h1>
                    <p v-if="verdictDirection" class="aw-verdict-direction">
                      {{ verdictDirection }}
                    </p>
                    <p v-if="extraDetails" class="aw-verdict-details">{{ extraDetails }}</p>
                  </section>
                  <CodexPositionConclusion
                    v-if="workspace.row.codex_conclusion"
                    :run-id="runId"
                    :result="workspace.row.codex_conclusion"
                    :documents="documents"
                    :coefficient-available="
                      workspace.position.position_type === 'Материал' &&
                      /купл\s*[-:=]?\s*1[,.]26\b/i.test(workspace.position.comment ?? '')
                    "
                    @project-source="openProjectSource"
                  />
                  <section aria-label="Таблица ВОР" class="aw-vor">
                    <h2 class="aw-section-title aw-table-head">
                      <span
                        >{{ workspace.position.position_source?.sheet || 'ВОР'
                        }}<template v-if="workspace.row.source_row">
                          · строка {{ workspace.row.source_row }}</template
                        ></span
                      ><UButton
                        icon="i-lucide-download"
                        color="neutral"
                        variant="ghost"
                        size="xs"
                        aria-label="Скачать исходную ВОР"
                        title="Скачать исходную ВОР"
                        @click="downloadSource"
                      />
                    </h2>
                    <div class="sheet-scroll">
                      <table class="sheet">
                        <thead>
                          <tr>
                            <th scope="col">Наименование</th>
                            <th scope="col">Ед. изм.</th>
                            <th scope="col">Количество</th>
                            <th
                              v-if="workspace.row.formula || workspace.position.formula"
                              scope="col"
                            >
                              Формула
                            </th>
                          </tr>
                        </thead>
                        <tbody>
                          <tr>
                            <td>{{ workspace.position.work_name || workspace.row.name }}</td>
                            <td>{{ workspace.row.unit || '—' }}</td>
                            <td class="numbers">{{ workspace.row.declared_quantity || '—' }}</td>
                            <td v-if="workspace.row.formula || workspace.position.formula">
                              {{ workspace.row.formula || workspace.position.formula }}
                            </td>
                          </tr>
                        </tbody>
                      </table>
                    </div>
                  </section>
                  <section
                    v-if="workspace.position.comment"
                    :key="positionIndex"
                    class="aw-note-card"
                    aria-label="Примечание"
                  >
                    <h2 class="aw-section-title">Примечание</h2>
                    <p>{{ workspace.position.comment }}</p>
                  </section>
                </div>
              </template>
            </USplitter>
          </main>
        </template>
      </div>
    </template>
  </UModal>
</template>

<style scoped>
.aw-dossier {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  overflow-x: clip;
  background: var(--ui-bg-muted);
  color: var(--ui-text);
}
.aw-topbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: var(--ui-bg-elevated);
  border-bottom: 1px solid var(--ui-border);
}
.aw-nav {
  display: flex;
}
.aw-heading {
  min-width: 0;
  flex: 1;
}
.aw-position {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--ui-text-highlighted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.aw-header-summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 12px;
  font-size: 14px;
  font-variant-numeric: tabular-nums;
}
.aw-loading {
  padding: 24px;
}
.aw-tabs {
  display: none;
  gap: 8px;
  padding: 12px 16px 0;
}
.aw-main {
  flex: 1;
  min-height: 0;
  padding: 12px 16px 16px;
}
.aw-splitter {
  height: 100%;
  min-height: 0;
}
.aw-splitter :deep([role='separator']) {
  width: 1px;
  background: var(--ui-border);
  margin-inline: 6px;
}
.aw-splitter :deep([role='separator']:hover),
.aw-splitter :deep([role='separator']:focus-visible) {
  background: var(--ui-primary);
}
.aw-pdf {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
  width: 100%;
  height: 100%;
  min-height: 0;
}
.aw-pdf-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border: 1px solid var(--ui-border);
  border-radius: 8px;
  background: var(--ui-bg-elevated);
}
.aw-document-select {
  max-width: 280px;
  min-width: 0;
}
.aw-outline-panel {
  position: absolute;
  z-index: 11;
  top: 52px;
  left: 0;
  bottom: 0;
  width: min(340px, 100%);
  border: 1px solid var(--ui-border);
  background: var(--ui-bg-elevated);
  padding: 12px;
  overflow: auto;
  box-shadow: var(--app-shadow-overlay);
}
.aw-outline-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.aw-dossier-pane {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 16px;
  min-width: 0;
  width: 100%;
  height: 100%;
  min-height: 0;
  overflow: auto;
}
.aw-result,
.aw-note-card {
  padding: 16px;
  background: var(--ui-bg-elevated);
  border: 1px solid var(--ui-border);
  border-radius: 12px;
  clip-path: polygon(
    0 0,
    calc(100% - 12px) 0,
    100% 12px,
    100% 100%,
    12px 100%,
    0 calc(100% - 12px)
  );
}
.aw-verdict-kicker {
  margin: 0 0 4px;
  font-size: 12px;
  font-weight: 600;
  color: var(--ui-text-muted);
}
.aw-verdict-title {
  margin: 0;
  font-size: 18px;
  font-weight: 700;
  line-height: 1.4;
  overflow-wrap: anywhere;
}
.aw-verdict-direction {
  margin: 8px 0 0;
  font-size: 15px;
  font-weight: 600;
}
.aw-verdict-details {
  margin: 8px 0 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.aw-section-title {
  margin: 0 0 8px;
  font-size: 14px;
  font-weight: 700;
  color: var(--ui-text-highlighted);
}
.aw-table-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.aw-note-card p {
  margin: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.6;
}
.sheet-scroll {
  width: 100%;
  min-width: 0;
  overflow-x: auto;
  border: 1px solid var(--ui-border);
  border-radius: 8px;
  overscroll-behavior: contain;
}
.sheet {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  background: var(--ui-bg-elevated);
}
.sheet th,
.sheet td {
  border: 1px solid var(--ui-border);
  padding: 8px;
  text-align: left;
  vertical-align: top;
  overflow-wrap: anywhere;
}
.sheet th {
  color: var(--ui-text-muted);
  font-weight: 600;
}
.sheet .numbers {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
@media (max-width: 720px) {
  .aw-topbar {
    flex-wrap: wrap;
  }
  .aw-heading {
    order: 3;
    flex-basis: 100%;
  }
  .aw-tabs {
    display: flex;
  }
  .aw-main[data-tab='vor'] .aw-splitter > :has(> .aw-pdf),
  .aw-main[data-tab='pdf'] .aw-splitter > :has(> .aw-dossier-pane),
  .aw-main [role='separator'] {
    display: none;
  }
  .aw-pdf,
  .aw-dossier-pane {
    padding: 0;
  }
}
</style>

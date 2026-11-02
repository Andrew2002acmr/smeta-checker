<script setup lang="ts">
import type { SplitterItem } from '@nuxt/ui'
import type {
  WorkspaceDto,
  VersionDto,
  StructureDto,
  StructureNodeDto,
  HumanBasisDto,
  BasisPreviewDto,
  BasisFragmentRequest,
  SpecifyBasisRequest,
} from '~/generated/types.gen'
import { queueColor, queueLabel, quantityDelta, quantityWithUnit } from '~/utils/presentation'
import { evidenceText, evidenceExcerpt, EVIDENCE_PREVIEW_LENGTH } from '~/utils/evidenceText'
const props = defineProps<{
    runId: string
    positionIndex: number
    documents: VersionDto[]
    positionCount?: number
    readyPositions?: number[]
  }>(),
  emit = defineEmits<{ close: []; navigate: [index: number] }>(),
  backend = useBackend(),
  open = ref(true),
  mobileTab = ref<'vor' | 'pdf'>('vor'),
  workspace = ref<WorkspaceDto | null>(null),
  structure = ref<StructureDto | null>(null),
  version = ref(''),
  page = ref(1),
  selectedNode = ref(''),
  sourceIndex = ref(0),
  error = ref(''),
  structureError = ref(''),
  busy = ref(true),
  search = ref(''),
  pdfPanel = ref<{
    find: (q: string) => Promise<number[]>
    selectedText: () => void
    clearSelection: () => void
  }>(),
  searchPages = ref<number[]>([]),
  expandedQuotes = ref<Set<number>>(new Set())
const historyPanel = ref<{ load: () => Promise<void> }>()
async function refreshVerdict() {
  const index = props.positionIndex
  try {
    const value = await backend.workspace(props.runId, index)
    if (workspace.value && props.positionIndex === index) workspace.value.row = value.row
  } catch {
    error.value = 'Не удалось обновить итог нормативной проверки. Обновите страницу.'
  }
}
const previousReady = computed(() =>
  props.readyPositions
    ? ([...props.readyPositions].filter((i) => i < props.positionIndex).sort((a, b) => b - a)[0] ??
      null)
    : props.positionIndex > 0
      ? props.positionIndex - 1
      : null,
)
const nextReady = computed(() =>
  props.readyPositions
    ? ([...props.readyPositions].filter((i) => i > props.positionIndex).sort((a, b) => a - b)[0] ??
      null)
    : typeof props.positionCount === 'number' && props.positionIndex + 1 < props.positionCount
      ? props.positionIndex + 1
      : null,
)
const humanBasis = ref<HumanBasisDto[]>([]),
  humanSourceId = ref(''),
  choosingBasis = ref(false),
  basisMode = ref<'row' | 'area' | 'text' | 'multi'>('row'),
  basisPreview = ref<BasisPreviewDto | null>(null),
  previewBusy = ref(false),
  basisRects = ref<number[][] | undefined>(),
  basisRect = ref<number[] | null>(null),
  basisTarget = ref<{ version_id: string; page: number } | null>(null),
  basisComment = ref(''),
  basisBusy = ref(false),
  basisError = ref(''),
  basisSaved = ref('')
const basisFragments = ref<BasisFragmentRequest[]>([])
const ocrKey = ref<string | null>(null)
const ocrBusy = ref(false)
const fragmentRoles = [
  { label: 'Без назначения', value: 'other' },
  { label: 'Работа', value: 'work' },
  { label: 'Количество', value: 'volume' },
  { label: 'Единица', value: 'unit' },
  { label: 'Условия', value: 'conditions' },
]
let previewEpoch = 0
const basisModes = [
  { value: 'row', label: 'Строка таблицы' },
  { value: 'area', label: 'Область' },
  { value: 'multi', label: 'Несколько областей' },
  { value: 'text', label: 'Текст' },
] as const
const humanNotes = computed(() => {
  if (choosingBasis.value && basisMode.value === 'multi')
    return basisFragments.value.map((f, i) => ({
      id: `draft:${i}`,
      page: f.page,
      rect: f.rect,
      text: `Область ${i + 1}`,
      deleted: false,
      position_id: '',
    }))
  return humanBasis.value
    .filter((b) => b.pdf_version_id === version.value)
    .flatMap((b) => {
      const fragments = b.fragments ?? []
      const parts = fragments.length
        ? fragments
        : [{ page: b.page, rect: b.rect, region: b.region }]
      return parts.flatMap((f, part) => {
        const ranges = f.region
          ? f.region.highlights.map((h) => [
              h.bbox[0]! / f.region!.width,
              h.bbox[1]! / f.region!.height,
              h.bbox[2]! / f.region!.width,
              h.bbox[3]! / f.region!.height,
            ])
          : f.rect
            ? [f.rect]
            : []
        return ranges.map((rect, i) => ({
          id: `${b.id}:${part}:${i}`,
          page: f.page,
          rect,
          text: b.comment || b.quote,
          deleted: false,
          position_id: '',
        }))
      })
    })
})
function regionOnPage(value: BasisPreviewDto | HumanBasisDto | null | undefined) {
  if (!value) return null
  if (!value.fragments?.length) return value.page === page.value ? value.region : null
  const parts = value.fragments!.filter((f) => f.page === page.value)
  return parts.length
    ? { ...parts[0]!.region, highlights: parts.flatMap((f) => f.region.highlights) }
    : null
}
const selectedHuman = computed(() =>
  humanBasis.value.find(
    (item) =>
      item.id === humanSourceId.value &&
      item.pdf_version_id === version.value &&
      (item.page === page.value || item.fragments?.some((f) => f.page === page.value)),
  ),
)
const source = computed(() => workspace.value?.evidence[sourceIndex.value]),
  isBasis = computed(
    () =>
      !!source.value &&
      source.value.version_id === version.value &&
      source.value.page === page.value &&
      !selectedNode.value,
  ),
  isLast = computed(() => nextReady.value === null),
  headerDescription = computed(() =>
    workspace.value
      ? `${workspace.value.row.number} · ${workspace.value.row.name}`
      : 'Открываем позицию…',
  ),
  region = computed(() =>
    choosingBasis.value
      ? basisMode.value === 'multi'
        ? null
        : regionOnPage(basisPreview.value)
      : (regionOnPage(selectedHuman.value) ?? (isBasis.value ? source.value?.region : null)),
  ),
  pdfs = computed(() => props.documents.filter((v) => v.kind === 'pdf')),
  printed = computed(
    () => structure.value?.pages.find((p) => p.physical_page === page.value)?.printed_page,
  ),
  sheetName = computed(() => workspace.value?.position.position_source?.sheet || ''),
  quantityCell = computed(() => {
    const cells = workspace.value?.row.cells || {},
      row = workspace.value?.row.source_row
    if (typeof row !== 'number') return ''
    // Cells map address -> value; the quantity column letter is unknown here,
    // so show the first address on the source row (stable, no guessing).
    return Object.keys(cells).find((addr) => addr.endsWith(String(row))) || ''
  }),
  verdictOpen = ref(false),
  verdictLead = computed(() => {
    const text = workspace.value?.row.problem || ''
    if (
      workspace.value?.row.queue === 'review' &&
      /структурированного подтверждения|однозначный источник параметра/.test(text)
    )
      return 'Не хватает подтверждения по ПД или нормативам.'
    const end = text.search(/[.?!]\s/)
    return end > 0 ? text.slice(0, end + 1) : text
  }),
  verdictRest = computed(() => {
    const text = workspace.value?.row.problem || ''
    const end = text.search(/[.?!]\s/)
    return end > 0 ? text.slice(end + 1).trim() : ''
  }),
  dossierItems: SplitterItem[] = [
    { slot: 'pdf', minSize: 30, defaultSize: 62 },
    { slot: 'dossier', minSize: 25, defaultSize: 38 },
  ],
  compareDelta = computed(() =>
    quantityDelta(workspace.value?.row.declared_quantity, workspace.value?.row.found_quantity),
  )
let active = true,
  epoch = 0
const kindLabels: Record<string, string> = {
  technology: 'Способ выполнения',
  presence: 'Работа',
  quantity: 'Количество',
  geometry: 'Размеры',
  attribute: 'Характеристики',
  conditions: 'Условия',
  work: 'Работа',
  volume: 'Объём',
  unit: 'Единица измерения',
  comment: 'Условия',
  reference: 'Ссылка ВОР',
}
const evidenceGroups = computed(() => {
  const groups: { item: WorkspaceDto['evidence'][number]; index: number; kinds: string[] }[] = []
  for (const [index, item] of (workspace.value?.evidence || []).entries()) {
    const group = groups.find(
      (g) =>
        g.item.version_id === item.version_id &&
        g.item.page === item.page &&
        g.item.quote === item.quote,
    )
    if (group) {
      if (!group.kinds.includes(item.kind)) group.kinds.push(item.kind)
    } else groups.push({ item, index, kinds: [item.kind] })
  }
  return groups
})
async function loadStructure() {
  const current = ++epoch
  structure.value = null
  structureError.value = ''
  if (!version.value) return
  try {
    const data = await backend.structure(version.value)
    if (active && current === epoch) structure.value = data
  } catch (e) {
    if (active && current === epoch) structureError.value = readableError(e)
  }
}
function basis(index = sourceIndex.value) {
  humanSourceId.value = ''
  sourceIndex.value = index
  const item = workspace.value?.evidence[index]
  if (item) {
    version.value = item.version_id
    page.value = item.page
    selectedNode.value = ''
  }
}
function clearPendingBasis() {
  previewEpoch++
  basisFragments.value = []
  ocrKey.value = null
  ocrBusy.value = false
  basisRect.value = null
  basisRects.value = undefined
  basisTarget.value = null
  basisPreview.value = null
  previewBusy.value = false
  pdfPanel.value?.clearSelection()
}
function cancelBasisSelection() {
  choosingBasis.value = false
  clearPendingBasis()
}
function startBasisSelection() {
  clearPendingBasis()
  basisError.value = ''
  basisSaved.value = ''
  basisComment.value = ''
  choosingBasis.value = true
}
function changeBasisMode(mode: 'row' | 'area' | 'text' | 'multi') {
  clearPendingBasis()
  basisMode.value = mode
  basisError.value = ''
}
async function selectedBasis(selection: {
  rect: number[]
  text: string
  rects?: number[][]
  mode?: 'row' | 'area' | 'text'
}) {
  if (basisMode.value === 'multi') {
    if (basisBusy.value || ocrBusy.value) return
    if (basisFragments.value.length >= 20) {
      basisError.value = 'Можно добавить до 20 областей.'
      return
    }
    basisFragments.value.push({
      page: page.value,
      rect: selection.rect as [number, number, number, number],
      corrected_text: null,
      text_checked: false,
      role: 'other',
    })
    ocrKey.value = null
    pdfPanel.value?.clearSelection()
    await refreshMulti()
    return
  }
  const current = ++previewEpoch
  const context = `${props.runId}:${props.positionIndex}:${version.value}:${page.value}`
  basisRect.value = selection.rect
  basisRects.value = selection.rects
  basisTarget.value = { version_id: version.value, page: page.value }
  basisPreview.value = null
  basisError.value = ''
  previewBusy.value = true
  try {
    const preview = await backend.previewBasis(props.runId, props.positionIndex, {
      ...basisTarget.value,
      rect: selection.rect,
      rects: selection.rects,
      mode: selection.mode || basisMode.value,
    })
    if (
      current !== previewEpoch ||
      context !== `${props.runId}:${props.positionIndex}:${version.value}:${page.value}`
    )
      return
    basisPreview.value = preview
    pdfPanel.value?.clearSelection()
  } catch (e) {
    if (current === previewEpoch) basisError.value = readableError(e)
  } finally {
    if (current === previewEpoch) previewBusy.value = false
  }
}
function multiPayload() {
  const first = basisFragments.value[0]!
  return {
    version_id: version.value,
    page: first.page,
    rect: first.rect,
    mode: 'multi' as const,
    fragments: JSON.parse(JSON.stringify(basisFragments.value)) as BasisFragmentRequest[],
    ocr_key: ocrKey.value,
    comment: basisComment.value,
  }
}
async function refreshMulti(recognize = false) {
  if (!basisFragments.value.length) {
    basisPreview.value = null
    return
  }
  const current = ++previewEpoch
  const context = `${props.runId}:${props.positionIndex}:${version.value}`
  const payload = multiPayload()
  previewBusy.value = true
  ocrBusy.value = recognize
  basisError.value = ''
  try {
    const result = await (recognize ? backend.recognizeBasis : backend.previewBasis)(
      props.runId,
      props.positionIndex,
      payload,
    )
    if (
      !active ||
      current !== previewEpoch ||
      context !== `${props.runId}:${props.positionIndex}:${version.value}`
    )
      return
    basisPreview.value = result
    ocrKey.value = result.ocr_key ?? null
  } catch (e) {
    if (current === previewEpoch) {
      basisPreview.value = null
      basisError.value = readableError(e)
    }
  } finally {
    if (current === previewEpoch) {
      previewBusy.value = false
      ocrBusy.value = false
    }
  }
}
function editFragment(fragment: BasisFragmentRequest, text: string) {
  fragment.corrected_text = text
  fragment.text_checked = false
}
function selectionPayload(): SpecifyBasisRequest | null {
  if (basisMode.value === 'multi') return basisFragments.value.length ? multiPayload() : null
  if (!basisRect.value || !basisTarget.value) return null
  return {
    ...basisTarget.value,
    rect: basisRect.value as [number, number, number, number],
    rects: basisRects.value as [number, number, number, number][] | undefined,
    mode: basisMode.value,
    comment: basisComment.value,
  }
}
async function saveDraft() {
  const payload = selectionPayload()
  if (!payload || basisBusy.value || previewBusy.value || ocrBusy.value) return
  basisBusy.value = true
  basisError.value = ''
  const current = epoch,
    runId = props.runId,
    index = props.positionIndex
  try {
    await backend.saveBasisDraft(runId, index, payload)
    if (active && current === epoch) {
      basisSaved.value = 'Черновик сохранён. Его можно открыть в истории.'
      void historyPanel.value?.load()
    }
  } catch (e) {
    if (current === epoch) basisError.value = readableError(e)
  } finally {
    if (current === epoch) basisBusy.value = false
  }
}
async function restoreDraft(selection: SpecifyBasisRequest) {
  cancelBasisSelection()
  version.value = selection.version_id
  page.value = selection.page
  await nextTick()
  choosingBasis.value = true
  basisMode.value = selection.mode ?? 'area'
  basisComment.value = selection.comment ?? ''
  if (selection.mode === 'multi') {
    basisFragments.value = JSON.parse(JSON.stringify(selection.fragments ?? []))
    ocrKey.value = selection.ocr_key ?? null
    await refreshMulti()
  } else {
    await selectedBasis({
      rect: selection.rect,
      rects: selection.rects ?? undefined,
      text: '',
      mode: selection.mode ?? 'area',
    })
  }
}
function removeFragment(index: number) {
  if (basisBusy.value || ocrBusy.value) return
  basisFragments.value.splice(index, 1)
  ocrKey.value = null
  void refreshMulti()
}
function recognizeBasis() {
  if (basisBusy.value || ocrBusy.value) return
  if (basisMode.value !== 'multi') {
    if (!basisRect.value || !basisTarget.value) return
    basisFragments.value = [
      {
        page: basisTarget.value.page,
        rect: basisRect.value as [number, number, number, number],
        corrected_text: null,
        text_checked: false,
        role: 'other',
      },
    ]
    basisMode.value = 'multi'
  }
  void refreshMulti(true)
}
async function saveMultiBasis() {
  if (!basisFragments.value.length || basisBusy.value || ocrBusy.value || previewBusy.value) return
  const payload = multiPayload()
  const runId = props.runId,
    index = props.positionIndex
  await saveHumanAction(async () => {
    const preview = await backend.previewBasis(runId, index, payload)
    return backend.specifyBasis(runId, index, {
      ...payload,
      selection_digest: preview.selection_digest,
    })
  })
}
function showHumanBasis(item: HumanBasisDto, targetPage = item.page) {
  cancelBasisSelection()
  humanSourceId.value = item.id
  version.value = item.pdf_version_id
  page.value = targetPage
  selectedNode.value = ''
}
function isHumanConfirmed(item: WorkspaceDto['evidence'][number]) {
  return humanBasis.value.some(
    (b) => b.pdf_version_id === item.version_id && b.page === item.page && b.quote === item.quote,
  )
}
async function saveHumanAction(action: () => Promise<HumanBasisDto[]>) {
  const current = epoch
  basisBusy.value = true
  basisError.value = ''
  basisSaved.value = ''
  try {
    const records = await action()
    if (!active || current !== epoch) return
    humanBasis.value = records
    void historyPanel.value?.load()
    basisSaved.value = 'Выбор сохранён для этой позиции и версии ПД.'
    cancelBasisSelection()
  } catch (e) {
    if (active && current === epoch) basisError.value = readableError(e)
  } finally {
    if (active && current === epoch) basisBusy.value = false
  }
}
function saveSpecifiedBasis() {
  if (!basisRect.value || !basisTarget.value || !basisPreview.value || previewBusy.value) return
  void saveHumanAction(() =>
    backend.specifyBasis(props.runId, props.positionIndex, {
      ...basisTarget.value!,
      rect: basisRect.value!,
      rects: basisRects.value,
      mode: basisMode.value,
      selection_digest: basisPreview.value!.selection_digest,
      comment: basisComment.value,
    }),
  )
}
function selectNode(node: StructureNodeDto) {
  page.value = node.physical_start
  selectedNode.value = node.id
}
function changePage(value: number) {
  page.value = value
  selectedNode.value = ''
}
function plainQuote(quote: string) {
  return evidenceText(quote)
}
function shortQuote(quote: string) {
  return evidenceExcerpt(quote)
}
function isNumericCell(value: unknown) {
  if (typeof value !== 'string' && typeof value !== 'number') return false
  const text = String(value).replace(/\s/g, '').replace(',', '.')
  return text !== '' && !Number.isNaN(Number(text))
}
function toggleQuote(index: number) {
  const next = new Set(expandedQuotes.value)
  if (next.has(index)) next.delete(index)
  else next.add(index)
  expandedQuotes.value = next
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
function confirmationText(value: boolean, detail: string) {
  return value ? `Да · ${detail}` : detail
}
const isNarrow = ref(false)
let mediaQuery: MediaQueryList | null = null
function onMediaChange(event: MediaQueryListEvent) {
  isNarrow.value = event.matches
}
watch(version, clearPendingBasis)
watch(page, () => {
  if (basisMode.value !== 'multi') clearPendingBasis()
})
watch(version, loadStructure)
watch(
  () => props.readyPositions,
  (ready) => {
    if (!workspace.value && !busy.value && ready?.includes(props.positionIndex)) void loadPosition()
  },
)
watch(
  () => props.positionIndex,
  () => {
    verdictOpen.value = false
    void loadPosition()
  },
)
watch(open, (value) => {
  if (!value) emit('close')
})
async function loadPosition() {
  const current = ++epoch
  busy.value = true
  error.value = ''
  cancelBasisSelection()
  humanSourceId.value = ''
  humanBasis.value = []
  basisError.value = ''
  basisSaved.value = ''
  basisBusy.value = false
  try {
    const [data, reviewed] = await Promise.all([
      backend.workspace(props.runId, props.positionIndex),
      backend.humanBasis(props.runId, props.positionIndex),
    ])
    if (!active || current !== epoch) return
    workspace.value = data
    humanBasis.value = reviewed
    if (workspace.value.evidence.length) basis(0)
    else {
      version.value = pdfs.value[0]?.id || ''
      page.value = 1
    }
  } catch (e) {
    if (active && current === epoch) error.value = readableError(e)
  } finally {
    if (active && current === epoch) busy.value = false
  }
}
onMounted(async () => {
  mediaQuery = window.matchMedia('(max-width: 720px)')
  isNarrow.value = mediaQuery.matches
  mediaQuery.addEventListener('change', onMediaChange)
  await loadPosition()
})
onBeforeUnmount(() => {
  active = false
  epoch++
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
              color="neutral"
              variant="ghost"
              icon="i-lucide-chevron-left"
              aria-label="Предыдущая позиция"
              :disabled="previousReady === null"
              @click="previousReady !== null && emit('navigate', previousReady)"
            />
            <UButton
              color="neutral"
              variant="ghost"
              icon="i-lucide-chevron-right"
              aria-label="Следующая позиция"
              :disabled="isLast"
              @click="nextReady !== null && emit('navigate', nextReady)"
            />
          </div>
          <div class="aw-heading">
            <p class="aw-position">
              Позиция №{{ workspace?.row.number ?? '…' }} ·
              {{ workspace?.row.name ?? 'Открываем позицию…' }}
            </p>
            <p class="aw-subline">
              {{ sheetName || 'Позиция CSV'
              }}{{ quantityCell ? ` · ячейка ${quantityCell}` : '' }} ·
              {{ source?.filename || pdfs[0]?.filename || 'PDF не выбран' }}
            </p>
          </div>
          <UButton
            color="neutral"
            variant="ghost"
            icon="i-lucide-x"
            aria-label="Закрыть сверку"
            @click="open = false"
          >
            Закрыть
          </UButton>
        </header>

        <div
          v-if="busy && !workspace"
          class="aw-loading"
          role="status"
          aria-label="Открываем позицию"
        >
          <USkeleton class="aw-loading-title" />
          <USkeleton class="aw-loading-line" />
          <div class="aw-loading-panes">
            <USkeleton class="aw-loading-pdf" />
            <USkeleton class="aw-loading-side" />
          </div>
        </div>
        <p v-if="error" class="error-box" role="alert">{{ error }}</p>
        <template v-if="workspace">
          <section class="aw-verdict" aria-live="polite">
            <div class="aw-verdict-main">
              <div class="aw-verdict-meta">
                <UBadge
                  :label="queueLabel(workspace.row.queue)"
                  :color="queueColor(workspace.row.queue)"
                  variant="solid"
                  size="md"
                />
                <span class="aw-verdict-queue">
                  Позиция {{ positionIndex + 1
                  }}{{ typeof positionCount === 'number' ? ` из ${positionCount}` : '' }}
                </span>
              </div>
              <h1 class="aw-verdict-title">
                {{ verdictLead }}
              </h1>
              <button
                v-if="verdictRest"
                type="button"
                class="aw-verdict-more"
                :aria-expanded="verdictOpen"
                @click="verdictOpen = !verdictOpen"
              >
                {{ verdictOpen ? 'Скрыть подробности' : 'Подробнее' }}
              </button>
              <p v-if="verdictOpen && verdictRest" class="aw-verdict-rest">{{ verdictRest }}</p>
              <p v-if="verdictOpen && workspace.row.details" class="aw-verdict-details">
                {{ workspace.row.details }}
              </p>
              <p v-if="workspace.row.related_work" class="muted">
                Расчётный материал работы №{{ workspace.row.related_work.number }} (строка
                {{ workspace.row.related_work.row }}). Проверяем основание работы, формулу и
                коэффициенты расхода.
                <UButton
                  variant="link"
                  @click="emit('navigate', Number(workspace.row.related_work.index))"
                  >К связанной работе</UButton
                >
              </p>
              <p class="aw-verdict-action">{{ workspace.row.action }}</p>
            </div>
            <div class="aw-compare" role="group" aria-label="Сравнение количества">
              <div class="aw-compare-values">
                <span class="aw-compare-side">
                  <span class="aw-compare-label"
                    >ВОР{{ quantityCell ? ` · ${quantityCell}` : '' }}</span
                  >
                  <span class="aw-compare-number">{{
                    quantityWithUnit(workspace.row.declared_quantity, workspace.row.unit)
                  }}</span>
                </span>
                <span
                  class="aw-compare-sign"
                  :class="{
                    match: compareDelta === '±0',
                    diff: compareDelta !== null && compareDelta !== '±0',
                  }"
                  aria-hidden="true"
                  >{{ compareDelta === null ? '→' : compareDelta === '±0' ? '=' : '≠' }}</span
                >
                <span class="aw-compare-side">
                  <span class="aw-compare-label"
                    >ПД{{ source ? ` · стр. ${source.page}` : '' }}</span
                  >
                  <span class="aw-compare-number">{{
                    quantityWithUnit(workspace.row.found_quantity, workspace.row.found_unit)
                  }}</span>
                </span>
              </div>
              <p v-if="compareDelta && compareDelta !== '±0'" class="aw-compare-delta">
                Разница {{ compareDelta }} {{ workspace.row.unit || '' }}
              </p>
            </div>
          </section>

          <div class="aw-tabs" role="group" aria-label="Выбор панели">
            <UButton
              color="neutral"
              :variant="mobileTab === 'vor' ? 'solid' : 'outline'"
              :aria-pressed="mobileTab === 'vor'"
              @click="mobileTab = 'vor'"
            >
              Данные ВОР
            </UButton>
            <UButton
              color="neutral"
              :variant="mobileTab === 'pdf' ? 'solid' : 'outline'"
              :aria-pressed="mobileTab === 'pdf'"
              @click="mobileTab = 'pdf'"
            >
              PDF
            </UButton>
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
                  <UCollapsible>
                    <UButton
                      :label="`Оглавление ПД${structure ? ` · ${structure.pages.length} стр.` : ''} (текущая: стр. ${page})`"
                      color="neutral"
                      variant="soft"
                      trailing-icon="i-lucide-chevron-down"
                      block
                      class="group"
                      :ui="{
                        trailingIcon:
                          'group-data-[state=open]:rotate-180 transition-transform duration-200',
                      }"
                    />
                    <template #content>
                      <div class="aw-outline-panel">
                        <USelect
                          v-model="version"
                          :items="pdfs.map((v) => ({ label: v.filename, value: v.id }))"
                          aria-label="Проектный PDF"
                          class="w-full"
                        />
                        <OutlineTree
                          :data="structure"
                          :page="page"
                          :selected-id="selectedNode"
                          :error="structureError"
                          @select="selectNode"
                        />
                      </div>
                    </template>
                  </UCollapsible>

                  <div class="aw-pdf-toolbar">
                    <div class="aw-pdf-search">
                      <UInput
                        v-model="search"
                        placeholder="Текст в PDF"
                        aria-label="Поиск в PDF, Enter для поиска"
                        size="sm"
                        icon="i-lucide-search"
                        @keyup.enter="find"
                      />
                    </div>
                    <UButton
                      label="Указать основание"
                      icon="i-lucide-mouse-pointer-2"
                      variant="soft"
                      :disabled="basisBusy || !version"
                      @click="startBasisSelection"
                    />
                  </div>
                  <div v-if="choosingBasis" class="aw-basis-editor">
                    <div
                      class="aw-evidence-actions"
                      role="group"
                      aria-label="Способ выделения основания"
                    >
                      <UButton
                        v-for="mode in basisModes"
                        :key="mode.value"
                        :label="mode.label"
                        :variant="basisMode === mode.value ? 'solid' : 'soft'"
                        :disabled="basisBusy || ocrBusy"
                        @click="changeBasisMode(mode.value)"
                      />
                      <UButton
                        label="Отмена"
                        color="neutral"
                        variant="ghost"
                        :disabled="basisBusy"
                        @click="cancelBasisSelection"
                      />
                    </div>
                    <p v-if="basisMode === 'row'">
                      Кликните по нужной строке таблицы — выделим её целиком по границам ячеек.
                    </p>
                    <p v-else-if="basisMode === 'area'">
                      Обведите нужную область. В предпросмотр попадёт только текст внутри рамки.
                    </p>
                    <p v-else-if="basisMode === 'multi'">
                      Обведите ячейки по очереди. Можно переходить на другие страницы этого PDF. До
                      20 областей в одном основании.
                    </p>
                    <p v-else>
                      Выделите текст мышью, как в обычном документе. Предпросмотр появится сразу
                      после отпускания кнопки мыши.
                    </p>
                    <UButton
                      v-if="basisFragments.length || basisRect"
                      label="Сохранить черновик"
                      variant="outline"
                      :disabled="basisBusy || previewBusy || ocrBusy"
                      @click="saveDraft"
                    />
                    <p v-if="previewBusy" role="status">
                      {{ ocrBusy ? 'Распознаём выбранные области…' : 'Уточняем выделение…' }}
                    </p>
                    <UButton
                      v-if="basisFragments.length || basisRect"
                      label="Распознать выбранное"
                      icon="i-lucide-scan-text"
                      :loading="ocrBusy"
                      :disabled="previewBusy || basisBusy"
                      @click="recognizeBasis"
                    />
                    <template v-if="basisMode === 'multi' && basisFragments.length">
                      <p>
                        Областей: {{ basisFragments.length }}. Результат OCR проверьте перед
                        сохранением.
                      </p>
                      <div
                        v-for="(fragment, index) in basisFragments"
                        :key="index"
                        class="aw-fragment"
                      >
                        <div class="aw-evidence-actions">
                          <UButton
                            :label="`Область ${index + 1} · стр. ${fragment.page}`"
                            variant="ghost"
                            @click="changePage(fragment.page)"
                          />
                          <UButton
                            :aria-label="`Удалить область ${index + 1}`"
                            label="Удалить"
                            color="error"
                            variant="ghost"
                            :disabled="ocrBusy || basisBusy"
                            @click="removeFragment(index)"
                          />
                        </div>
                        <USelect
                          v-model="fragment.role"
                          :items="fragmentRoles"
                          :aria-label="`Назначение области ${index + 1}`"
                          :disabled="ocrBusy || basisBusy"
                        />
                        <UTextarea
                          :model-value="
                            fragment.corrected_text ?? basisPreview?.fragments?.[index]?.text ?? ''
                          "
                          :aria-label="`Текст области ${index + 1}`"
                          placeholder="Нажмите «Распознать выбранное» или введите текст вручную"
                          :maxlength="4000"
                          :rows="2"
                          :disabled="ocrBusy || basisBusy || previewBusy"
                          @update:model-value="editFragment(fragment, $event)"
                        />
                        <UCheckbox
                          v-model="fragment.text_checked"
                          label="Текст проверен по изображению"
                          :disabled="ocrBusy || basisBusy || previewBusy"
                        />
                      </div>
                      <UTextarea
                        v-model="basisComment"
                        aria-label="Комментарий к основанию"
                        placeholder="Комментарий к основанию (необязательно)"
                        :maxlength="2000"
                      />
                      <UButton
                        label="Сохранить основание"
                        :loading="basisBusy"
                        :disabled="ocrBusy || previewBusy"
                        @click="saveMultiBasis"
                      />
                    </template>
                    <template v-if="basisPreview && basisMode !== 'multi'">
                      <p>
                        Будет сохранено · {{ basisPreview.filename }} · стр. {{ basisPreview.page }}
                      </p>
                      <UTextarea
                        v-if="basisPreview.quote"
                        :model-value="basisPreview.quote"
                        readonly
                        aria-label="Текст выбранного основания"
                        :rows="4"
                        class="w-full"
                      />
                      <p v-else class="muted">
                        В этой области нет текстового слоя. Сохранится выделенная область PDF.
                      </p>
                      <UTextarea
                        v-model="basisComment"
                        placeholder="Комментарий к основанию (необязательно)"
                        aria-label="Комментарий к основанию"
                        :maxlength="2000"
                        class="w-full"
                      />
                      <UButton
                        label="Сохранить основание"
                        icon="i-lucide-check"
                        :loading="basisBusy"
                        :disabled="previewBusy"
                        @click="saveSpecifiedBasis"
                      />
                    </template>
                  </div>
                  <p v-if="basisError" class="error-box" role="alert">{{ basisError }}</p>
                  <p v-if="basisSaved" class="muted" role="status">{{ basisSaved }}</p>
                  <p v-if="searchPages.length" class="muted">
                    Найдено на страницах: {{ searchPages.join(', ') }}
                  </p>
                  <UAlert
                    v-if="source && !isBasis && !selectedHuman"
                    color="warning"
                    variant="soft"
                    title="Просмотр другой страницы. Заключение по позиции не изменилось."
                    :description="`Открыта стр. ${page}, основание — на стр. ${source.page}. Вердикт «${queueLabel(workspace.row.queue)}» относится к позиции, а не к открытому листу.`"
                  />
                  <p v-if="region && !region.highlights.length" class="muted">
                    Совпадение не подсвечено — листайте страницу вручную
                  </p>

                  <PdfPanel
                    v-if="version"
                    ref="pdfPanel"
                    :key="version"
                    :source-url="backend.contentUrl(version)"
                    :page="page"
                    :region="region"
                    :annotate="choosingBasis && !ocrBusy && !basisBusy"
                    :selection-mode="basisMode === 'multi' ? 'area' : basisMode"
                    :notes="humanNotes"
                    @selection="selectedBasis"
                    @page="changePage"
                  />
                  <p v-else class="empty">Для этого аудита PDF не выбран.</p>
                  <p class="aw-filename">{{ source?.filename || pdfs[0]?.filename }}</p>
                </div>
              </template>

              <template #dossier>
                <div class="aw-dossier-pane excel-pane">
                  <NormativePanel
                    :key="`${runId}:${positionIndex}`"
                    :run-id="runId"
                    :position-index="positionIndex"
                    :row="workspace.row"
                    @validated="refreshVerdict"
                    @specify-basis="startBasisSelection"
                  />
                  <section v-if="humanBasis.length" aria-label="Выбор пользователя">
                    <h2 class="aw-section-title">Выбор пользователя · {{ humanBasis.length }}</h2>
                    <p class="muted">Основания сохранены отдельно от автоматического заключения.</p>
                    <UCard
                      v-for="item in humanBasis"
                      :key="item.id"
                      :ui="{ body: 'p-3 sm:p-3' }"
                      class="mb-2"
                    >
                      <UBadge label="Подтверждено пользователем" color="success" variant="soft" />
                      <p>{{ item.filename }} · стр. {{ item.page }}</p>
                      <p v-if="item.outdated" class="muted">
                        Машинный результат обновился после подтверждения. Проверьте основание
                        повторно.
                      </p>
                      <p v-if="item.reused" class="muted">
                        Выбрано в предыдущей проверке этих же документов.
                      </p>
                      <blockquote class="aw-quote">
                        {{ shortQuote(item.quote) || 'Область PDF без текстового слоя' }}
                      </blockquote>
                      <div v-if="item.fragments?.length" class="aw-evidence-actions">
                        <UButton
                          v-for="(fragment, index) in item.fragments"
                          :key="index"
                          :label="`Область ${index + 1} · стр. ${fragment.page}`"
                          variant="ghost"
                          size="xs"
                          @click="showHumanBasis(item, fragment.page)"
                        />
                      </div>
                      <p v-if="item.comment">{{ item.comment }}</p>
                      <p class="muted">
                        {{ item.actor }} · {{ new Date(item.created_at).toLocaleString('ru-RU') }}
                      </p>
                      <div class="aw-evidence-actions">
                        <UButton
                          label="Показать в PDF"
                          variant="soft"
                          size="xs"
                          @click="showHumanBasis(item)"
                        />
                        <UButton
                          label="Отменить подтверждение"
                          variant="ghost"
                          color="neutral"
                          size="xs"
                          :disabled="basisBusy"
                          @click="
                            saveHumanAction(() =>
                              backend.revokeBasis(props.runId, props.positionIndex, item.id),
                            )
                          "
                        />
                      </div>
                    </UCard>
                  </section>
                  <ReviewHistory
                    :key="`${runId}:${positionIndex}`"
                    ref="historyPanel"
                    :run-id="runId"
                    :position-index="positionIndex"
                    :page="page"
                    @restore="restoreDraft"
                  />
                  <section aria-label="Основания">
                    <h2 class="aw-section-title">Основания · {{ evidenceGroups.length }}</h2>
                    <p v-if="!evidenceGroups.length" class="muted">
                      Подтверждённый источник не установлен. Документ слева открыт для ручной
                      сверки.
                    </p>
                    <div class="aw-evidence-list">
                      <UCard
                        v-for="group in evidenceGroups"
                        :key="group.index"
                        :ui="{ body: 'p-3 sm:p-3' }"
                        :style="group.item.confirmed ? undefined : 'border-color: var(--ui-error)'"
                      >
                        <div class="aw-evidence-meta">
                          <UBadge
                            v-for="kind in group.kinds"
                            :key="kind"
                            :label="kindLabels[kind] || 'Основание'"
                            color="neutral"
                            variant="soft"
                          />
                          <span class="aw-evidence-page">стр. {{ group.item.page }}</span>
                          <UBadge
                            :label="
                              isHumanConfirmed(group.item)
                                ? 'Подтверждено пользователем'
                                : group.item.confirmed
                                  ? 'Найдено автоматически'
                                  : 'Не подтверждено'
                            "
                            :color="group.item.confirmed ? 'success' : 'error'"
                            variant="soft"
                          />
                        </div>
                        <blockquote class="aw-quote">
                          «{{
                            expandedQuotes.has(group.index)
                              ? plainQuote(group.item.quote)
                              : shortQuote(group.item.quote)
                          }}»
                        </blockquote>
                        <div class="aw-evidence-actions">
                          <UButton
                            v-if="group.item.confirmed"
                            :label="
                              isHumanConfirmed(group.item)
                                ? 'Основание подтверждено'
                                : 'Подтвердить основание'
                            "
                            icon="i-lucide-check"
                            color="success"
                            variant="soft"
                            size="xs"
                            :disabled="basisBusy || isHumanConfirmed(group.item)"
                            @click="
                              saveHumanAction(() =>
                                backend.confirmBasis(props.runId, props.positionIndex, group.index),
                              )
                            "
                          />
                          <UButton
                            label="К основанию"
                            icon="i-lucide-crosshair"
                            color="primary"
                            variant="soft"
                            size="xs"
                            @click="basis(group.index)"
                          />
                          <UButton
                            v-if="plainQuote(group.item.quote).length > EVIDENCE_PREVIEW_LENGTH"
                            :label="
                              expandedQuotes.has(group.index) ? 'Свернуть' : 'Показать полностью'
                            "
                            color="neutral"
                            variant="ghost"
                            size="xs"
                            @click="toggleQuote(group.index)"
                          />
                          <span class="aw-evidence-file">{{ group.item.filename }}</span>
                        </div>
                      </UCard>
                    </div>
                  </section>

                  <section aria-label="Таблица ВОР">
                    <h2 class="aw-section-title aw-table-head">
                      <span>
                        {{ sheetName || 'Позиция CSV' }} ·
                        {{ quantityCell || workspace.row.number }}
                      </span>
                      <UButton
                        icon="i-lucide-download"
                        color="neutral"
                        variant="ghost"
                        size="xs"
                        aria-label="Скачать исходную ВОР"
                        title="Скачать исходную ВОР"
                        @click="downloadSource"
                      />
                    </h2>
                    <div v-if="workspace.excel" class="sheet-scroll">
                      <table class="sheet">
                        <thead>
                          <tr>
                            <th scope="col">Строка</th>
                            <th v-for="col in workspace.excel.columns" :key="col" scope="col">
                              {{ col }}
                            </th>
                          </tr>
                        </thead>
                        <tbody>
                          <tr
                            v-for="row in workspace.excel.rows"
                            :key="row.number"
                            :class="{ selected: row.selected }"
                          >
                            <th scope="row">{{ row.number }}</th>
                            <td
                              v-for="cell in row.cells"
                              :key="cell.coordinate"
                              :colspan="cell.column_span"
                              :rowspan="cell.row_span"
                              :class="{
                                question: cell.highlight,
                                numbers: isNumericCell(cell.value),
                              }"
                            >
                              {{ cell.value }}
                            </td>
                          </tr>
                        </tbody>
                      </table>
                    </div>
                    <div v-else class="panel aw-fallback">
                      <p>{{ workspace.position.work_name }}</p>
                      <p>{{ workspace.position.reference }}</p>
                      <p>{{ workspace.position.formula }}</p>
                    </div>
                    <details v-if="workspace.position.comment" :key="positionIndex" class="aw-note">
                      <summary>Примечание</summary>
                      <p>{{ workspace.position.comment }}</p>
                    </details>
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
.aw-fragment {
  display: grid;
  gap: 8px;
  padding: 10px;
  border: 1px solid var(--ui-border);
  border-radius: 6px;
}
.aw-basis-editor {
  max-height: 40vh;
  overflow-y: auto;
  flex-shrink: 0;
  display: grid;
  gap: 8px;
  padding: 12px;
  border: 1px solid var(--ui-border);
  border-radius: 8px;
}
.aw-dossier {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: var(--ui-bg-muted);
  color: var(--ui-text);
}
.aw-loading {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 12px 16px;
  flex: 1;
  min-height: 0;
}
.aw-loading-title {
  height: 28px;
  width: min(420px, 70%);
}
.aw-loading-line {
  height: 16px;
  width: min(560px, 85%);
}
.aw-loading-panes {
  display: grid;
  grid-template-columns: 62fr 38fr;
  gap: 12px;
  flex: 1;
  min-height: 280px;
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
  align-items: center;
  gap: 4px;
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
.aw-subline {
  margin: 0;
  font-size: 12px;
  color: var(--ui-text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.aw-verdict {
  border-bottom: 1px solid var(--ui-border);
  padding: 12px 16px;
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  align-items: flex-start;
}
.aw-verdict-main {
  min-width: min(100%, 420px);
  flex: 1;
}
.aw-verdict-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}
.aw-verdict-queue {
  font-size: 12px;
  color: var(--ui-text-muted);
}
.aw-verdict-title {
  margin: 0;
  font-size: 18px;
  font-weight: 700;
  line-height: 1.35;
}
.aw-verdict-action {
  margin: 4px 0 0;
  font-size: 14px;
  color: var(--ui-text);
}
.aw-compare {
  margin: 0;
  padding: 10px 14px;
  border: 1px solid var(--ui-border);
  border-radius: 8px;
  background: var(--ui-bg-elevated);
  flex: 0 1 340px;
}
.aw-compare-values {
  display: flex;
  align-items: center;
  gap: 12px;
}
.aw-compare-side {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1;
}
.aw-compare-label {
  font-size: 11px;
  text-transform: uppercase;
  color: var(--ui-text-muted);
  white-space: nowrap;
}
.aw-compare-number {
  font-size: 28px;
  font-weight: 700;
  line-height: 1.15;
  color: var(--ui-text-highlighted);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.aw-compare-sign {
  font-size: 24px;
  font-weight: 700;
  color: var(--ui-text-muted);
  flex-shrink: 0;
}
.aw-compare-sign.match {
  color: var(--ui-success);
}
.aw-compare-sign.diff {
  color: var(--ui-error);
}
.aw-compare-delta {
  margin: 6px 0 0;
  font-size: 13px;
  color: var(--ui-text-muted);
  font-variant-numeric: tabular-nums;
}
.aw-verdict-more {
  background: none;
  border: none;
  padding: 0;
  margin-top: 6px;
  color: var(--ui-primary);
  font: inherit;
  font-size: 14px;
  cursor: pointer;
  text-align: left;
}
.aw-verdict-more:hover {
  text-decoration: underline;
}
.aw-verdict-rest {
  margin: 6px 0 0;
  font-size: 15px;
  line-height: 1.5;
  color: var(--ui-text-muted);
}
.aw-verdict-details {
  margin: 6px 0 0;
  font-size: 13px;
  line-height: 1.55;
  color: var(--ui-text-muted);
  overflow-wrap: anywhere;
}
.aw-table-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.aw-tabs {
  display: none;
  gap: 12px;
  padding: 12px 16px 0;
}
.aw-main {
  flex: 1;
  min-height: 0;
  padding: 12px 16px 16px;
}
.aw-splitter {
  height: 100%;
  min-height: 560px;
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
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
  flex: 1;
  width: 100%;
  height: 100%;
  min-height: 0;
  padding-right: 0;
}
.aw-outline-panel {
  border: 1px solid var(--ui-border);
  border-radius: 8px;
  background: var(--ui-bg-elevated);
  padding: 8px;
  margin-top: 8px;
  display: grid;
  gap: 8px;
  max-height: 260px;
  overflow: auto;
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
  font-size: 14px;
}
.aw-page {
  font-size: 13px;
  color: var(--ui-text-muted);
  font-variant-numeric: tabular-nums;
}
.aw-pdf-search {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: min(100%, 280px);
}
.aw-filename {
  margin: 0;
  font-size: 12px;
  color: var(--ui-text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.aw-dossier-pane {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
  flex: 1;
  width: 100%;
  height: 100%;
  min-height: 0;
  overflow: auto;
  padding-left: 0;
}
.aw-section-title {
  margin: 0 0 6px;
  font-size: 14px;
  font-weight: 700;
  color: var(--ui-text-highlighted);
}
.aw-evidence-list {
  display: grid;
  gap: 8px;
}
.aw-evidence-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}
.aw-evidence-page {
  font-size: 12px;
  color: var(--ui-text-muted);
  font-variant-numeric: tabular-nums;
}
.aw-quote {
  margin: 0;
  padding-left: 8px;
  border-left: 2px solid var(--ui-primary);
  font-size: 14px;
  line-height: 1.65;
  white-space: pre-line;
  color: var(--ui-text);
  overflow-wrap: anywhere;
}
.aw-evidence-actions {
  margin-top: 8px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.aw-evidence-file {
  margin-left: auto;
  max-width: 100%;
  font-size: 11px;
  color: var(--ui-text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sheet-scroll {
  width: 100%;
  min-width: 0;
  border: 1px solid var(--ui-border);
  border-radius: 8px;
}
.sheet {
  width: 100%;
  table-layout: fixed;
  border-collapse: collapse;
  font-size: 12px;
  background: var(--ui-bg-elevated);
}
.sheet thead {
  position: sticky;
  top: 0;
  background: var(--ui-bg-muted);
}
.sheet th,
.sheet td {
  border: 1px solid var(--ui-border);
  padding: 4px 8px;
  text-align: left;
  vertical-align: top;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
  white-space: normal;
}
.sheet th:first-child {
  width: 9%;
}
.sheet th:nth-child(2) {
  width: 7%;
}
.sheet th:nth-child(3) {
  width: 38%;
}
.sheet th:nth-child(4) {
  width: 9%;
}
.sheet th:nth-child(5) {
  width: 15%;
}
.sheet th:nth-child(6) {
  width: 22%;
}
.aw-note {
  margin-top: 8px;
  overflow-wrap: anywhere;
}
.aw-note summary {
  cursor: pointer;
  color: var(--ui-primary);
  padding: 8px 0;
}
.aw-note p {
  white-space: pre-wrap;
  padding: 8px;
  background: var(--ui-bg-muted);
}
.sheet td.numbers,
.sheet th.numbers {
  text-align: right;
}
.sheet thead th {
  color: var(--ui-text-muted);
  font-weight: 600;
}
.sheet tbody th {
  color: var(--ui-text-muted);
  font-variant-numeric: tabular-nums;
}
.sheet tbody tr.selected {
  background: var(--app-selected-bg);
}
.sheet .question {
  background: var(--app-warning-bg);
  color: var(--app-warning-text);
}
.aw-fallback {
  padding: 16px;
  font-size: 14px;
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
  .aw-splitter {
    min-height: 0;
  }
  .aw-pdf,
  .aw-dossier-pane {
    padding: 0;
  }
  .aw-verdict {
    padding: 12px;
  }
}
</style>

<script setup lang="ts">
import type {
  WorkspaceDto,
  VersionDto,
  StructureDto,
  StructureNodeDto,
} from '~/generated/types.gen'
import { queueColor, queueLabel, quantityWithUnit } from '~/utils/presentation'
const props = defineProps<{ runId: string; positionIndex: number; documents: VersionDto[] }>(),
  emit = defineEmits<{ close: [] }>(),
  backend = useBackend(),
  workspace = ref<WorkspaceDto | null>(null),
  structure = ref<StructureDto | null>(null),
  version = ref(''),
  page = ref(1),
  selectedNode = ref(''),
  sourceIndex = ref(0),
  error = ref(''),
  structureError = ref(''),
  busy = ref(true),
  formatting = ref<string[] | null>(null),
  checking = ref(false),
  search = ref(''),
  pdfPanel = ref<{ find: (q: string) => Promise<number[]> }>(),
  searchPages = ref<number[]>([])
const source = computed(() => workspace.value?.evidence[sourceIndex.value]),
  isBasis = computed(
    () =>
      !!source.value &&
      source.value.version_id === version.value &&
      source.value.page === page.value &&
      !selectedNode.value,
  ),
  region = computed(() => (isBasis.value ? source.value?.region : null)),
  pdfs = computed(() => props.documents.filter((v) => v.kind === 'pdf')),
  printed = computed(
    () => structure.value?.pages.find((p) => p.physical_page === page.value)?.printed_page,
  )
let active = true,
  epoch = 0
const kindLabels: Record<string, string> = {
  work: 'работа',
  volume: 'количество',
  unit: 'единица',
  comment: 'условия',
  reference: 'ссылка ВОР',
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
  sourceIndex.value = index
  const item = workspace.value?.evidence[index]
  if (item) {
    version.value = item.version_id
    page.value = item.page
    selectedNode.value = ''
  }
}
function selectNode(node: StructureNodeDto) {
  page.value = node.physical_start
  selectedNode.value = node.id
}
function changePage(value: number) {
  page.value = value
  selectedNode.value = ''
}
async function checkFormat() {
  if (!workspace.value) return
  checking.value = true
  try {
    const data = await backend.formatting(
      workspace.value.vor_version_id,
      workspace.value.position.sheet,
    )
    formatting.value = data.issues
      .filter((i) => i.status !== 'ОК')
      .map((i) => `${i.coord}: ${i.error || i.expected}`)
  } catch (e) {
    error.value = readableError(e)
  } finally {
    checking.value = false
  }
}
async function find() {
  if (pdfPanel.value) searchPages.value = await pdfPanel.value.find(search.value)
}
watch(version, loadStructure)
onMounted(async () => {
  try {
    workspace.value = await backend.workspace(props.runId, props.positionIndex)
    if (!active) return
    if (workspace.value.evidence.length) basis(0)
    else {
      version.value = pdfs.value[0]?.id || ''
      page.value = 1
    }
  } catch (e) {
    if (active) error.value = readableError(e)
  } finally {
    if (active) busy.value = false
  }
})
function escape(event: KeyboardEvent) {
  if (event.key === 'Escape') emit('close')
}
onMounted(() => window.addEventListener('keydown', escape))
onBeforeUnmount(() => {
  active = false
  epoch++
  window.removeEventListener('keydown', escape)
})
</script>
<template>
  <section class="workspace" role="dialog" aria-modal="true" aria-label="Сверка ВОР с PDF">
    <header class="toolbar spread workspace-header">
      <b>{{ workspace?.row.number }} · {{ workspace?.row.name || 'Открываем позицию…' }}</b
      ><UButton color="neutral" variant="outline" @click="emit('close')">Закрыть ×</UButton>
    </header>
    <p v-if="busy" class="empty" role="status">Открываем источники…</p>
    <p v-if="error" class="error-box" role="alert">{{ error }}</p>
    <template v-if="workspace"
      ><div class="instruction">
        <div class="toolbar">
          <UBadge :color="queueColor(workspace.row.queue)">{{
            queueLabel(workspace.row.queue)
          }}</UBadge>
          <h1>{{ workspace.row.problem }}</h1>
        </div>
        <p>{{ workspace.row.action }}</p>
      </div>
      <div class="workspace-grid">
        <aside class="outline-pane">
          <USelect
            v-model="version"
            :items="pdfs.map((v) => ({ label: v.filename, value: v.id }))"
            aria-label="Проектный PDF"
            class="w-full"
          /><OutlineTree
            :data="structure"
            :page="page"
            :selected-id="selectedNode"
            :error="structureError"
            style="margin-top: 16px"
            @select="selectNode"
          />
        </aside>
        <div class="pdf-pane">
          <div class="pane-toolbar">
            <span class="muted"
              >Физическая страница {{ page }} · печатная {{ printed || 'не установлена' }}</span
            >
            <div class="toolbar" style="margin-top: 8px">
              <UInput
                v-model="search"
                placeholder="Текст в PDF"
                aria-label="Поиск в PDF"
                @keyup.enter="find"
              /><UButton size="xs" variant="outline" @click="find">Найти</UButton
              ><UButton v-if="source" size="xs" variant="outline" @click="basis()"
                >К основанию</UButton
              >
            </div>
            <p v-if="searchPages.length" class="muted">
              Найдено на страницах: {{ searchPages.join(', ') }}
            </p>
            <p v-if="source && !isBasis" class="muted">
              Просмотр другой страницы. Заключение по позиции не изменилось.
            </p>
            <p v-if="region?.reason" class="muted">{{ region.reason }}</p>
          </div>
          <PdfPanel
            v-if="version"
            ref="pdfPanel"
            :key="version"
            :source-url="backend.contentUrl(version)"
            :page="page"
            :region="region"
            @page="changePage"
          />
          <p v-else class="empty">Для этого аудита PDF не выбран.</p>
        </div>
        <aside class="excel-pane">
          <h2>
            {{ workspace.position.sheet || 'Позиция CSV' }} ·
            {{ workspace.row.cells.quantity || workspace.row.number }}
          </h2>
          <div class="values">
            <div>
              <small>ВОР</small
              ><strong>{{
                quantityWithUnit(workspace.row.declared_quantity, workspace.row.unit)
              }}</strong>
            </div>
            <div>
              <small>По проекту</small
              ><strong>{{
                quantityWithUnit(workspace.row.found_quantity, workspace.row.found_unit)
              }}</strong>
            </div>
          </div>
          <div v-if="workspace.excel" class="sheet-scroll">
            <table class="sheet">
              <thead>
                <tr>
                  <th></th>
                  <th v-for="col in workspace.excel.columns" :key="col">{{ col }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in workspace.excel.rows" :key="row.number">
                  <th>{{ row.number }}</th>
                  <td
                    v-for="cell in row.cells"
                    :key="cell.coordinate"
                    :colspan="cell.column_span"
                    :rowspan="cell.row_span"
                    :class="{ question: cell.highlight }"
                  >
                    {{ cell.value }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-else class="panel" style="padding: 12px; margin: 12px 0">
            <p>{{ workspace.position.work_name }}</p>
            <p>{{ workspace.position.reference }}</p>
            <p>{{ workspace.position.formula }}</p>
          </div>
          <div class="toolbar">
            <UButton
              variant="outline"
              @click="
                backend
                  .download(
                    backend.contentUrl(workspace.vor_version_id) + '?download=true',
                    'ВОР.' + (workspace.excel ? 'xlsx' : 'csv'),
                  )
                  .catch((e) => (error = readableError(e)))
              "
              >Скачать исходную ВОР</UButton
            ><UButton
              v-if="workspace.excel"
              variant="outline"
              :loading="checking"
              @click="checkFormat"
              >Проверить оформление</UButton
            >
          </div>
          <details v-if="formatting" open class="details">
            <summary>Оформление: {{ formatting.length }} замечаний</summary>
            <p v-for="(issue, i) in formatting" :key="i">{{ issue }}</p>
          </details>
          <h2 style="margin-top: 24px">Основания в проекте</h2>
          <p v-if="!workspace.evidence.length" class="muted">
            Подтверждённый источник не установлен. Документ слева открыт для ручной сверки.
          </p>
          <div
            v-for="group in evidenceGroups"
            :key="group.index"
            class="panel"
            style="padding: 12px; margin: 10px 0"
          >
            <UButton variant="link" @click="basis(group.index)"
              >{{ group.item.filename }} · стр. {{ group.item.page }}</UButton
            >
            <p>
              {{ group.item.quote.slice(0, 240) }}{{ group.item.quote.length > 240 ? '…' : '' }}
            </p>
            <details v-if="group.item.quote.length > 240" class="details">
              <summary>Полная цитата</summary>
              <p>{{ group.item.quote }}</p>
            </details>
            <small class="muted">{{
              group.item.confirmed
                ? 'Основание: ' + group.kinds.map((kind) => kindLabels[kind] || kind).join(', ')
                : 'Ссылка из ВОР; соответствие не подтверждено'
            }}</small>
          </div>
          <details class="details">
            <summary>Подробности проверки</summary>
            <p>{{ workspace.row.audit_status }} · {{ workspace.row.details }}</p>
            <p>Формула / пояснение ВОР: {{ workspace.position.formula || 'не указано' }}</p>
            <p>Ссылка на страницы: {{ workspace.position.page_ref }}</p>
          </details>
        </aside>
      </div>
    </template>
  </section>
</template>

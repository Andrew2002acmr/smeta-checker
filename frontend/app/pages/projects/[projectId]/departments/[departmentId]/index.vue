<script setup lang="ts">
import type { ProjectDto, RunDto, DocumentDto, DepartmentDto } from '~/generated/types.gen'
import type { TableColumn } from '@nuxt/ui'
import type { SortingState } from '@tanstack/vue-table'
type Tab = 'vor' | 'pdf' | 'runs'
type Queue = 'fix' | 'review' | 'missing' | 'ok'
type SummaryState =
  | { status: 'loading' }
  | { status: 'error' }
  | { status: 'ready'; queues: Record<Queue, number> }

const queueLabels: Record<Queue, string> = {
  fix: 'Исправить',
  review: 'Проверить вручную',
  missing: 'Не выполнено',
  ok: 'Подтверждено',
}

const backend = useBackend()
const route = useRoute()
const router = useRouter()

const projectId = computed(() => String(route.params.projectId || ''))
const departmentId = computed(() => String(route.params.departmentId || ''))
const department = ref<DepartmentDto | null>(null)
const workspaceArchived = computed(() => Boolean(project.value?.archived))
const tab = computed<Tab>(() =>
  route.query.tab === 'pdf' || route.query.tab === 'runs' ? route.query.tab : 'vor',
)

const project = ref<ProjectDto | null>(null)
const documents = ref<DocumentDto[]>([])
const runs = ref<RunDto[]>([])
const summaries = ref<Record<string, SummaryState>>({})
let catalogEpoch = 0
const busy = ref(true)
const runsOffset = ref(0)
const runsBusy = ref(false)
let runsEpoch = 0
const error = ref('')
const search = ref('')
const showArchived = ref(false)

const renamingDocId = ref<string | null>(null)
const docNameDraft = ref('')
const docBusy = ref(false)
const archivingDocId = ref<string | null>(null)
const deletingDocId = ref<string | null>(null)
const deletingDocBusy = ref(false)
const archiveBusy = ref(false)

const importOpen = ref(false)
const revisionTargetId = ref<string | undefined>(undefined)

let active = true
let epoch = 0

const vorDocs = computed(() => documents.value.filter((d) => d.kind === 'vor'))
const pdfDocs = computed(() => documents.value.filter((d) => d.kind === 'pdf'))

const visibleDocs = computed(() => {
  const q = search.value.trim().toLocaleLowerCase('ru-RU')
  const base = tab.value === 'pdf' ? pdfDocs.value : vorDocs.value
  if (!q) return base
  return base.filter(
    (d) =>
      d.title.toLocaleLowerCase('ru-RU').includes(q) ||
      d.versions.some((v) => v.filename.toLocaleLowerCase('ru-RU').includes(q)),
  )
})
const runColumns: TableColumn<RunDto>[] = [
  { id: 'vor', accessorFn: (row: RunDto) => row.vor_filename, header: 'Ведомость' },
  { id: 'status', accessorFn: (row: RunDto) => row.status, header: 'Статус' },
  { id: 'date', accessorFn: (row: RunDto) => row.created_at, header: 'Дата' },
  { id: 'action', header: '', enableSorting: false },
]
const runSorting = ref<SortingState>([])
const docColumns: TableColumn<DocumentDto>[] = [
  { id: 'doc', accessorFn: (row: DocumentDto) => row.title, header: 'Документ' },
  {
    id: 'revision',
    accessorFn: (row: DocumentDto) => row.versions.length,
    header: 'Текущая ревизия',
  },
  { id: 'status', accessorFn: (row: DocumentDto) => row.last_run?.status || '', header: 'Статус' },
  { id: 'action', header: '', enableSorting: false },
]
const docSorting = ref<SortingState>([])
function setTab(next: Tab) {
  router.replace({ query: { ...route.query, tab: next === 'vor' ? undefined : next } })
}

function formatDate(value: string | null) {
  return value
    ? new Date(value).toLocaleString('ru-RU', {
        day: 'numeric',
        month: 'short',
        hour: '2-digit',
        minute: '2-digit',
      })
    : '—'
}

function statusColor(status: RunDto['status']): 'success' | 'error' | 'warning' | 'neutral' {
  return status === 'done' ? 'success' : status === 'failed' ? 'error' : 'warning'
}

function currentVersion(doc: DocumentDto) {
  return doc.versions.find((v) => v.id === doc.current_version_id) || doc.versions[0] || null
}

function summaryText(runId: string) {
  const state = summaries.value[runId]
  if (!state || state.status !== 'ready') return ''
  return (
    (Object.keys(queueLabels) as Queue[])
      .filter((queue) => state.queues[queue] > 0)
      .map((queue) => `${queueLabels[queue]} · ${state.queues[queue]}`)
      .join(' / ') || 'Проверка без позиций'
  )
}
async function loadSummary(runId: string, current: number) {
  summaries.value[runId] = { status: 'loading' }
  try {
    const summary = await backend.summary(runId)
    if (!active || current !== catalogEpoch) return
    summaries.value[runId] = {
      status: 'ready',
      queues: {
        fix: summary.queues.fix ?? 0,
        review: summary.queues.review ?? 0,
        missing: summary.queues.missing ?? 0,
        ok: summary.queues.ok ?? 0,
      },
    }
  } catch {
    if (active && current === catalogEpoch) summaries.value[runId] = { status: 'error' }
  }
}

function setCatalog(catalog: DocumentDto[]) {
  documents.value = catalog
  const current = ++catalogEpoch
  summaries.value = {}
  const ids = new Set(
    catalog
      .filter((doc) => doc.kind === 'vor' && doc.last_run?.status === 'done')
      .map((doc) => doc.last_run!.run_id),
  )
  for (const id of ids) void loadSummary(id, current)
}

function openDocument(doc: DocumentDto) {
  router.push(`/projects/${projectId.value}/documents/${doc.id}`)
}

function openImport(targetDocumentId?: string) {
  revisionTargetId.value = targetDocumentId
  importOpen.value = true
}

function closeImport() {
  importOpen.value = false
  revisionTargetId.value = undefined
  if (route.query.import) router.replace({ query: { ...route.query, import: undefined } })
}

async function afterSaved() {
  closeImport()
  await loadCatalog()
}

function startDocRename(doc: DocumentDto) {
  renamingDocId.value = doc.id
  docNameDraft.value = doc.title
}

async function saveDocRename(doc: DocumentDto) {
  const title = docNameDraft.value.trim()
  if (!title || docBusy.value) return
  docBusy.value = true
  try {
    const updated = await backend.updateDocument(doc.id, { title })
    doc.title = updated.title
    renamingDocId.value = null
  } catch (e) {
    error.value = readableError(e)
  } finally {
    docBusy.value = false
  }
}

function askDeleteDoc(id: string) {
  deletingDocId.value = id
}

async function confirmDeleteDoc() {
  if (!deletingDocId.value || deletingDocBusy.value) return
  deletingDocBusy.value = true
  try {
    await backend.deleteDocument(deletingDocId.value)
    deletingDocId.value = null
    await load()
  } catch (e) {
    error.value = readableError(e)
  } finally {
    deletingDocBusy.value = false
  }
}

async function setDocArchived(doc: DocumentDto, archived: boolean) {
  if (archiveBusy.value) return
  archiveBusy.value = true
  try {
    await backend.updateDocument(doc.id, { archived })
    archivingDocId.value = null
    await loadCatalog()
  } catch (e) {
    error.value = readableError(e)
  } finally {
    archiveBusy.value = false
  }
}

function reset() {
  project.value = null
  documents.value = []
  summaries.value = {}
  catalogEpoch++
  runs.value = []
  error.value = ''
  renamingDocId.value = null
  archivingDocId.value = null
  closeImportSilent()
}

function closeImportSilent() {
  importOpen.value = false
  revisionTargetId.value = undefined
}

async function loadCatalog() {
  const id = projectId.value
  const archived = showArchived.value
  try {
    const catalog = await backend.catalog(id, archived, departmentId.value)
    if (active && projectId.value === id && showArchived.value === archived) setCatalog(catalog)
  } catch (e) {
    if (active && projectId.value === id) error.value = readableError(e)
  }
}

async function loadRuns(offset: number) {
  const current = ++runsEpoch
  const id = projectId.value
  runsBusy.value = true
  try {
    const loaded = await backend.runs(id, offset, departmentId.value)
    if (!active || current !== runsEpoch || projectId.value !== id) return
    runs.value = loaded
    runsOffset.value = offset
  } catch (e) {
    if (active && current === runsEpoch) error.value = readableError(e)
  } finally {
    if (active && current === runsEpoch) runsBusy.value = false
  }
}

async function load() {
  const current = ++epoch
  reset()
  runsEpoch++
  runsOffset.value = 0
  runsBusy.value = false
  busy.value = true
  try {
    await backend.connect()
    if (!active || current !== epoch) return
    const id = projectId.value
    const [loaded, catalog, history, loadedDepartment] = await Promise.all([
      backend.project(id),
      backend.catalog(id, showArchived.value, departmentId.value),
      backend.runs(id, 0, departmentId.value),
      backend.department(id, departmentId.value),
    ])
    if (!active || current !== epoch || projectId.value !== id) return
    project.value = loaded
    department.value = loadedDepartment
    setCatalog(catalog)
    runs.value = history
  } catch (e) {
    if (active && current === epoch) error.value = readableError(e)
  } finally {
    if (active && current === epoch) busy.value = false
  }
}

watch([projectId, departmentId], () => load())
watch(showArchived, () => loadCatalog())

onMounted(() => {
  load()
  if (route.query.import === '1') importOpen.value = true
})
onBeforeUnmount(() => {
  active = false
  epoch++
  catalogEpoch++
})
</script>

<template>
  <div>
    <WorkspaceHeader active="check" />
    <main id="main-content" tabindex="-1" class="shell project">
      <nav aria-label="Навигация">
        <UButton
          :to="`/projects/${projectId}`"
          variant="link"
          color="neutral"
          icon="i-lucide-arrow-left"
          >Отделы проекта</UButton
        >
      </nav>

      <template v-if="busy && !project && !error">
        <USkeleton class="project-title-skeleton" />
        <p class="empty" role="status">Загружаем проект…</p>
      </template>

      <UAlert
        v-else-if="error && !project"
        color="error"
        variant="soft"
        title="Не удалось загрузить проект"
        :description="error"
      >
        <template #actions>
          <UButton color="error" variant="soft" size="sm" @click="load()">Повторить</UButton>
        </template>
      </UAlert>

      <template v-else-if="project">
        <div class="page-heading">
          <div class="project-heading-main">
            <p class="muted">{{ project.name }}</p>
            <h1 class="page-title department-workspace-title">{{ department?.name }}</h1>
          </div>
          <div class="project-heading-side">
            <UButton
              icon="i-lucide-plus"
              color="primary"
              :disabled="workspaceArchived"
              @click="openImport()"
              >Добавить документы</UButton
            >
          </div>
        </div>

        <UAlert
          v-if="workspaceArchived"
          color="neutral"
          variant="soft"
          title="Раздел в архиве"
          description="Восстановите проект, чтобы добавлять документы и запускать проверки."
        />

        <UAlert
          v-if="error"
          color="error"
          variant="soft"
          :title="error"
          role="alert"
          class="project-alert"
        />

        <div class="project-tabs" role="group" aria-label="Разделы проекта">
          <UButton
            :variant="tab === 'vor' ? 'soft' : 'ghost'"
            color="neutral"
            :aria-pressed="tab === 'vor'"
            @click="setTab('vor')"
            >Ведомости ({{ vorDocs.length }})</UButton
          >
          <UButton
            :variant="tab === 'pdf' ? 'soft' : 'ghost'"
            color="neutral"
            :aria-pressed="tab === 'pdf'"
            @click="setTab('pdf')"
            >ПД ({{ pdfDocs.length }})</UButton
          >
          <UButton
            :variant="tab === 'runs' ? 'soft' : 'ghost'"
            color="neutral"
            :aria-pressed="tab === 'runs'"
            @click="setTab('runs')"
            >Проверки</UButton
          >
        </div>

        <template v-if="tab === 'runs'">
          <div v-if="!runs.length" class="panel project-empty">
            <UIcon name="i-lucide-clipboard-list" class="project-empty-icon" aria-hidden="true" />
            <h2>Проверок пока нет</h2>
            <p class="muted">Добавьте ведомость и проектные PDF, затем запустите проверку.</p>
          </div>
          <div v-else class="panel project-panel" :aria-busy="busy">
            <UTable
              sticky="header"
              :data="runs"
              :columns="runColumns"
              class="results project-table max-h-[60vh] overflow-auto"
            >
              <template #vor-cell="{ row }">
                <span class="project-cell-name">{{ row.original.vor_filename }}</span>
              </template>
              <template #status-cell="{ row }">
                <UBadge :color="statusColor(row.original.status)" variant="subtle">
                  {{ runLabel(row.original.status) }}
                </UBadge>
              </template>
              <template #date-cell="{ row }">
                <span class="muted">{{ formatDate(row.original.created_at) }}</span>
              </template>
              <template #action-cell="{ row }">
                <UButton variant="ghost" :to="`/audits/${row.original.run_id}`">
                  Открыть результат
                </UButton>
              </template>
            </UTable>
          </div>
          <div v-if="runsOffset || runs.length === 50" class="toolbar">
            <UButton
              :disabled="runsBusy || runsOffset === 0"
              @click="loadRuns(Math.max(0, runsOffset - 50))"
              >Назад</UButton
            >
            <span class="muted">Запуски {{ runsOffset + 1 }}–{{ runsOffset + runs.length }}</span>
            <UButton :disabled="runsBusy || runs.length < 50" @click="loadRuns(runsOffset + 50)"
              >Далее</UButton
            >
          </div>
        </template>

        <template v-else>
          <div class="project-toolbar">
            <UInput
              v-model="search"
              icon="i-lucide-search"
              placeholder="Найти документ…"
              aria-label="Поиск документов"
              class="project-search"
            />
            <UButton
              color="neutral"
              :variant="showArchived ? 'soft' : 'outline'"
              icon="i-lucide-archive"
              :aria-pressed="showArchived"
              @click="showArchived = !showArchived"
              >{{ showArchived ? 'Архив: показаны' : 'Архив' }}</UButton
            >
          </div>

          <p v-if="!visibleDocs.length" class="empty" role="status">
            {{
              search
                ? `По запросу «${search}» ничего не найдено.`
                : tab === 'vor'
                  ? 'Ведомостей пока нет. Добавьте первую ведомость.'
                  : 'Проектных PDF пока нет. Добавьте документы ПД.'
            }}
          </p>

          <div v-else class="panel project-panel" :aria-busy="busy">
            <UTable
              sticky="header"
              :data="visibleDocs"
              :columns="docColumns"
              class="results project-table max-h-[60vh] overflow-auto"
            >
              <template #doc-cell="{ row }">
                <div>
                  <template v-if="renamingDocId === row.original.id">
                    <div class="project-rename" @click.stop>
                      <UInput
                        v-model="docNameDraft"
                        :aria-label="`Новое название документа ${row.original.title}`"
                        :disabled="docBusy"
                        autofocus
                        @keydown.enter="saveDocRename(row.original)"
                        @keydown.escape="renamingDocId = null"
                      />
                      <UButton
                        icon="i-lucide-check"
                        :aria-label="`Сохранить название документа ${row.original.title}`"
                        color="primary"
                        :loading="docBusy"
                        @click="saveDocRename(row.original)"
                      />
                      <UButton
                        icon="i-lucide-x"
                        :aria-label="`Отменить переименование документа ${row.original.title}`"
                        color="neutral"
                        variant="ghost"
                        :disabled="docBusy"
                        @click="renamingDocId = null"
                      />
                    </div>
                  </template>
                  <template v-else>
                    <div class="project-name-row">
                      <NuxtLink
                        :to="`/projects/${projectId}/documents/${row.original.id}`"
                        class="project-doc-link"
                        >{{ row.original.title }}</NuxtLink
                      >
                      <UButton
                        icon="i-lucide-pencil"
                        :aria-label="`Переименовать документ ${row.original.title}`"
                        color="neutral"
                        variant="ghost"
                        size="xs"
                        @click="startDocRename(row.original)"
                      />
                    </div>
                  </template>
                  <p class="muted">Ревизий: {{ row.original.versions.length }}</p>
                </div>
              </template>
              <template #revision-cell="{ row }">
                <span class="muted">
                  <template v-if="currentVersion(row.original)">
                    {{ currentVersion(row.original)!.filename }} · №{{
                      currentVersion(row.original)!.number
                    }}
                  </template>
                  <template v-else>—</template>
                </span>
              </template>
              <template #status-cell="{ row }">
                <div class="project-status">
                  <div class="project-badges">
                    <UBadge v-if="row.original.archived" color="neutral" variant="soft" size="sm">
                      В архиве
                    </UBadge>
                    <template v-if="row.original.kind === 'vor' && row.original.last_run">
                      <UBadge
                        :color="statusColor(row.original.last_run.status)"
                        variant="subtle"
                        size="sm"
                      >
                        {{ runLabel(row.original.last_run.status) }}
                      </UBadge>
                      <UBadge
                        v-if="row.original.run_freshness === 'stale'"
                        color="warning"
                        variant="soft"
                        size="sm"
                      >
                        Проверка неактуальна
                      </UBadge>
                      <UBadge
                        v-if="row.original.vor_updated"
                        color="warning"
                        variant="soft"
                        size="sm"
                      >
                        ВОР обновлена
                      </UBadge>
                      <UBadge
                        v-if="row.original.pd_updated"
                        color="warning"
                        variant="soft"
                        size="sm"
                      >
                        ПД обновлена
                      </UBadge>
                      <UBadge
                        v-if="row.original.run_freshness === 'unavailable'"
                        color="neutral"
                        variant="soft"
                        size="sm"
                      >
                        Актуальность недоступна
                      </UBadge>
                    </template>
                    <span
                      v-else-if="
                        row.original.kind === 'vor' && row.original.run_freshness === 'unavailable'
                      "
                      class="muted"
                      >Результат недоступен</span
                    >
                    <span v-else-if="row.original.kind === 'vor'" class="muted"
                      >Не проверялась</span
                    >
                    <span v-else-if="!row.original.archived" class="muted">—</span>
                  </div>
                  <template v-if="row.original.kind === 'vor' && row.original.last_run">
                    <span class="muted"
                      >Последняя проверка: {{ formatDate(row.original.last_run.created_at) }}</span
                    >
                    <UProgress
                      v-if="
                        row.original.last_run.status === 'running' && row.original.last_run.total
                      "
                      :model-value="
                        Math.floor(
                          ((row.original.last_run.done + row.original.last_run.failed) /
                            row.original.last_run.total) *
                            100,
                        )
                      "
                      color="neutral"
                      size="xs"
                      class="project-progress"
                    />
                    <template v-if="row.original.last_run.status === 'done'">
                      <span
                        v-if="
                          !summaries[row.original.last_run.run_id] ||
                          summaries[row.original.last_run.run_id]?.status === 'loading'
                        "
                        class="muted"
                        role="status"
                      >
                        Загрузка результата…
                      </span>
                      <span
                        v-else-if="summaries[row.original.last_run.run_id]?.status === 'error'"
                        role="status"
                      >
                        Результат недоступен
                        <UButton
                          variant="link"
                          size="xs"
                          :aria-label="`Повторить загрузку результата ${row.original.title}`"
                          @click="loadSummary(row.original.last_run.run_id, catalogEpoch)"
                          >Повторить</UButton
                        >
                      </span>
                      <span v-else>{{ summaryText(row.original.last_run.run_id) }}</span>
                    </template>
                  </template>
                </div>
              </template>
              <template #action-cell="{ row }">
                <div class="project-actions">
                  <UButton
                    v-if="row.original.last_run"
                    variant="ghost"
                    :to="`/audits/${row.original.last_run.run_id}`"
                    >Открыть результат</UButton
                  >
                  <UButton
                    v-if="!row.original.archived"
                    variant="ghost"
                    color="neutral"
                    icon="i-lucide-file-plus-2"
                    :aria-label="`Новая ревизия ${row.original.title}`"
                    @click="openImport(row.original.id)"
                  />
                  <UButton
                    v-if="row.original.archived"
                    variant="ghost"
                    color="neutral"
                    icon="i-lucide-archive-restore"
                    :aria-label="`Восстановить документ ${row.original.title}`"
                    @click="setDocArchived(row.original, false)"
                  />
                  <UButton
                    v-else
                    variant="ghost"
                    color="neutral"
                    icon="i-lucide-archive"
                    :aria-label="`Архивировать документ ${row.original.title}`"
                    @click="archivingDocId = row.original.id"
                  />
                  <UButton
                    variant="ghost"
                    color="error"
                    icon="i-lucide-trash-2"
                    :aria-label="`Удалить документ ${row.original.title}`"
                    @click="askDeleteDoc(row.original.id)"
                  />
                </div>
              </template>
            </UTable>
          </div>
        </template>

        <UModal
          v-model:open="importOpen"
          :title="
            revisionTargetId
              ? `Новая ревизия · ${documents.find((d) => d.id === revisionTargetId)?.title || ''}`
              : 'Добавить документы'
          "
          description="XLSX/CSV и PDF · до 100 МБ · до 50 файлов"
          :ui="{ content: 'max-w-3xl' }"
          @update:open="(open: boolean) => !open && closeImport()"
        >
          <template #body>
            <DocumentImport
              :project-id="projectId"
              :department-id="departmentId"
              :project-name="project.name"
              :documents="documents"
              @changed="loadCatalog"
              :target-document-id="revisionTargetId"
              @saved="afterSaved"
            />
          </template>
        </UModal>

        <UModal
          :open="archivingDocId !== null"
          title="Архивировать документ?"
          @update:open="(open: boolean) => !open && (archivingDocId = null)"
        >
          <template #body>
            <p class="muted modal-text">
              Документ «{{ documents.find((d) => d.id === archivingDocId)?.title }}» исчезнет из
              активного списка. Восстановить можно через фильтр «Архив».
            </p>
            <div class="toolbar spread modal-footer">
              <UButton color="neutral" variant="ghost" @click="archivingDocId = null"
                >Отмена</UButton
              >
              <UButton
                color="primary"
                :loading="archiveBusy"
                @click="
                  () => {
                    const doc = documents.find((d) => d.id === archivingDocId)
                    if (doc) setDocArchived(doc, true)
                  }
                "
                >Архивировать</UButton
              >
            </div>
          </template>
        </UModal>

        <UModal
          :open="deletingDocId !== null"
          title="Удалить документ?"
          @update:open="(open: boolean) => !open && (deletingDocId = null)"
        >
          <template #body>
            <p class="muted modal-text">
              Документ и все его ревизии будут удалены без восстановления.
            </p>
            <div class="toolbar spread modal-footer">
              <UButton
                color="neutral"
                variant="ghost"
                :disabled="deletingDocBusy"
                @click="deletingDocId = null"
              >
                Отмена
              </UButton>
              <UButton color="error" :loading="deletingDocBusy" @click="confirmDeleteDoc">
                Удалить
              </UButton>
            </div>
          </template>
        </UModal>
      </template>
    </main>
  </div>
</template>

<style scoped>
.department-workspace-title {
  overflow-wrap: anywhere;
}
.project-title-skeleton {
  height: 40px;
  width: min(420px, 80%);
  margin-bottom: 16px;
}
.project-heading-main {
  min-width: 0;
}
.project-heading-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 4px;
}
.project-heading-side {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.project-alert {
  margin-bottom: 16px;
}
.project-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 16px;
}
.project-toolbar {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
}
.project-search {
  flex: 1;
  min-width: min(100%, 240px);
}
.project-panel {
  overflow: auto;
  padding: 8px;
}
.modal-footer {
  margin-top: 12px;
}
.project-cell-name {
  overflow-wrap: anywhere;
}
.project-name-row {
  display: flex;
  align-items: center;
  gap: 4px;
}
.project-doc-link {
  font-weight: 650;
  color: var(--ui-text-highlighted);
  overflow-wrap: anywhere;
}
.project-rename {
  display: flex;
  align-items: center;
  gap: 8px;
}
.project-status {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
}
.project-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.project-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 4px;
  flex-wrap: wrap;
}
.project-empty {
  padding: 48px 24px;
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
}
.project-empty h2 {
  margin: 0;
  font-size: 20px;
}
.project-empty p {
  margin: 0;
}
.project-empty-icon {
  font-size: 40px;
  color: var(--ui-text-muted);
}
.row-action {
  text-align: right;
  white-space: nowrap;
}
.modal-text {
  padding: 0 24px 8px;
  overflow-wrap: anywhere;
}
.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}
@media (max-width: 700px) {
  .project-toolbar {
    flex-direction: column;
  }
  .project-table thead {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
  .project-table tbody,
  .project-table tr,
  .project-table td {
    display: block;
  }
  .project-table tr {
    padding: 12px 0;
  }
  .project-table tbody tr + tr {
    border-top: 1px solid var(--ui-border);
  }
  .row-action {
    text-align: left;
  }
  .project-actions {
    justify-content: flex-start;
  }
}
</style>

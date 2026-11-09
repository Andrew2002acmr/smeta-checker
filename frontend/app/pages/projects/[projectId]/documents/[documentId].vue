<script setup lang="ts">
import type { DocumentDto, PreflightResponse, PreflightRow, RunDto } from '~/generated/types.gen'
import type { TableColumn } from '@nuxt/ui'
const backend = useBackend()
const route = useRoute()
const router = useRouter()
definePageMeta({
  key: (pageRoute) => `${pageRoute.params.projectId}:${pageRoute.params.documentId}`,
})
let active = true
let documentEpoch = 0
let historyEpoch = 0

type Backend = typeof backend
type DocumentDetail = Awaited<ReturnType<Backend['document']>>
type RichVersion = DocumentDetail['versions'][number]
type BindingOption = {
  label: string
  value: string
  filename: string
  documentTitle: string
}

const projectId = computed(() => String(route.params.projectId || ''))
const documentId = computed(() => String(route.params.documentId || ''))
const requestedVersion = computed(() =>
  typeof route.query.version === 'string' ? route.query.version : '',
)

const projectName = ref('')
const projectArchived = ref(false)
const document = ref<DocumentDetail | null>(null)
const busy = ref(true)
const error = ref('')
const notice = ref('')

const editingTitle = ref(false)
const titleDraft = ref('')
const titleBusy = ref(false)

const revisionInput = ref<HTMLInputElement | null>(null)
const uploadBusy = ref(false)
const importOpen = ref(false)
const catalogDocuments = ref<DocumentDto[]>([])
const pendingDocumentDelete = ref<DocumentDto | null>(null)
const documentDeleteBusy = ref(false)

const preflight = ref<PreflightResponse | null>(null)
const preflightBusy = ref(false)
const preflightError = ref('')
const acknowledged = ref(false)
const starting = ref(false)
const startedRunId = ref('')
let preflightEpoch = 0

const pendingDelete = ref<RichVersion | null>(null)
const deleteBusy = ref(false)
const deleteOpen = ref(false)
const archiveBusy = ref(false)
const archiveOpen = ref(false)
const historyOffset = ref(0)
const historyRuns = ref<RunDto[]>([])
const historyBusy = ref(false)
const historyError = ref('')
const historyHasMore = ref(false)
const revisionsOpen = ref(true)
const runsOpen = ref(false)
const preflightColumns: TableColumn<PreflightRow>[] = [
  { accessorKey: 'vor', header: 'ВОР' },
  { accessorKey: 'pdf', header: 'PDF' },
  { accessorKey: 'refs', header: 'Ссылки' },
  { accessorKey: 'status', header: 'Статус' },
]
const historyColumns: TableColumn<RunDto>[] = [
  { id: 'run', header: 'Запуск' },
  { id: 'status', header: 'Статус' },
  { id: 'count', header: 'Позиций' },
  { id: 'action', header: 'Действие' },
]
const historyLimit = 50
const departmentName = ref('')
const departmentPath = computed(() =>
  document.value?.department_id
    ? `/projects/${projectId.value}/departments/${document.value.department_id}`
    : `/projects/${projectId.value}`,
)
const locksMutations = computed(() => projectArchived.value || document.value?.archived)
const kindLabel = computed(() => (document.value?.kind === 'pdf' ? 'ПД' : 'ВОР'))
const sortedVersions = computed<RichVersion[]>(() => document.value?.versions ?? [])
const isVor = computed(() => document.value?.kind === 'vor')
const isPdf = computed(() => document.value?.kind === 'pdf')
const manageableDocuments = computed(() =>
  catalogDocuments.value.filter((item) => item.id !== document.value?.id && !item.archived),
)
const selectedVersion = computed<RichVersion | null>(() => {
  if (!sortedVersions.value.length) return null
  const wanted = requestedVersion.value || document.value?.current_version_id || ''
  return (
    sortedVersions.value.find((v) => v.id === wanted) ||
    sortedVersions.value.find((v) => v.id === document.value?.current_version_id) ||
    sortedVersions.value[0] ||
    null
  )
})
const bindingReferences = ref<string[]>([])
const bindingDraft = ref<Record<string, string>>({})
const savedBindings = ref('{}')
const bindingOptions = ref<BindingOption[]>([])
const bindingsBusy = ref(false)
const bindingsError = ref('')
const bindingsManualOpen = ref(true)
let bindingEpoch = 0
const bindingsDirty = computed(() => JSON.stringify(bindingDraft.value) !== savedBindings.value)
const fileTypeLabel = computed(() => {
  if (isPdf.value) return 'PDF'
  const extension = selectedVersion.value?.filename.split('.').pop()?.toUpperCase()
  return extension === 'CSV' ? 'CSV' : 'XLSX'
})
function cleanBindingName(value: string) {
  return value.replaceAll('\\', '/').split('/').pop()?.trim().toLocaleLowerCase('ru-RU') || ''
}
function bindingStem(value: string) {
  return cleanBindingName(value)
    .replace(/\.[^.]+$/, '')
    .replace(/[^a-zA-Zа-яА-Я0-9]/g, '')
}
function resolveBindingOption(reference: string) {
  const explicit = bindingDraft.value[reference]
  if (explicit) return bindingOptions.value.find((item) => item.value === explicit) || null
  const clean = cleanBindingName(reference)
  const exact = bindingOptions.value.filter((item) => cleanBindingName(item.filename) === clean)
  if (exact.length === 1) return exact[0] || null
  const target = bindingStem(reference)
  const fuzzy = bindingOptions.value.filter((item) => {
    const candidate = bindingStem(item.filename)
    return (
      target &&
      candidate &&
      (target === candidate || target.includes(candidate) || candidate.includes(target))
    )
  })
  return fuzzy.length === 1 ? fuzzy[0] || null : null
}
const resolvedBindingGroups = computed(() => {
  const groups = new Map<
    string,
    { key: string; option: BindingOption | null; references: string[] }
  >()
  for (const reference of bindingReferences.value) {
    const option = resolveBindingOption(reference)
    const key = option?.value || `unresolved:${reference}`
    const existing = groups.get(key)
    if (existing) existing.references.push(reference)
    else groups.set(key, { key, option, references: [reference] })
  }
  return Array.from(groups.values())
})
const latestRun = computed(() => historyRuns.value[0] || null)
const auditStatusLabel = (status: string) =>
  ({ done: 'Завершена', failed: 'Ошибка', running: 'Выполняется', queued: 'В очереди' })[status] ||
  status
async function loadBindings() {
  const revision = selectedVersion.value
  const current = ++bindingEpoch
  bindingReferences.value = []
  bindingDraft.value = {}
  savedBindings.value = '{}'
  bindingsError.value = ''
  if (!isVor.value || !revision) return
  bindingsBusy.value = true
  try {
    const [data, catalog] = await Promise.all([
      backend.pdfBindings(revision.id),
      backend.catalog(projectId.value, false, document.value?.department_id || undefined),
    ])
    if (!active || current !== bindingEpoch) return
    catalogDocuments.value = catalog
    bindingReferences.value = data.references
    bindingDraft.value = { ...data.bindings }
    savedBindings.value = JSON.stringify(bindingDraft.value)
    bindingOptions.value = catalog
      .filter((d) => d.kind === 'pdf' && !d.archived)
      .flatMap((d) =>
        d.versions.map((v) => ({
          value: v.id,
          label: `${d.title || v.filename} · версия ${v.number} · ${v.filename}`,
          filename: v.filename,
          documentTitle: d.title || v.filename,
        })),
      )
  } catch (e) {
    if (active && current === bindingEpoch) bindingsError.value = readableError(e)
  } finally {
    if (current === bindingEpoch) bindingsBusy.value = false
  }
}

async function refreshCatalog() {
  catalogDocuments.value = await backend.catalog(
    projectId.value,
    false,
    document.value?.department_id || undefined,
  )
}

async function afterDocumentsSaved() {
  importOpen.value = false
  await loadDocument()
  await loadBindings()
  notice.value = 'Документы добавлены.'
}

async function confirmDocumentDelete() {
  const target = pendingDocumentDelete.value
  if (!target || documentDeleteBusy.value || locksMutations.value) return
  documentDeleteBusy.value = true
  try {
    await backend.deleteDocument(target.id)
    pendingDocumentDelete.value = null
    await loadBindings()
    notice.value = `Документ «${target.title}» удалён.`
  } catch (e) {
    error.value = readableError(e)
  } finally {
    documentDeleteBusy.value = false
  }
}
async function saveBindings() {
  const revision = selectedVersion.value
  if (!revision || bindingsBusy.value || locksMutations.value) return
  const current = bindingEpoch
  bindingsBusy.value = true
  bindingsError.value = ''
  try {
    const fields = Object.fromEntries(
      Object.entries(bindingDraft.value).filter(([, value]) => value),
    )
    const data = await backend.savePdfBindings(revision.id, fields)
    if (!active || current !== bindingEpoch) return
    bindingDraft.value = { ...data.bindings }
    savedBindings.value = JSON.stringify(bindingDraft.value)
    resetPreflight()
    notice.value = 'Привязки ПД сохранены для этой версии ВОР.'
  } catch (e) {
    if (active && current === bindingEpoch) bindingsError.value = readableError(e)
  } finally {
    if (current === bindingEpoch) bindingsBusy.value = false
  }
}
watch(
  () => selectedVersion.value?.id,
  () => {
    void loadBindings()
  },
)

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} Б`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} КБ`
  return `${(bytes / (1024 * 1024)).toFixed(1)} МБ`
}
function formatDate(value: string | null | undefined) {
  return value
    ? new Date(value).toLocaleString('ru-RU', {
        day: 'numeric',
        month: 'short',
        hour: '2-digit',
        minute: '2-digit',
      })
    : '—'
}

function currentRevisionId() {
  const wanted = requestedVersion.value || document.value?.current_version_id || ''
  return (
    sortedVersions.value.find((v) => v.id === wanted)?.id ||
    document.value?.current_version_id ||
    sortedVersions.value[0]?.id ||
    ''
  )
}

function resetPreflight() {
  preflightEpoch++
  preflightBusy.value = false
  preflight.value = null
  preflightError.value = ''
  acknowledged.value = false
  startedRunId.value = ''
}

async function loadDocument() {
  const currentEpoch = ++documentEpoch
  const ownerId = projectId.value
  const identity = documentId.value
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    await backend.connect()
    const [project, detail] = await Promise.all([
      backend.project(ownerId),
      backend.document(identity),
    ])
    if (!active || currentEpoch !== documentEpoch) return
    if (project.id !== detail.project_id) throw Error('Документ принадлежит другому проекту.')
    projectName.value = project.name
    projectArchived.value = project.archived
    document.value = detail
    departmentName.value = ''
    if (detail.department_id) {
      const department = await backend.department(ownerId, detail.department_id)
      if (!active || currentEpoch !== documentEpoch) return
      departmentName.value = department.name
    }
    titleDraft.value = detail.title
    resetPreflight()
    historyOffset.value = 0
    await loadHistory()
  } catch (e) {
    if (active && currentEpoch === documentEpoch) error.value = readableError(e)
  } finally {
    if (active && currentEpoch === documentEpoch) busy.value = false
  }
}

function changeHistoryPage(delta: number) {
  historyOffset.value = Math.max(0, historyOffset.value + delta)
  void loadHistory()
}

async function loadHistory() {
  const revision = selectedVersion.value
  const currentEpoch = ++historyEpoch
  historyRuns.value = []
  historyError.value = ''
  if (!revision || !isVor.value) return
  historyBusy.value = true
  try {
    const values = await backend.runs(
      projectId.value,
      historyOffset.value,
      document.value?.department_id || undefined,
    )
    if (!active || currentEpoch !== historyEpoch || selectedVersion.value?.id !== revision.id)
      return
    historyHasMore.value = values.length === historyLimit
    historyRuns.value = values.filter((run) => run.vor_version_id === revision.id)
  } catch (e) {
    if (active && currentEpoch === historyEpoch) historyError.value = readableError(e)
  } finally {
    if (active && currentEpoch === historyEpoch) historyBusy.value = false
  }
}

function selectVersion(id: string) {
  resetPreflight()
  historyOffset.value = 0
  router.replace({ query: { ...route.query, version: id } })
}

async function saveTitle() {
  if (!document.value || titleBusy.value) return
  const next = titleDraft.value.trim()
  if (!next) {
    error.value = 'Название документа не может быть пустым.'
    return
  }
  titleBusy.value = true
  try {
    document.value = await backend.updateDocument(document.value.id, { title: next })
    titleDraft.value = document.value.title
    editingTitle.value = false
    notice.value = 'Название сохранено.'
  } catch (e) {
    error.value = readableError(e)
  } finally {
    titleBusy.value = false
  }
}

async function setCurrent(id: string) {
  if (!document.value || locksMutations.value) return
  resetPreflight()
  try {
    document.value = await backend.updateDocument(document.value.id, { current_version_id: id })
    notice.value = isPdf.value
      ? 'Действующая редакция ПД обновлена. Следующая проверка использует её.'
      : 'Текущая редакция ВОР обновлена.'
  } catch (e) {
    error.value = readableError(e)
  }
}

async function onRevisionPicked(event: Event) {
  const input = event.target as HTMLInputElement | null
  const file = input?.files?.[0]
  if (input) input.value = ''
  if (!file || !document.value || uploadBusy.value || locksMutations.value) return
  uploadBusy.value = true
  error.value = ''
  try {
    const form = new FormData()
    form.append('project_id', projectId.value)
    form.append('kind', document.value.kind)
    form.append('document_id', document.value.id)
    form.append('file', file)
    const uploaded = await backend.upload(form)
    if (!active) return
    await loadDocument()
    if (uploaded.version_id) {
      await router.replace({ query: { ...route.query, version: uploaded.version_id } })
      await loadHistory()
    }
    resetPreflight()
    notice.value = 'Новая редакция загружена.'
  } catch (e) {
    error.value = readableError(e)
  } finally {
    uploadBusy.value = false
  }
}

function askDelete(version: RichVersion) {
  pendingDelete.value = version
  deleteOpen.value = true
}

async function confirmDelete() {
  const target = pendingDelete.value
  if (!target || deleteBusy.value || locksMutations.value) return
  deleteBusy.value = true
  try {
    const outcome = await backend.removeVersion(target.id)
    if (!outcome.deleted) throw Error('Ревизия не удалена.')
    deleteOpen.value = false
    pendingDelete.value = null
    notice.value = `Редакция ${target.number} удалена. Файлы-дубликаты по хешу сохранены.`
    await loadDocument()
  } catch (e) {
    const message = readableError(e)
    error.value = /используется|409|run|snapshot/i.test(message)
      ? `Редакция используется в запусках и не может быть удалена: ${message}`
      : message
  } finally {
    deleteBusy.value = false
  }
}

async function toggleArchive(next: boolean) {
  if (!document.value || archiveBusy.value || projectArchived.value) return
  archiveBusy.value = true
  try {
    document.value = await backend.updateDocument(document.value.id, { archived: next })
    archiveOpen.value = false
    notice.value = next ? 'Документ архивирован.' : 'Документ восстановлен из архива.'
  } catch (e) {
    error.value = readableError(e)
  } finally {
    archiveBusy.value = false
  }
}

async function checkAndStart() {
  const revision = selectedVersion.value
  if (
    !revision ||
    !isVor.value ||
    preflightBusy.value ||
    starting.value ||
    bindingsBusy.value ||
    bindingsDirty.value ||
    bindingsError.value
  )
    return
  // Повторный клик после подтверждения критических ошибок запускает проверку.
  if (preflight.value && (!preflight.value.has_critical_error || acknowledged.value)) {
    await startAudit()
    return
  }
  const current = ++preflightEpoch
  preflightBusy.value = true
  preflightError.value = ''
  try {
    if (current !== preflightEpoch) return
    const value = await backend.preflight({
      project_id: projectId.value,
      vor_version_id: revision.id,
    })
    if (current !== preflightEpoch) return
    preflight.value = value
    acknowledged.value = false
    if (!value.has_critical_error) await startAudit()
  } catch (e) {
    if (current !== preflightEpoch) return
    preflightError.value = readableError(e)
    preflight.value = null
  } finally {
    if (current === preflightEpoch) preflightBusy.value = false
  }
}

async function startAudit() {
  const revision = selectedVersion.value
  if (
    !revision ||
    !isVor.value ||
    starting.value ||
    locksMutations.value ||
    !preflight.value ||
    (preflight.value.has_critical_error && !acknowledged.value)
  )
    return
  if (startedRunId.value) {
    await navigateTo(`/audits/${startedRunId.value}`)
    return
  }
  starting.value = true
  preflightError.value = ''
  try {
    const created = await backend.start({
      project_id: projectId.value,
      vor_version_id: revision.id,
    })
    if (!active) return
    startedRunId.value = created.run_id
    await navigateTo(`/audits/${created.run_id}`)
  } catch (e) {
    preflightError.value = readableError(e)
  } finally {
    starting.value = false
  }
}

function downloadRevision(version: RichVersion) {
  const url = `${backend.contentUrl(version.id)}?download=true`
  backend.download(url, version.filename).catch((e: unknown) => (error.value = readableError(e)))
}

watch(requestedVersion, () => {
  resetPreflight()
  historyOffset.value = 0
  void loadHistory()
})

onMounted(() => {
  void loadDocument()
})
onBeforeUnmount(() => {
  active = false
  documentEpoch++
  historyEpoch++
  preflightEpoch++
})
</script>

<template>
  <div>
    <WorkspaceHeader />
    <main id="main-content" tabindex="-1" class="shell document-detail">
      <nav aria-label="Хлебные крошки" class="toolbar">
        <UButton variant="link" :to="departmentPath" aria-label="Назад к документам отдела">
          ← К проверкам отдела
        </UButton>
        <span v-if="departmentName" class="muted">{{ departmentName }}</span>
        <span v-if="projectArchived" class="muted">Проект архивирован: изменения недоступны</span>
      </nav>

      <div class="page-heading">
        <div class="title-block">
          <p class="muted">Документ · {{ kindLabel }} · {{ projectName }}</p>
          <h1 class="title-text" :title="document?.title || 'Документ'">
            {{ busy ? 'Загружаем…' : document?.title || 'Документ' }}
          </h1>
          <p v-if="document" class="muted title-meta">
            Редакций: {{ sortedVersions.length }}
            <span v-if="document.pd_updated">· ПД обновлён после последнего запуска</span>
            <span v-if="document.last_run">
              · Последний запуск {{ formatDate(document.last_run.created_at) }}
            </span>
          </p>
        </div>
        <div class="heading-actions">
          <UButton
            v-if="!editingTitle"
            variant="outline"
            :disabled="busy || !document || !!locksMutations"
            aria-label="Переименовать документ"
            @click="editingTitle = true"
          >
            Переименовать
          </UButton>
          <UButton
            v-if="document && !document.archived"
            variant="outline"
            :disabled="busy || projectArchived"
            aria-label="Архивировать документ"
            @click="archiveOpen = true"
          >
            Архивировать
          </UButton>
          <UButton
            v-if="document?.archived"
            color="primary"
            :disabled="busy || projectArchived"
            :loading="archiveBusy"
            aria-label="Восстановить документ из архива"
            @click="toggleArchive(false)"
          >
            Восстановить
          </UButton>
          <DocumentFormatCheck v-if="isVor && selectedVersion" :version="selectedVersion" />
        </div>
      </div>

      <UAlert
        v-if="error"
        color="error"
        variant="soft"
        title="Не удалось выполнить действие"
        :description="error"
        class="document-alert"
        role="alert"
      >
        <template #actions>
          <UButton color="error" variant="soft" size="sm" @click="loadDocument()"
            >Повторить</UButton
          >
        </template>
      </UAlert>
      <UAlert
        v-if="notice"
        color="success"
        variant="soft"
        :title="notice"
        class="document-alert"
        role="status"
      />

      <div v-if="editingTitle" class="panel title-editor">
        <UInput
          v-model="titleDraft"
          aria-label="Название документа"
          placeholder="Название документа"
          class="title-input"
        />
        <div class="toolbar">
          <UButton
            color="primary"
            :loading="titleBusy"
            :disabled="titleBusy"
            aria-label="Сохранить название документа"
            @click="saveTitle()"
          >
            Сохранить
          </UButton>
          <UButton
            variant="ghost"
            :disabled="titleBusy"
            aria-label="Отменить переименование"
            @click="editingTitle = false"
          >
            Отмена
          </UButton>
        </div>
      </div>

      <template v-if="document && selectedVersion">
        <section class="panel revision-panel revision-overview" aria-label="Текущая редакция">
          <div class="revision-copy">
            <div
              class="file-mark"
              :class="isVor ? 'file-mark-xlsx' : 'file-mark-pdf'"
              aria-hidden="true"
            >
              <span>{{ fileTypeLabel }}</span>
            </div>
            <div class="revision-identity">
              <span class="section-kicker">Текущая редакция</span>
              <strong class="source-name" :title="selectedVersion.filename">
                {{ selectedVersion.filename }}
              </strong>
              <span class="muted">
                Редакция {{ selectedVersion.number }} · {{ formatBytes(selectedVersion.size) }} ·
                {{ formatDate(selectedVersion.created_at) }}
              </span>
            </div>
          </div>
          <div class="revision-controls">
            <div class="revision-select">
              <label class="muted" for="revision-select">Выбрать редакцию</label>
              <USelect
                id="revision-select"
                :model-value="selectedVersion.id"
                :items="
                  sortedVersions.map((v) => ({
                    label: `${v.filename}, редакция ${v.number}${v.id === document?.current_version_id ? ' · действующая' : ''}`,
                    value: v.id,
                  }))
                "
                aria-label="Редакция документа"
                @update:model-value="selectVersion(String($event))"
              />
            </div>
            <div class="toolbar revision-actions">
              <input
                ref="revisionInput"
                type="file"
                :accept="isVor ? '.xlsx,.csv' : '.pdf'"
                class="revision-file"
                :aria-label="`Файл новой редакции ${kindLabel}`"
                @change="onRevisionPicked($event)"
              />
              <UButton
                variant="outline"
                :loading="uploadBusy"
                :disabled="uploadBusy || !!locksMutations"
                aria-label="Новая редакция"
                @click="revisionInput?.click()"
              >
                Новая редакция
              </UButton>
              <UButton
                variant="outline"
                :aria-label="`Скачать ${selectedVersion.filename}, редакция ${selectedVersion.number}`"
                @click="downloadRevision(selectedVersion)"
              >
                Скачать
              </UButton>
            </div>
          </div>
          <div v-if="selectedVersion.id !== document.current_version_id" class="revision-current">
            <UButton
              variant="outline"
              :disabled="!!locksMutations"
              :aria-label="`Сделать действующей редакцию ${selectedVersion.number}`"
              @click="setCurrent(selectedVersion.id)"
            >
              Сделать действующей
            </UButton>
          </div>
        </section>

        <section
          v-if="isVor"
          class="panel binding-panel"
          aria-label="Связи ВОР и проектной документации"
        >
          <div class="binding-heading">
            <div>
              <h2>Связи ВОР и проектной документации</h2>
              <p class="muted">
                Сначала система сопоставит ссылки из ведомости с PDF по имени файла.
              </p>
            </div>
            <div class="binding-heading-side">
              <UButton
                variant="outline"
                icon="i-lucide-files"
                :disabled="!!locksMutations"
                @click="importOpen = true"
              >
                Добавить PDF или XLSX
              </UButton>
            </div>
          </div>
          <div class="binding-canvas" aria-label="Привязки ВОР и ПД">
            <p v-if="bindingsBusy" role="status">Загружаем или сохраняем привязки…</p>
            <p v-if="bindingsError" class="error-box" role="alert">{{ bindingsError }}</p>
            <div
              v-if="!bindingsBusy && !bindingsError && bindingReferences.length"
              class="binding-auto-summary"
            >
              <div class="binding-summary-head">
                <div class="binding-auto-icon" aria-hidden="true">
                  <UIcon name="i-lucide-wand-sparkles" />
                </div>
                <div class="binding-auto-copy">
                  <strong>Связи определены по именам файлов</strong>
                  <span class="muted">
                    {{ resolvedBindingGroups.filter((group) => group.option).length }} из
                    {{ resolvedBindingGroups.length }} определено автоматически
                  </span>
                </div>
                <UButton
                  variant="outline"
                  :icon="bindingsManualOpen ? 'i-lucide-chevron-up' : 'i-lucide-settings-2'"
                  :aria-expanded="bindingsManualOpen"
                  @click="bindingsManualOpen = !bindingsManualOpen"
                >
                  {{ bindingsManualOpen ? 'Скрыть настройку' : 'Настроить вручную' }}
                </UButton>
              </div>
              <ul class="binding-map-list" aria-label="Автоматически определённые связи">
                <li v-for="group in resolvedBindingGroups" :key="group.key" class="binding-map-row">
                  <div class="binding-map-file binding-map-xlsx">
                    <span class="binding-file-tile">{{ fileTypeLabel }}</span>
                    <div>
                      <strong :title="selectedVersion.filename">{{
                        selectedVersion.filename
                      }}</strong>
                    </div>
                  </div>
                  <div class="binding-map-link" aria-hidden="true">
                    <UIcon :name="group.option ? 'i-lucide-arrow-right' : 'i-lucide-circle-help'" />
                    <span>{{ group.option ? 'Связан' : 'Не определён' }}</span>
                  </div>
                  <div
                    class="binding-map-file binding-map-pdf"
                    :class="{ 'binding-map-unresolved': !group.option }"
                  >
                    <span class="binding-file-tile">{{ group.option ? 'PDF' : '?' }}</span>
                    <div>
                      <strong :title="group.option?.filename || group.references.join(', ')">
                        {{ group.option?.filename || 'PDF не определён' }}
                      </strong>
                      <span v-if="!group.option" class="muted">Нужно выбрать вручную</span>
                    </div>
                  </div>
                </li>
              </ul>
            </div>
            <div v-if="bindingsManualOpen && bindingReferences.length" class="binding-manual">
              <div class="binding-manual-note">
                <strong>Ручные исключения</strong>
                <span class="muted"
                  >Меняйте только ссылки, которые система не смогла определить однозначно.</span
                >
              </div>
              <div
                v-for="(reference, index) in bindingReferences"
                :key="reference"
                class="binding-row"
              >
                <div class="binding-source">
                  <span class="binding-index">{{ index + 1 }}</span>
                  <div>
                    <span class="binding-label">Ссылка в XLSX</span>
                    <strong :title="reference">{{ reference }}</strong>
                  </div>
                </div>
                <div class="binding-arrow" aria-hidden="true">
                  <UIcon name="i-lucide-arrow-right" />
                </div>
                <div
                  class="binding-target"
                  :class="{ 'binding-target-linked': bindingDraft[reference] }"
                >
                  <div class="binding-target-head">
                    <span class="binding-label">Редакция PDF</span>
                    <UBadge
                      :color="bindingDraft[reference] ? 'success' : 'neutral'"
                      variant="soft"
                      :label="bindingDraft[reference] ? 'Выбрано вручную' : 'Автоподбор'"
                    />
                  </div>
                  <label :for="`binding-${reference}`" class="visually-hidden">
                    ПД для ссылки {{ reference }}
                  </label>
                  <select
                    :id="`binding-${reference}`"
                    v-model="bindingDraft[reference]"
                    :aria-label="`ПД для ссылки ${reference}`"
                    :disabled="bindingsBusy || !!locksMutations"
                    class="binding-select"
                  >
                    <option value="">Автоматически по имени файла</option>
                    <option v-for="item in bindingOptions" :key="item.value" :value="item.value">
                      {{ item.label }}
                    </option>
                    <option
                      v-if="
                        bindingDraft[reference] &&
                        !bindingOptions.some((item) => item.value === bindingDraft[reference])
                      "
                      :value="bindingDraft[reference]"
                    >
                      Выбранная версия недоступна — выберите другой PDF
                    </option>
                  </select>
                </div>
              </div>
            </div>
            <div
              v-if="!bindingsBusy && !bindingsError && !bindingReferences.length"
              class="binding-empty"
            >
              <UIcon name="i-lucide-file-check-2" />
              <div>
                <strong>Ссылки на PDF не найдены</strong>
                <p class="muted">Проверку можно запустить без ручных привязок.</p>
              </div>
            </div>
          </div>
          <div class="launch-bar">
            <div class="launch-copy">
              <strong>{{
                bindingsDirty ? 'Есть несохранённые изменения' : 'Можно запускать проверку'
              }}</strong>
              <span class="muted">
                {{
                  bindingsDirty
                    ? 'Сначала сохраните выбранные PDF.'
                    : 'Привязки фиксируются внутри нового запуска.'
                }}
              </span>
            </div>
            <div class="toolbar launch-actions">
              <UButton
                v-if="bindingsManualOpen && bindingReferences.length"
                variant="outline"
                :loading="bindingsBusy"
                :disabled="!bindingsDirty || !!locksMutations"
                @click="saveBindings"
              >
                Сохранить привязки ПД
              </UButton>
              <UButton
                data-testid="start-audit"
                color="primary"
                :loading="preflightBusy || starting"
                :disabled="
                  preflightBusy ||
                  starting ||
                  !!locksMutations ||
                  bindingsBusy ||
                  bindingsDirty ||
                  !!bindingsError
                "
                aria-label="Запустить проверку"
                @click="checkAndStart()"
              >
                Запустить проверку
              </UButton>
            </div>
          </div>
          <div class="preflight-block">
            <p v-if="preflightError" class="error-box" role="alert">{{ preflightError }}</p>
            <div v-if="preflight" class="preflight-result">
              <UBadge
                :color="preflight.has_critical_error ? 'error' : 'success'"
                variant="soft"
                :label="
                  preflight.has_critical_error
                    ? 'Есть критические ошибки'
                    : 'Критических ошибок нет'
                "
              />
              <div class="table-wrap">
                <UTable
                  sticky="header"
                  :data="preflight.rows"
                  :columns="preflightColumns"
                  class="results max-h-[60vh] overflow-auto"
                >
                  <template #vor-cell="{ row }">
                    <span class="cell-wrap">{{ row.original.vor }}</span>
                  </template>
                  <template #pdf-cell="{ row }">
                    <span class="cell-wrap">{{ row.original.pdf }}</span>
                  </template>
                  <template #refs-cell="{ row }">
                    <span class="cell-wrap">{{ row.original.refs }}</span>
                  </template>
                  <template #status-cell="{ row }">
                    {{ row.original.status }}
                  </template>
                </UTable>
              </div>
              <label v-if="preflight.has_critical_error" class="ack-row">
                <input
                  v-model="acknowledged"
                  type="checkbox"
                  aria-label="Подтверждаю запуск несмотря на критические ошибки"
                />
                <span>Запустить несмотря на критические ошибки, затем нажмите «Проверить»</span>
              </label>
            </div>
          </div>
        </section>

        <section
          class="panel disclosure-panel"
          :aria-label="isVor ? 'История редакций ВОР' : 'История редакций ПД'"
        >
          <button
            type="button"
            class="disclosure-trigger"
            :aria-label="isVor ? 'История редакций ВОР' : 'История редакций ПД'"
            :aria-expanded="revisionsOpen"
            @click="revisionsOpen = !revisionsOpen"
          >
            <span class="disclosure-title">
              <UIcon name="i-lucide-layers-3" />
              <span>
                <strong>{{ isVor ? 'История редакций ВОР' : 'История редакций ПД' }}</strong>
                <small
                  >{{ sortedVersions.length }} редакций · текущая №
                  {{ selectedVersion.number }}</small
                >
              </span>
            </span>
            <UIcon :name="revisionsOpen ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'" />
          </button>
          <ul
            v-if="revisionsOpen"
            class="revision-list disclosure-content"
            :aria-label="isVor ? 'Все редакции ведомости' : 'Все редакции ПД'"
          >
            <li
              v-for="version in sortedVersions"
              :key="version.id"
              class="revision-row"
              :class="{ 'revision-row-current': version.id === document.current_version_id }"
            >
              <div class="revision-number" aria-hidden="true">
                <span>№</span>
                <strong>{{ version.number }}</strong>
              </div>
              <div class="revision-history-copy">
                <strong class="revision-name" :title="version.filename">
                  {{ version.filename }}, редакция {{ version.number }}
                </strong>
                <span class="muted">
                  {{ formatDate(version.created_at) }} · {{ formatBytes(version.size) }}
                </span>
              </div>
              <div class="revision-state">
                <UBadge
                  v-if="version.id === document.current_version_id"
                  color="success"
                  variant="soft"
                  label="Действующая"
                />
                <UBadge
                  v-if="version.id === selectedVersion.id"
                  color="neutral"
                  variant="soft"
                  label="Открыта"
                />
              </div>
              <div class="revision-history-actions">
                <UButton
                  v-if="version.id !== selectedVersion.id"
                  variant="ghost"
                  color="neutral"
                  icon="i-lucide-arrow-right"
                  :aria-label="`Выбрать редакцию ${version.number}`"
                  @click="selectVersion(version.id)"
                >
                  Открыть
                </UButton>
                <UButton
                  variant="ghost"
                  color="neutral"
                  icon="i-lucide-download"
                  :aria-label="`Скачать ${version.filename}, редакция ${version.number}`"
                  @click="downloadRevision(version)"
                >
                  Скачать
                </UButton>
                <UButton
                  variant="ghost"
                  color="error"
                  icon="i-lucide-trash-2"
                  :disabled="!!locksMutations"
                  :aria-label="`Удалить редакцию ${version.number}`"
                  @click="askDelete(version)"
                >
                  <span class="visually-hidden">Удалить</span>
                </UButton>
              </div>
            </li>
          </ul>
        </section>

        <section v-if="isVor" class="panel runs-panel" aria-label="Проверки выбранной редакции">
          <div class="runs-heading">
            <div>
              <span class="section-kicker">Редакция {{ selectedVersion.number }}</span>
              <h2>Проверки</h2>
            </div>
            <UBadge variant="soft" color="neutral" :label="`${historyRuns.length} запусков`" />
          </div>
          <p v-if="historyBusy" class="empty" role="status">Загружаем запуски…</p>
          <p v-else-if="historyError" class="error-box" role="alert">
            {{ historyError }}
            <UButton variant="link" aria-label="Повторить загрузку запусков" @click="loadHistory()">
              Повторить
            </UButton>
          </p>
          <p v-else-if="!historyRuns.length" class="empty" role="status">
            Запусков с этим документом пока нет.
          </p>
          <template v-else>
            <NuxtLink :to="`/audits/${latestRun?.run_id}`" class="latest-run">
              <div class="latest-run-icon"><UIcon name="i-lucide-file-check-2" /></div>
              <div class="latest-run-copy">
                <span class="binding-label">Последняя проверка</span>
                <strong>{{ formatDate(latestRun?.created_at) }}</strong>
                <span class="muted">{{ latestRun?.vor_filename }}</span>
              </div>
              <UBadge
                :color="
                  latestRun?.status === 'done'
                    ? 'success'
                    : latestRun?.status === 'failed'
                      ? 'error'
                      : 'warning'
                "
                variant="soft"
                :label="auditStatusLabel(latestRun?.status || '')"
              />
              <span class="latest-run-count">
                {{ (latestRun?.done || 0) + (latestRun?.failed || 0) }} /
                {{ latestRun?.total ?? '…' }}
              </span>
              <UIcon name="i-lucide-arrow-up-right" class="latest-run-arrow" />
            </NuxtLink>
            <button
              v-if="historyRuns.length > 1"
              type="button"
              class="runs-trigger"
              :aria-expanded="runsOpen"
              @click="runsOpen = !runsOpen"
            >
              <span>{{
                runsOpen
                  ? 'Скрыть остальные проверки'
                  : `Показать остальные · ${historyRuns.length - 1}`
              }}</span>
              <UIcon :name="runsOpen ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'" />
            </button>
            <div v-if="runsOpen && historyRuns.length > 1" class="table-wrap runs-table">
              <UTable
                sticky="header"
                :data="historyRuns.slice(1)"
                :columns="historyColumns"
                class="results max-h-[60vh] overflow-auto"
              >
                <template #run-cell="{ row }">
                  <span class="cell-wrap" :title="row.original.vor_filename">
                    {{ row.original.vor_filename }} · {{ formatDate(row.original.created_at) }}
                  </span>
                </template>
                <template #status-cell="{ row }">
                  <UBadge
                    :color="
                      row.original.status === 'done'
                        ? 'success'
                        : row.original.status === 'failed'
                          ? 'error'
                          : 'warning'
                    "
                    variant="soft"
                    :label="row.original.status"
                  />
                </template>
                <template #count-cell="{ row }">
                  <span class="numbers"
                    >{{ row.original.done + row.original.failed }} /
                    {{ row.original.total ?? '…' }}</span
                  >
                </template>
                <template #action-cell="{ row }">
                  <UButton
                    variant="link"
                    :to="`/audits/${row.original.run_id}`"
                    :aria-label="`Открыть запуск ${row.original.vor_filename}`"
                  >
                    Открыть
                  </UButton>
                </template>
              </UTable>
            </div>
          </template>
          <div v-if="historyOffset || historyHasMore" class="toolbar pagination">
            <UButton
              :disabled="historyOffset === 0"
              aria-label="Предыдущие запуски"
              @click="changeHistoryPage(-historyLimit)"
            >
              Назад
            </UButton>
            <UButton
              :disabled="historyBusy || !historyHasMore"
              aria-label="Следующие запуски"
              @click="changeHistoryPage(historyLimit)"
            >
              Далее
            </UButton>
          </div>
        </section>

        <section v-else class="panel" aria-label="Проверки документа ПД">
          <h2>Проверки</h2>
          <p v-if="document.last_run" class="muted">
            Последний запуск проекта:
            <UButton
              variant="link"
              :to="`/audits/${document.last_run.run_id}`"
              aria-label="Открыть последний запуск проекта"
            >
              Открыть
            </UButton>
          </p>
          <p v-else class="empty" role="status">Запусков с этим документом пока нет.</p>
        </section>
      </template>

      <div v-else-if="!busy && !error" class="panel empty" role="status">
        Документ пуст: загрузите первую редакцию кнопкой «Новая редакция» из проекта.
      </div>
    </main>

    <UModal
      v-model:open="importOpen"
      title="Добавить документы"
      description="Загрузка останется на текущей странице"
      :ui="{ content: 'max-w-3xl' }"
    >
      <template #body>
        <DocumentImport
          v-if="document"
          :project-id="projectId"
          :department-id="document.department_id || undefined"
          :project-name="projectName"
          :documents="catalogDocuments"
          @changed="refreshCatalog"
          @saved="afterDocumentsSaved"
        />

        <section class="managed-documents" aria-labelledby="managed-documents-title">
          <div class="managed-documents-heading">
            <div>
              <h3 id="managed-documents-title">Документы отдела</h3>
              <p class="muted">Можно удалить ошибочно добавленный PDF или XLSX.</p>
            </div>
            <span class="muted">{{ manageableDocuments.length }}</span>
          </div>
          <ul v-if="manageableDocuments.length" class="managed-document-list">
            <li v-for="item in manageableDocuments" :key="item.id" class="managed-document-row">
              <div class="managed-document-icon" aria-hidden="true">
                <UIcon
                  :name="item.kind === 'pdf' ? 'i-lucide-file-text' : 'i-lucide-file-spreadsheet'"
                />
              </div>
              <div class="managed-document-copy">
                <strong :title="item.title">{{ item.title }}</strong>
                <span class="muted">
                  {{ item.kind === 'pdf' ? 'ПД' : 'ВОР' }} · редакций {{ item.versions.length }}
                </span>
              </div>
              <UButton
                color="error"
                variant="ghost"
                icon="i-lucide-trash-2"
                :disabled="!!locksMutations"
                :aria-label="`Удалить документ ${item.title}`"
                @click="pendingDocumentDelete = item"
              />
            </li>
          </ul>
          <p v-else class="empty">Других документов в отделе пока нет.</p>
        </section>
      </template>
    </UModal>

    <UModal
      :open="pendingDocumentDelete !== null"
      title="Удалить документ?"
      description="Документ и все его редакции будут удалены без восстановления"
      @update:open="(open: boolean) => !open && (pendingDocumentDelete = null)"
    >
      <template #body>
        <p v-if="pendingDocumentDelete" class="delete-name">
          {{ pendingDocumentDelete.title }}
        </p>
      </template>
      <template #footer>
        <div class="toolbar">
          <UButton
            color="error"
            :loading="documentDeleteBusy"
            :disabled="documentDeleteBusy"
            @click="confirmDocumentDelete"
          >
            Удалить документ
          </UButton>
          <UButton
            variant="ghost"
            :disabled="documentDeleteBusy"
            @click="pendingDocumentDelete = null"
          >
            Отмена
          </UButton>
        </div>
      </template>
    </UModal>

    <UModal
      v-model:open="deleteOpen"
      title="Удалить редакцию?"
      description="Удаление возможно только для неиспользуемых редакций"
    >
      <template #body>
        <p v-if="pendingDelete" class="delete-name" :title="pendingDelete.filename">
          {{ pendingDelete.filename }}, редакция {{ pendingDelete.number }}
        </p>
        <p class="muted">Редакции, использованные в запусках, удалить нельзя.</p>
      </template>
      <template #footer>
        <div class="toolbar">
          <UButton
            color="error"
            :loading="deleteBusy"
            :disabled="deleteBusy"
            aria-label="Подтвердить удаление редакции"
            @click="confirmDelete()"
          >
            Удалить
          </UButton>
          <UButton
            variant="ghost"
            :disabled="deleteBusy"
            aria-label="Отменить удаление редакции"
            @click="deleteOpen = false"
          >
            Отмена
          </UButton>
        </div>
      </template>
    </UModal>

    <UModal
      v-model:open="archiveOpen"
      title="Архивировать документ?"
      description="Архив скрывает документ из активного каталога"
    >
      <template #body>
        <p class="delete-name" :title="document?.title || ''">{{ document?.title }}</p>
      </template>
      <template #footer>
        <div class="toolbar">
          <UButton
            color="error"
            :loading="archiveBusy"
            :disabled="archiveBusy"
            aria-label="Подтвердить архивирование документа"
            @click="toggleArchive(true)"
          >
            Архивировать
          </UButton>
          <UButton
            variant="ghost"
            :disabled="archiveBusy"
            aria-label="Отменить архивирование документа"
            @click="archiveOpen = false"
          >
            Отмена
          </UButton>
        </div>
      </template>
    </UModal>
  </div>
</template>

<style scoped>
.document-detail {
  display: flex;
  flex-direction: column;
  gap: 20px;
  padding-top: 28px;
  padding-bottom: 64px;
}
.document-detail > .panel {
  padding: 28px;
}
.document-detail h2 {
  margin: 2px 0 6px;
  font-size: 20px;
  font-weight: 700;
  letter-spacing: -0.02em;
}
.title-block,
.revision-identity,
.revision-controls {
  min-width: 0;
}
.title-text,
.title-meta,
.source-name,
.revision-name,
.delete-name {
  overflow-wrap: anywhere;
}
.heading-actions,
.revision-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.document-alert {
  margin: 0;
}
.title-editor {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.title-input,
.binding-select {
  width: 100%;
}
.revision-panel {
  display: grid;
  grid-template-columns: minmax(260px, 1fr) minmax(360px, 1.2fr) auto;
  align-items: center;
  gap: 24px;
}
.revision-overview {
  border-color: var(--ui-border-muted);
  background: var(--ui-bg-elevated);
}
.revision-copy,
.revision-identity,
.revision-controls {
  display: flex;
}
.revision-copy {
  align-items: center;
  gap: 16px;
}
.revision-identity,
.revision-controls {
  flex-direction: column;
  gap: 4px;
}
.revision-select {
  display: flex;
  flex: 1;
  min-width: 0;
  flex-direction: column;
  gap: 4px;
}
.revision-current {
  display: flex;
  justify-content: flex-end;
}
.revision-file {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
}
.file-mark {
  display: flex;
  width: 54px;
  height: 54px;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  border: 1px solid #b9d8c4;
  border-radius: 9px;
  background: #eef8f1;
  color: #287a49;
}
.file-mark span {
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.03em;
}
.file-mark-pdf {
  border-color: #efc4c0;
  background: #fff1f0;
  color: #b7352d;
}
.section-kicker,
.binding-label {
  color: var(--ui-text-muted);
  font-size: 13px;
  font-weight: 600;
}
.binding-heading,
.runs-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
}
.binding-heading-side {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 20px;
}
.binding-canvas {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 22px;
}
.binding-auto-summary {
  display: flex;
  overflow: hidden;
  flex-direction: column;
  border: 1px solid var(--ui-border);
  border-radius: 10px;
  background: var(--ui-bg);
}
.binding-summary-head {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 14px;
  padding: 14px 16px;
  background: var(--ui-bg-muted);
}
.binding-auto-icon {
  display: grid;
  width: 42px;
  height: 42px;
  place-items: center;
  border-radius: 9px;
  background: color-mix(in srgb, var(--ui-primary) 10%, var(--ui-bg));
  color: var(--ui-primary);
}
.binding-auto-icon svg {
  width: 21px;
  height: 21px;
}
.binding-auto-copy,
.binding-manual,
.binding-manual-note {
  display: flex;
  flex-direction: column;
}
.binding-auto-copy {
  min-width: 0;
  gap: 3px;
}
.binding-map-list {
  display: flex;
  flex-direction: column;
  margin: 0;
  padding: 0;
  border-top: 1px solid var(--ui-border);
  list-style: none;
}
.binding-map-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 92px minmax(0, 1fr);
  align-items: center;
  gap: 12px;
  padding: 16px;
  background: var(--ui-bg);
}
.binding-map-row + .binding-map-row {
  border-top: 1px solid var(--ui-border-muted);
}
.binding-map-file {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 12px;
}
.binding-map-file > div {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 2px;
}
.binding-map-file strong,
.binding-map-file .muted {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.binding-map-file .muted {
  font-size: 12px;
}
.binding-file-tile {
  display: grid;
  width: 44px;
  height: 44px;
  flex: 0 0 auto;
  place-items: center;
  border: 1px solid #efc4c0;
  border-radius: 8px;
  background: #fff1f0;
  color: #b7352d;
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.03em;
}
.binding-map-xlsx .binding-file-tile {
  border-color: #b9d8c4;
  background: #eef8f1;
  color: #287a49;
}
.binding-map-unresolved .binding-file-tile {
  border-color: var(--ui-border);
  background: var(--ui-bg);
  color: var(--ui-text-muted);
}
.binding-map-link {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  color: var(--ui-primary);
}
.binding-map-link span {
  font-size: 11px;
  font-weight: 650;
}
.binding-map-link svg {
  width: 24px;
  height: 24px;
  stroke-width: 2.5;
}
.binding-manual {
  gap: 12px;
  padding-top: 4px;
}
.binding-manual-note {
  gap: 2px;
  padding: 2px 2px 4px;
}
.binding-manual-note .muted {
  font-size: 13px;
}
.binding-row {
  display: grid;
  grid-template-columns: minmax(190px, 0.8fr) 64px minmax(320px, 1.5fr);
  align-items: stretch;
}
.binding-source,
.binding-target {
  min-width: 0;
  padding: 16px;
  border: 1px solid var(--ui-border);
  border-radius: 10px;
  background: var(--ui-bg);
}
.binding-source {
  display: flex;
  align-items: center;
  gap: 12px;
}
.binding-source > div,
.binding-target {
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.binding-source strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.binding-index {
  display: grid;
  width: 28px;
  height: 28px;
  flex: 0 0 auto;
  place-items: center;
  border-radius: 50%;
  background: var(--ui-bg-accented);
  font-size: 13px;
  font-weight: 700;
}
.binding-target {
  transition: border-color 160ms var(--app-ease);
}
.binding-target-linked {
  border-color: color-mix(in srgb, var(--ui-success) 55%, var(--ui-border));
}
.binding-target-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.binding-select {
  min-height: 44px;
  padding: 10px 36px 10px 12px;
  border: 1px solid var(--ui-border);
  border-radius: 6px;
  background: var(--ui-bg);
  color: var(--ui-text);
}
.binding-arrow {
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--ui-text-muted);
}
.binding-arrow svg {
  width: 20px;
  height: 20px;
}
.binding-empty,
.runs-empty {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 24px;
  border: 1px dashed var(--ui-border-accented);
  border-radius: 10px;
  background: var(--ui-bg);
}
.binding-empty > svg,
.runs-empty > svg {
  width: 28px;
  height: 28px;
  color: var(--ui-primary);
}
.binding-empty p,
.runs-empty p {
  margin: 2px 0 0;
}
.launch-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  margin-top: 20px;
  padding-top: 20px;
  border-top: 1px solid var(--ui-border-muted);
}
.launch-copy {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.preflight-block {
  margin-top: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.preflight-result {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.revision-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 12px 0;
  padding: 0;
  list-style: none;
}
.revision-row {
  padding: 12px 16px;
  border: 1px solid var(--ui-border);
  border-radius: 8px;
}
.revision-row-current {
  border-color: var(--ui-primary);
}
.revision-main {
  display: flex;
  min-width: 0;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.disclosure-panel {
  padding: 0 !important;
  overflow: hidden;
}
.disclosure-trigger,
.runs-trigger {
  width: 100%;
  border: 0;
  background: transparent;
  color: var(--ui-text);
  text-align: left;
}
.disclosure-trigger {
  display: flex;
  min-height: 82px;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 24px;
}
.disclosure-trigger:hover,
.runs-trigger:hover {
  background: var(--ui-bg-muted);
}
.disclosure-title {
  display: flex;
  align-items: center;
  gap: 14px;
}
.disclosure-title > svg {
  width: 22px;
  height: 22px;
  color: var(--ui-primary);
}
.disclosure-title span {
  display: flex;
  flex-direction: column;
}
.disclosure-title small {
  color: var(--ui-text-muted);
  font-size: 13px;
}
.disclosure-content {
  margin: 0;
  padding: 16px 24px 24px;
  border-top: 1px solid var(--ui-border-muted);
}
.disclosure-content .revision-row {
  display: grid;
  grid-template-columns: 48px minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 16px;
  padding: 14px 16px;
  border-color: var(--ui-border-muted);
  background: var(--ui-bg);
}
.disclosure-content .revision-row-current {
  border-color: color-mix(in srgb, var(--ui-primary) 48%, var(--ui-border));
  background: color-mix(in srgb, var(--app-selected-bg) 55%, var(--ui-bg));
}
.revision-number {
  display: flex;
  width: 44px;
  height: 44px;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  background: var(--ui-bg-muted);
  color: var(--ui-text-muted);
  line-height: 1;
}
.revision-number span {
  font-size: 10px;
  font-weight: 600;
}
.revision-number strong {
  margin-top: 3px;
  color: var(--ui-text-highlighted);
  font-size: 17px;
}
.revision-history-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
}
.revision-history-copy .revision-name {
  overflow: hidden;
  color: var(--ui-text-highlighted);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.revision-state,
.revision-history-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.revision-history-actions {
  justify-content: flex-end;
}
.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}
.runs-panel {
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.latest-run {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto auto auto;
  align-items: center;
  gap: 16px;
  padding: 18px;
  border: 1px solid var(--ui-border);
  border-radius: 10px;
  color: inherit;
  text-decoration: none;
  transition:
    transform 160ms var(--app-ease),
    border-color 160ms var(--app-ease);
}
.latest-run:hover {
  transform: translateY(-2px);
  border-color: var(--ui-border-accented);
}
.latest-run-icon {
  display: grid;
  width: 42px;
  height: 42px;
  place-items: center;
  border-radius: 9px;
  background: var(--app-selected-bg);
  color: var(--app-selected-text);
}
.latest-run-icon svg {
  width: 22px;
  height: 22px;
}
.latest-run-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
}
.latest-run-copy .muted {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.latest-run-count {
  font-variant-numeric: tabular-nums;
  font-weight: 700;
}
.latest-run-arrow {
  color: var(--ui-text-muted);
}
.runs-trigger {
  display: flex;
  min-height: 44px;
  align-items: center;
  justify-content: center;
  gap: 8px;
  border-radius: 8px;
  font-weight: 600;
}
.runs-table {
  padding-top: 8px;
  border-top: 1px solid var(--ui-border-muted);
}
.table-wrap {
  overflow-x: auto;
}
.cell-wrap {
  min-width: 140px;
  overflow-wrap: anywhere;
}
.ack-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.pagination {
  margin-top: 12px;
}
.managed-documents {
  margin-top: 24px;
  padding-top: 20px;
  border-top: 1px solid var(--ui-border-muted);
}
.managed-documents-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.managed-documents-heading h3 {
  margin: 0;
  font-size: 16px;
}
.managed-documents-heading p {
  margin: 3px 0 0;
  font-size: 13px;
}
.managed-document-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 14px 0 0;
  padding: 0;
  list-style: none;
}
.managed-document-row {
  display: grid;
  grid-template-columns: 36px minmax(0, 1fr) auto;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border: 1px solid var(--ui-border);
  border-radius: 9px;
}
.managed-document-icon {
  display: grid;
  width: 36px;
  height: 36px;
  place-items: center;
  border-radius: 7px;
  background: var(--ui-bg-muted);
  color: var(--ui-primary);
}
.managed-document-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 2px;
}
.managed-document-copy strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.managed-document-copy span {
  font-size: 13px;
}
@media (max-width: 640px) {
  .document-detail > .panel {
    padding: 16px;
  }
  .revision-panel,
  .binding-row,
  .latest-run {
    grid-template-columns: 1fr;
  }
  .revision-current {
    justify-content: flex-start;
  }
  .binding-arrow {
    min-height: 28px;
    transform: rotate(90deg);
  }
  .binding-heading,
  .launch-bar,
  .binding-heading-side {
    align-items: stretch;
    flex-direction: column;
  }
  .binding-summary-head {
    grid-template-columns: auto minmax(0, 1fr);
  }
  .binding-summary-head > :last-child {
    grid-column: 1 / -1;
  }
  .binding-map-row {
    grid-template-columns: 1fr;
  }
  .binding-map-link {
    min-height: 42px;
  }
  .launch-actions > * {
    flex: 1 1 100%;
  }
  .latest-run > :not(.latest-run-copy, .latest-run-icon) {
    justify-self: start;
  }
  .disclosure-content {
    padding-inline: 12px;
  }
  .disclosure-content .revision-row {
    grid-template-columns: 44px minmax(0, 1fr);
    gap: 12px;
  }
  .revision-state,
  .revision-history-actions {
    grid-column: 1 / -1;
    justify-content: flex-start;
  }
}
@media (min-width: 641px) and (max-width: 960px) {
  .revision-panel {
    grid-template-columns: 1fr 1fr;
  }
  .revision-current {
    grid-column: 1 / -1;
    justify-content: flex-start;
  }
  .binding-row {
    grid-template-columns: minmax(160px, 0.8fr) 44px minmax(260px, 1.2fr);
  }
}
</style>

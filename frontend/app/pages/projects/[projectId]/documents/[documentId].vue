<script setup lang="ts">
import type { PreflightResponse, PreflightRow, RunDto } from '~/generated/types.gen'
import type { TableColumn } from '@nuxt/ui'
const backend = useBackend()
const route = useRoute()
const router = useRouter()
definePageMeta({
  key: (route) => `${route.params.projectId}:${route.params.documentId}`,
})
let active = true
let documentEpoch = 0
let historyEpoch = 0

type Backend = typeof backend
type DocumentDetail = Awaited<ReturnType<Backend['document']>>
type RichVersion = DocumentDetail['versions'][number]

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

const preflight = ref<PreflightResponse | null>(null)
const preflightBusy = ref(false)
const preflightError = ref('')
const acknowledged = ref(false)
const starting = ref(false)
const startedRunId = ref('')
let preflightEpoch = 0

const metadataBusy = ref(false)
const metadataDraft = ref<Record<string, string>>({})
const editingMetadata = ref(false)
function openMetadataEditor() {
  const revision = selectedVersion.value
  if (revision)
    metadataDraft.value = Object.fromEntries(
      metadataEntries(revision).map((row) => [row.key, String(row.value)]),
    )
  editingMetadata.value = true
}

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
const locksMutations = computed(() => projectArchived.value || document.value?.archived)
const kindLabel = computed(() => (document.value?.kind === 'pdf' ? 'ПД' : 'ВОР'))
const sortedVersions = computed<RichVersion[]>(() => document.value?.versions ?? [])
const isVor = computed(() => document.value?.kind === 'vor')
const isPdf = computed(() => document.value?.kind === 'pdf')
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
const bindingOptions = ref<{ label: string; value: string }[]>([])
const bindingsBusy = ref(false)
const bindingsError = ref('')
let bindingEpoch = 0
const bindingsDirty = computed(() => JSON.stringify(bindingDraft.value) !== savedBindings.value)
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
      backend.catalog(projectId.value),
    ])
    if (!active || current !== bindingEpoch) return
    bindingReferences.value = data.references
    bindingDraft.value = { ...data.bindings }
    savedBindings.value = JSON.stringify(bindingDraft.value)
    bindingOptions.value = catalog
      .filter((d) => d.kind === 'pdf' && !d.archived)
      .flatMap((d) =>
        d.versions.map((v) => ({
          value: v.id,
          label: `${d.title || v.filename} · версия ${v.number} · ${v.filename}`,
        })),
      )
  } catch (e) {
    if (active && current === bindingEpoch) bindingsError.value = readableError(e)
  } finally {
    if (current === bindingEpoch) bindingsBusy.value = false
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

function metadataEntries(version: RichVersion) {
  const meta = version.metadata
  const overrides = version.metadata_overrides || {}
  return [
    {
      key: 'construction_title',
      label: 'Объект строительства',
      extracted: meta.construction_title,
    },
    { key: 'raw_title', label: 'Название из файла', extracted: meta.raw_title },
    { key: 'document_code', label: 'Шифр документа', extracted: meta.document_code },
    { key: 'pd_reference', label: 'Ссылка на ПД', extracted: meta.pd_reference },
  ].map((row) => ({ ...row, value: overrides[row.key] ?? row.extracted ?? '' }))
}

function metadataProvenance(version: RichVersion, key: string) {
  const source = version.metadata.sources?.[key]
  if (!source) return 'Источник не зафиксирован'
  const parts = [source.sheet, source.cell].filter(Boolean).join(' ')
  return parts ? `Источник: ${parts}` : 'Источник не зафиксирован'
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
    titleDraft.value = detail.title
    resetPreflight()
    editingMetadata.value = false
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
    const values = await backend.runs(projectId.value, historyOffset.value)
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
  editingMetadata.value = false
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

async function saveMetadata() {
  const revision = selectedVersion.value
  if (!revision || metadataBusy.value || locksMutations.value) return
  metadataBusy.value = true
  error.value = ''
  try {
    const allowed = ['construction_title', 'raw_title', 'document_code', 'pd_reference']
    const fields: Record<string, string> = {}
    for (const key of allowed) {
      fields[key] = (metadataDraft.value[key] ?? '').trim()
    }
    await backend.updateMetadata(revision.id, fields)
    editingMetadata.value = false
    await loadDocument()
    notice.value = 'Метаданные сохранены. Извлечённые значения не изменены.'
  } catch (e) {
    error.value = readableError(e)
  } finally {
    metadataBusy.value = false
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
  editingMetadata.value = false
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
        <UButton
          variant="link"
          :to="`/projects/${projectId}`"
          aria-label="Назад к документам проекта"
        >
          ← К проекту
        </UButton>
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
        <section class="panel revision-panel" aria-label="Выбор редакции">
          <div class="toolbar spread revision-bar">
            <div class="revision-select">
              <label class="muted" for="revision-select">Редакция документа</label>
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
            <div class="toolbar">
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
              <DocumentFormatCheck v-if="isVor" :version="selectedVersion" />
            </div>
          </div>
          <p class="muted source-name" :title="selectedVersion.filename">
            Исходный файл: {{ selectedVersion.filename }}, редакция {{ selectedVersion.number }} ·
            {{ formatBytes(selectedVersion.size) }} ·
            {{ formatDate(selectedVersion.created_at) }}
            <span v-if="selectedVersion.id === document.current_version_id">· действующая</span>
          </p>
          <div class="toolbar">
            <UButton
              v-if="selectedVersion.id !== document.current_version_id"
              variant="outline"
              :disabled="!!locksMutations"
              :aria-label="`Сделать действующей редакцию ${selectedVersion.number}`"
              @click="setCurrent(selectedVersion.id)"
            >
              Сделать действующей
            </UButton>
            <span v-else class="muted" role="status">Действующая редакция</span>
          </div>
        </section>

        <section class="panel" aria-label="Реквизиты редакции">
          <div class="toolbar spread">
            <div>
              <h2>Реквизиты</h2>
              <p class="muted">Извлечённые значения сохраняются; правки лежат поверх извлечения.</p>
            </div>
            <UButton
              v-if="!editingMetadata"
              variant="outline"
              :disabled="!!locksMutations"
              aria-label="Уточнить реквизиты"
              @click="openMetadataEditor()"
            >
              Уточнить реквизиты
            </UButton>
          </div>
          <dl v-if="!editingMetadata" class="metadata-read">
            <div
              v-for="row in metadataEntries(selectedVersion)"
              :key="row.key"
              class="metadata-line"
            >
              <dt>{{ row.label }}</dt>
              <dd class="metadata-value" :title="String(row.value || '')">
                {{ row.value || '—' }}
              </dd>
              <dd
                class="muted metadata-source"
                :title="metadataProvenance(selectedVersion, row.key)"
              >
                {{ metadataProvenance(selectedVersion, row.key) }}
              </dd>
            </div>
          </dl>
          <template v-else>
            <div
              v-for="row in metadataEntries(selectedVersion)"
              :key="row.key"
              class="metadata-row"
            >
              <div class="metadata-head">
                <label :for="`metadata-${row.key}`">{{ row.label }}</label>
                <span
                  class="muted metadata-source"
                  :title="metadataProvenance(selectedVersion, row.key)"
                >
                  {{ metadataProvenance(selectedVersion, row.key) }}
                </span>
              </div>
              <UInput
                :id="`metadata-${row.key}`"
                v-model="metadataDraft[row.key]"
                :placeholder="row.extracted || '—'"
                :aria-label="`${row.label}, извлечено: ${row.extracted || 'пусто'}`"
                class="metadata-input"
              />
              <p class="muted metadata-raw" :title="String(row.extracted || '')">
                Извлечено: {{ row.extracted || '—' }}
              </p>
            </div>
            <div class="toolbar">
              <UButton
                color="primary"
                :loading="metadataBusy"
                :disabled="metadataBusy || !!locksMutations"
                aria-label="Сохранить метаданные редакции"
                @click="saveMetadata()"
              >
                Сохранить метаданные
              </UButton>
              <UButton
                variant="ghost"
                :disabled="metadataBusy"
                aria-label="Отменить уточнение реквизитов"
                @click="editingMetadata = false"
              >
                Отмена
              </UButton>
            </div>
          </template>
        </section>

        <section v-if="isVor" class="panel" aria-label="Проверка и запуск">
          <h2>Проверка и запуск</h2>
          <p class="muted">
            Укажите, какой загруженный PDF соответствует ссылке из ВОР. Явная привязка имеет
            приоритет над именем файла: можно выбрать копию или нужную редакцию ПД. Обозначения и
            содержимое документа продолжат проверяться.
          </p>
          <div class="preflight-block" aria-label="Привязки ВОР и ПД">
            <p v-if="bindingsBusy" role="status">Загружаем или сохраняем привязки…</p>
            <p v-if="bindingsError" class="error-box" role="alert">{{ bindingsError }}</p>
            <div v-for="reference in bindingReferences" :key="reference" class="preflight-block">
              <label :for="`binding-${reference}`">Ссылка из ВОР: {{ reference }}</label>
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
            <p v-if="!bindingsBusy && !bindingsError && !bindingReferences.length" class="muted">
              В этой версии ВОР нет ссылок на файлы ПД.
            </p>
            <UButton
              v-if="bindingReferences.length"
              :loading="bindingsBusy"
              :disabled="!bindingsDirty || !!locksMutations"
              @click="saveBindings"
              >Сохранить привязки ПД</UButton
            >
            <p v-if="bindingsDirty" class="muted">
              Сохраните привязки перед запуском проверки. Действующие проверки сохраняют прежние
              привязки.
            </p>
          </div>
          <div class="preflight-block">
            <div class="toolbar">
              <UButton
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
                aria-label="Проверить"
                @click="checkAndStart()"
              >
                Проверить
              </UButton>
            </div>
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

        <section v-else class="panel" aria-label="Редакции ПД">
          <h2>Редакции ПД</h2>
          <p class="muted">Действующая редакция выделяется явно и участвует в проверке.</p>
          <ul class="revision-list" aria-label="Все редакции ПД">
            <li
              v-for="version in sortedVersions"
              :key="version.id"
              class="revision-row"
              :class="{ 'revision-row-current': version.id === document.current_version_id }"
            >
              <div class="revision-main">
                <span
                  class="revision-name"
                  :title="`${version.filename}, редакция ${version.number}`"
                >
                  {{ version.filename }}, редакция {{ version.number }}
                </span>
                <UBadge
                  v-if="version.id === document.current_version_id"
                  color="primary"
                  variant="soft"
                  label="Действующая"
                />
                <UBadge
                  v-if="version.id === selectedVersion.id"
                  color="neutral"
                  variant="soft"
                  label="Выбрана"
                />
              </div>
              <div class="toolbar">
                <UButton
                  variant="link"
                  :aria-label="`Выбрать редакцию ${version.number}`"
                  @click="selectVersion(version.id)"
                >
                  Выбрать
                </UButton>
                <UButton
                  v-if="version.id !== document.current_version_id"
                  variant="link"
                  :disabled="!!locksMutations"
                  :aria-label="`Сделать действующей редакцию ${version.number}`"
                  @click="setCurrent(version.id)"
                >
                  Сделать действующей
                </UButton>
                <UButton
                  variant="link"
                  :aria-label="`Скачать ${version.filename}, редакция ${version.number}`"
                  @click="downloadRevision(version)"
                >
                  Скачать
                </UButton>
                <UButton
                  variant="link"
                  color="error"
                  :disabled="!!locksMutations"
                  :aria-label="`Удалить редакцию ${version.number}`"
                  @click="askDelete(version)"
                >
                  Удалить
                </UButton>
              </div>
            </li>
          </ul>
        </section>

        <section v-if="isVor" class="panel" aria-label="История редакций ВОР">
          <h2>История редакций</h2>
          <ul class="revision-list" aria-label="Все редакции ведомости">
            <li
              v-for="version in sortedVersions"
              :key="version.id"
              class="revision-row"
              :class="{ 'revision-row-current': version.id === document.current_version_id }"
            >
              <div class="revision-main">
                <span
                  class="revision-name"
                  :title="`${version.filename}, редакция ${version.number}`"
                >
                  {{ version.filename }}, редакция {{ version.number }} ·
                  {{ formatDate(version.created_at) }}
                </span>
                <UBadge
                  v-if="version.id === document.current_version_id"
                  color="primary"
                  variant="soft"
                  label="Действующая"
                />
              </div>
              <div class="toolbar">
                <UButton
                  variant="link"
                  :aria-label="`Выбрать редакцию ${version.number}`"
                  @click="selectVersion(version.id)"
                >
                  Выбрать
                </UButton>
                <UButton
                  variant="link"
                  :aria-label="`Скачать ${version.filename}, редакция ${version.number}`"
                  @click="downloadRevision(version)"
                >
                  Скачать
                </UButton>
                <UButton
                  variant="link"
                  color="error"
                  :disabled="!!locksMutations"
                  :aria-label="`Удалить редакцию ${version.number}`"
                  @click="askDelete(version)"
                >
                  Удалить
                </UButton>
              </div>
            </li>
          </ul>
        </section>

        <section v-if="isVor" class="panel" aria-label="Проверки выбранной редакции">
          <h2>Проверки редакции</h2>
          <p class="muted">Неизменяемые запуски для редакции {{ selectedVersion.number }}.</p>
          <p v-if="historyBusy" class="empty" role="status">Загружаем запуски…</p>
          <p v-else-if="historyError" class="error-box" role="alert">
            {{ historyError }}
            <UButton variant="link" aria-label="Повторить загрузку запусков" @click="loadHistory()">
              Повторить
            </UButton>
          </p>
          <p v-else-if="!historyRuns.length" class="empty" role="status">
            {{
              historyOffset || historyHasMore
                ? 'На этой странице истории запусков этой редакции нет.'
                : 'Для этой редакции запусков пока нет.'
            }}
          </p>
          <div v-else class="table-wrap">
            <UTable
              sticky="header"
              :data="historyRuns"
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
.binding-select {
  width: 100%;
  padding: 10px;
  border: 1px solid var(--ui-border);
  border-radius: 6px;
  background: var(--ui-bg);
  color: var(--ui-text);
}

.document-detail {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding-bottom: 48px;
}
.document-detail > .panel {
  padding: 24px;
  font-size: 14px;
}
.document-detail h2 {
  font-size: 16px;
  font-weight: 600;
}
.title-block {
  min-width: 0;
}
.title-text {
  overflow-wrap: anywhere;
}
.title-meta {
  overflow-wrap: anywhere;
}
.heading-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.document-alert {
  margin: 0;
}
.title-editor,
.revision-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.title-input {
  width: 100%;
}
.revision-bar {
  align-items: end;
}
.revision-select {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  flex: 1;
}
.revision-file {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
}
.source-name {
  overflow-wrap: anywhere;
}
.metadata-read {
  margin: 12px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.metadata-line {
  display: grid;
  grid-template-columns: minmax(140px, 1fr) minmax(0, 2fr) minmax(160px, 1fr);
  align-items: baseline;
  gap: 4px 12px;
  padding-block: 8px;
  border-top: 1px solid var(--ui-border);
}
.metadata-line dt {
  font-weight: 600;
}
.metadata-line dd {
  margin: 0;
}
.metadata-value {
  overflow-wrap: anywhere;
}
.metadata-row {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-block: 10px;
  border-top: 1px solid var(--ui-border);
}
.metadata-head {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: baseline;
  justify-content: space-between;
}
.metadata-source,
.metadata-raw {
  overflow-wrap: anywhere;
}
.metadata-input {
  width: 100%;
}
.revision-list {
  list-style: none;
  margin: 12px 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.revision-row {
  border: 1px solid var(--ui-border);
  border-radius: 8px;
  padding: 8px 12px;
}
.revision-row-current {
  border-color: var(--ui-primary);
}
.revision-name,
.delete-name {
  min-width: 0;
  overflow-wrap: anywhere;
}
.revision-main {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  min-width: 0;
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
.table-wrap {
  overflow-x: auto;
}
.cell-wrap {
  overflow-wrap: anywhere;
  min-width: 140px;
}
.ack-row {
  display: flex;
  gap: 8px;
  align-items: center;
}
.pagination {
  margin-top: 12px;
}
@media (max-width: 640px) {
  .document-detail > .panel {
    padding: 16px;
  }
  .metadata-line {
    grid-template-columns: 1fr;
  }
}
</style>

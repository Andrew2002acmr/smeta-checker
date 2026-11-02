<script setup lang="ts">
import type { ProjectDto, RunDto } from '~/generated/types.gen'
import type { TableColumn, TabsItem } from '@nuxt/ui'

type Queue = 'fix' | 'review' | 'missing' | 'ok'

interface DashboardProject {
  id: string
  name: string
  archived: boolean
  runs: number
  documents: number
  vorCount: number
  pdfCount: number
  lastRun: RunDto | null
  queues: Record<Queue, number> | null
  summaryBusy: boolean
}

const backend = useBackend()
const router = useRouter()
const route = useRoute()
const query = ref('')
const sort = ref<'recent' | 'name' | 'attention'>('recent')
const showArchived = computed({
  get: () => route.query.archived === 'true',
  set: (archived: boolean) => {
    void router.push({ query: { ...route.query, archived: archived ? 'true' : undefined } })
  },
})
const projectTabs: TabsItem[] = [
  { label: 'Активные', value: 'active', icon: 'i-lucide-folder-open' },
  { label: 'Архив', value: 'archived', icon: 'i-lucide-archive' },
]
const projectScope = computed({
  get: () => (showArchived.value ? 'archived' : 'active'),
  set: (value: string | number) => {
    showArchived.value = value === 'archived'
  },
})
const projects = ref<DashboardProject[]>([])
const error = ref('')
const busy = ref(true)
const hasLoaded = ref(false)
const scopeCache: Record<'active' | 'archived', DashboardProject[] | null> = {
  active: null,
  archived: null,
}
const renamingId = ref<string | null>(null)
const renamingName = ref('')
const renamingBusy = ref(false)
const confirmArchiveId = ref<string | null>(null)
const deleteProjectId = ref<string | null>(null)
const deleteProjectName = ref('')
const deleteProjectBusy = ref(false)
const archiveBusy = ref(false)
const actionError = ref('')
const restoringId = ref<string | null>(null)

let active = true
let epoch = 0
let timer: ReturnType<typeof setTimeout> | undefined

const attention = (p: DashboardProject) =>
  (p.queues?.fix || 0) * 10 + (p.queues?.review || 0) * 3 + (p.queues?.missing || 0)

const confirmArchiveProject = computed(
  () => projects.value.find((p) => p.id === confirmArchiveId.value) || null,
)
const deleteProjectTarget = computed(
  () => projects.value.find((p) => p.id === deleteProjectId.value) || null,
)

const filtered = computed(() => {
  const q = query.value.trim().toLocaleLowerCase('ru-RU')
  const list = projects.value.filter(
    (p) =>
      !q ||
      p.name.toLocaleLowerCase('ru-RU').includes(q) ||
      p.lastRun?.vor_filename.toLocaleLowerCase('ru-RU').includes(q),
  )
  if (sort.value === 'name') list.sort((a, b) => a.name.localeCompare(b.name, 'ru-RU'))
  else if (sort.value === 'attention') list.sort((a, b) => attention(b) - attention(a))
  else
    list.sort(
      (a, b) =>
        (b.lastRun ? Date.parse(b.lastRun.created_at || '') || 0 : 0) -
        (a.lastRun ? Date.parse(a.lastRun.created_at || '') || 0 : 0),
    )
  return list
})
const indexColumns: TableColumn<DashboardProject>[] = [
  { id: 'project', header: 'Проект' },
  { id: 'lastCheck', header: 'Последняя проверка' },
  { id: 'result', header: 'Результат' },
  { id: 'actions', header: '' },
]
function formatDate(value: string | null) {
  return value
    ? new Date(value).toLocaleString('ru-RU', {
        day: 'numeric',
        month: 'short',
        hour: '2-digit',
        minute: '2-digit',
      })
    : '-'
}

function countLabel(count: number, forms: [string, string, string]) {
  const last = count % 10
  const teen = count % 100 >= 11 && count % 100 <= 14
  return `${count} ${forms[teen ? 2 : last === 1 ? 0 : last >= 2 && last <= 4 ? 1 : 2]}`
}

function positionCount(count: number) {
  return countLabel(count, ['позиция', 'позиции', 'позиций'])
}

function confirmedLabel(count: number) {
  const singular = count % 10 === 1 && count % 100 !== 11
  return `${singular ? 'Подтверждена' : 'Подтверждено'} ${positionCount(count)}`
}

const queueLabels = {
  fix: 'Исправить',
  review: 'Проверить вручную',
  missing: 'Не выполнено',
} as const

const progressLabel = () => 'Прогресс проверки'

async function readQueues(runId: string): Promise<Record<Queue, number>> {
  const summary = await backend.summary(runId)
  return {
    fix: summary.queues.fix || 0,
    review: summary.queues.review || 0,
    missing: summary.queues.missing || 0,
    ok: summary.queues.ok || 0,
  }
}

async function retrySummary(p: DashboardProject) {
  if (!p.lastRun || p.summaryBusy) return
  p.summaryBusy = true
  try {
    p.queues = await readQueues(p.lastRun.run_id)
  } catch {
    p.queues = null
  } finally {
    p.summaryBusy = false
  }
}

const archiveModalOpen = computed({
  get: () => confirmArchiveId.value !== null,
  set: (open: boolean) => {
    if (!open) confirmArchiveId.value = null
  },
})
const deleteProjectOpen = computed({
  get: () => deleteProjectId.value !== null,
  set: (open: boolean) => {
    if (!open) deleteProjectId.value = null
  },
})

function openProject(p: DashboardProject) {
  router.push(`/projects/${p.id}`)
}

function startRename(p: DashboardProject) {
  actionError.value = ''
  renamingId.value = p.id
  renamingName.value = p.name
}

function cancelRename() {
  renamingId.value = null
  renamingName.value = ''
}

async function saveRename(p: DashboardProject) {
  actionError.value = ''
  const name = renamingName.value.trim()
  if (!name || renamingBusy.value) return
  renamingBusy.value = true
  try {
    const renamed = await backend.updateProject(p.id, { name })
    p.name = renamed.name
    cancelRename()
  } catch (e) {
    actionError.value = readableError(e)
  } finally {
    renamingBusy.value = false
  }
}

function askArchive(p: DashboardProject) {
  actionError.value = ''
  confirmArchiveId.value = p.id
}

async function restore(p: DashboardProject) {
  if (restoringId.value) return
  actionError.value = ''
  restoringId.value = p.id
  try {
    await backend.updateProject(p.id, { archived: false })
    scopeCache.active = null
    await load()
  } catch (e) {
    actionError.value = readableError(e)
  } finally {
    restoringId.value = null
  }
}

async function confirmArchive() {
  if (!confirmArchiveId.value || archiveBusy.value) return
  archiveBusy.value = true
  try {
    await backend.updateProject(confirmArchiveId.value, { archived: true })
    scopeCache.archived = null
    confirmArchiveId.value = null
    await load()
  } catch (e) {
    actionError.value = readableError(e)
  } finally {
    archiveBusy.value = false
  }
}

function askDeleteProject(p: DashboardProject) {
  deleteProjectId.value = p.id
  deleteProjectName.value = ''
}

async function confirmDeleteProject() {
  const target = deleteProjectTarget.value
  if (!target || deleteProjectBusy.value) return
  if (deleteProjectName.value.trim() !== target.name) return
  deleteProjectBusy.value = true
  try {
    await backend.deleteProject(target.id)
    scopeCache.active = null
    deleteProjectId.value = null
    await load()
  } catch (e) {
    actionError.value = readableError(e)
  } finally {
    deleteProjectBusy.value = false
  }
}

async function loadProject(p: ProjectDto): Promise<DashboardProject> {
  const [runs, documents] = await Promise.all([backend.runs(p.id, 0), backend.catalog(p.id)])
  const lastRun = runs[0] || null
  let queues: Record<Queue, number> | null = null
  if (lastRun?.status === 'done') {
    try {
      queues = await readQueues(lastRun.run_id)
    } catch {
      // Unavailable results stay distinct from a successful summary with zero findings.
    }
  }
  return {
    id: p.id,
    name: p.name,
    archived: Boolean(p.archived),
    runs: runs.length,
    documents: documents.length,
    vorCount: documents.filter((document) => document.kind === 'vor').length,
    pdfCount: documents.filter((document) => document.kind === 'pdf').length,
    lastRun,
    queues,
    summaryBusy: false,
  }
}

async function load(background = false) {
  clearTimeout(timer)
  const current = ++epoch
  const archived = showArchived.value
  busy.value = !background
  error.value = ''
  try {
    await backend.connect()
    const list = await backend.projects(archived)
    if (!active || current !== epoch) return
    const rows = await Promise.all(list.map(loadProject))
    if (!active || current !== epoch) return
    projects.value = rows
    scopeCache[archived ? 'archived' : 'active'] = rows
    void prefetchOther(archived, current)
    if (rows.some((p) => p.lastRun?.status === 'running'))
      timer = setTimeout(() => load(true), 5000)
  } catch (e) {
    if (active && current === epoch) {
      error.value = readableError(e)
      if (background) timer = setTimeout(() => load(true), 5000)
    }
  } finally {
    if (current === epoch) {
      busy.value = false
      hasLoaded.value = true
    }
  }
}
async function prefetchOther(archived: boolean, current: number) {
  const other = archived ? 'active' : 'archived'
  if (scopeCache[other] !== null) return
  try {
    const list = await backend.projects(!archived)
    const rows = await Promise.all(list.map(loadProject))
    if (active && current === epoch && scopeCache[other] === null) scopeCache[other] = rows
  } catch {
    // The regular load reports errors if this scope is opened.
  }
}

watch(showArchived, () => {
  const cached = scopeCache[showArchived.value ? 'archived' : 'active']
  projects.value = cached ?? []
  query.value = ''
  cancelRename()
  actionError.value = ''
  load(cached !== null)
})

onMounted(() => load())
onBeforeUnmount(() => {
  active = false
  epoch++
  clearTimeout(timer)
})
</script>

<template>
  <div class="dashboard-page">
    <WorkspaceHeader active="check" />
    <main id="main-content" tabindex="-1" class="shell dashboard">
      <div class="dashboard-heading">
        <div class="dashboard-title">
          <h1>Проекты</h1>
          <span v-if="!busy && !error && filtered.length" class="dashboard-count" role="status">
            {{ countLabel(filtered.length, ['проект', 'проекта', 'проектов']) }}
          </span>
        </div>
        <UButton
          v-if="showArchived || projects.length || busy || error"
          to="/upload"
          icon="i-lucide-plus"
          color="primary"
          >Новый проект</UButton
        >
      </div>

      <div class="dashboard-toolbar">
        <UTabs
          v-model="projectScope"
          :items="projectTabs"
          :content="false"
          :ui="{
            list: 'w-auto bg-elevated ring ring-default',
            indicator: 'bg-default shadow-sm',
            trigger: 'min-h-9 data-[state=active]:text-highlighted',
          }"
          class="dashboard-scope"
          color="neutral"
          activation-mode="manual"
          aria-label="Раздел проектов"
        />
        <UInput
          v-if="projects.length"
          v-model="query"
          icon="i-lucide-search"
          placeholder="Найти проект или ведомость"
          aria-label="Поиск проектов"
          class="dashboard-search"
        />
        <div v-if="projects.length" class="dashboard-sort">
          <USelect
            v-model="sort"
            :items="[
              { label: 'Недавние', value: 'recent' },
              { label: 'По вниманию', value: 'attention' },
              { label: 'По алфавиту', value: 'name' },
            ]"
            :ui="{
              base: 'cursor-pointer',
              item: 'cursor-pointer data-highlighted:not-data-disabled:before:bg-accented',
            }"
            aria-label="Сортировка проектов"
          />
        </div>
      </div>

      <div v-if="error && projects.length" class="dashboard-notice" role="alert">
        <UIcon name="i-lucide-circle-alert" aria-hidden="true" />
        <span>Не удалось обновить список. Показаны последние загруженные данные.</span>
        <UButton color="neutral" variant="link" @click="load()">Повторить</UButton>
      </div>
      <p v-if="actionError && !archiveModalOpen" class="dashboard-action-error" role="alert">
        {{ actionError }}
      </p>

      <section class="dashboard-panel" aria-label="Список проектов" :aria-busy="busy">
        <div
          v-if="busy && !hasLoaded && !projects.length && !error"
          class="dashboard-loading"
          aria-hidden="true"
        >
          <USkeleton v-for="n in 3" :key="n" class="dashboard-skeleton skeleton-name" />
        </div>
        <div
          v-else-if="busy && !projects.length && !error"
          class="dashboard-switch-loading"
          role="status"
        >
          Загрузка проектов…
        </div>
        <div v-else-if="error && !projects.length" class="dashboard-empty">
          <h2>Не удалось загрузить проекты</h2>
          <p>Проверьте соединение с сервером и попробуйте ещё раз.</p>
          <UButton color="neutral" variant="outline" @click="load()">Повторить</UButton>
        </div>
        <div v-else-if="!projects.length" class="dashboard-empty dashboard-empty-list">
          <UIcon
            :name="showArchived ? 'i-lucide-archive' : 'i-lucide-folder-plus'"
            class="dashboard-empty-icon"
            aria-hidden="true"
          />
          <h2>{{ showArchived ? 'Архив проектов пуст' : 'Пока нет проектов' }}</h2>
          <p>
            {{
              showArchived
                ? 'Архивированные проекты появятся здесь. Вернуться можно вкладкой «Активные».'
                : 'Создайте проект, загрузите ведомость объёмов работ и проектную документацию для проверки.'
            }}
          </p>
          <UButton v-if="!showArchived" to="/upload" icon="i-lucide-plus"> Создать проект </UButton>
        </div>
        <div v-else-if="!filtered.length" class="dashboard-empty">
          <h2>Ничего не найдено</h2>
          <p>По запросу «{{ query }}» нет проектов или ведомостей.</p>
          <UButton color="neutral" variant="outline" @click="query = ''">Сбросить поиск</UButton>
        </div>
        <div
          v-else
          class="dashboard-grid grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4"
        >
          <UPageCard
            v-for="project in filtered"
            :key="project.id"
            variant="outline"
            class="dashboard-card"
            @click="openProject(project)"
          >
            <template #header>
              <div class="dashboard-card-header">
                <div class="dashboard-card-title">
                  <NuxtLink
                    v-if="renamingId !== project.id"
                    :to="`/projects/${project.id}`"
                    class="dashboard-name"
                    :aria-label="`Открыть проект ${project.name}`"
                    @click.stop
                    >{{ project.name }}</NuxtLink
                  >
                  <div v-else class="dashboard-rename" @click.stop @keydown.enter.stop>
                    <UInput
                      v-model="renamingName"
                      aria-label="Новое название проекта"
                      :disabled="renamingBusy"
                      autofocus
                      class="rename-input"
                      @keydown.enter="saveRename(project)"
                      @keydown.escape="cancelRename"
                    />
                    <UButton
                      icon="i-lucide-check"
                      aria-label="Сохранить название"
                      color="neutral"
                      variant="outline"
                      :disabled="!renamingName.trim()"
                      :loading="renamingBusy"
                      @click="saveRename(project)"
                    />
                    <UButton
                      icon="i-lucide-x"
                      aria-label="Отменить переименование"
                      color="neutral"
                      variant="ghost"
                      :disabled="renamingBusy"
                      @click="cancelRename"
                    />
                  </div>
                </div>
                <div class="dashboard-actions" @click.stop>
                  <UDropdownMenu
                    :items="[
                      {
                        label: 'Переименовать',
                        icon: 'i-lucide-pencil',
                        disabled: renamingBusy,
                        onSelect: () => startRename(project),
                      },
                      {
                        label: project.archived ? 'Восстановить' : 'Архивировать',
                        icon: project.archived ? 'i-lucide-archive-restore' : 'i-lucide-archive',
                        disabled: archiveBusy || restoringId !== null,
                        onSelect: () => (project.archived ? restore(project) : askArchive(project)),
                      },
                    ]"
                    :content="{ align: 'end' }"
                  >
                    <UButton
                      color="neutral"
                      variant="ghost"
                      icon="i-lucide-ellipsis"
                      :loading="restoringId === project.id"
                      :aria-label="`Действия проекта ${project.name}`"
                    />
                  </UDropdownMenu>
                </div>
              </div>
              <div
                class="dashboard-meta"
                role="group"
                :aria-label="`Документов: ${project.documents}, проверок: ${project.runs}`"
              >
                <span>ВОР {{ project.vorCount }}</span>
                <span>ПД {{ project.pdfCount }}</span>
              </div>
            </template>
            <div class="dashboard-card-body">
              <div class="dashboard-last-check" :class="{ 'no-check': !project.lastRun }">
                <template v-if="project.lastRun">
                  <p class="dashboard-date">{{ formatDate(project.lastRun.created_at) }}</p>
                  <p class="dashboard-file" :title="project.lastRun.vor_filename">
                    {{ project.lastRun.vor_filename }}
                  </p>
                </template>
                <p v-else class="dashboard-muted">Нет проверок</p>
              </div>
              <div class="dashboard-result">
                <template v-if="!project.lastRun">
                  <p class="dashboard-muted">Ожидает документов</p>
                </template>
                <template v-else-if="project.lastRun.status === 'done'">
                  <div v-if="project.queues" class="dashboard-queues">
                    <template v-for="q in ['fix', 'review', 'missing'] as const" :key="q">
                      <p v-if="project.queues[q]" :class="`result-${q}`">
                        {{ queueLabels[q] }}: {{ positionCount(project.queues[q]) }}
                      </p>
                    </template>
                    <p v-if="project.queues.ok" class="result-ok">
                      {{ confirmedLabel(project.queues.ok) }}
                    </p>
                    <p v-if="!Object.values(project.queues).some(Boolean)" class="dashboard-muted">
                      Проверка без позиций
                    </p>
                  </div>
                  <div v-else class="dashboard-summary-error" @click.stop>
                    <p>Результат недоступен</p>
                    <UButton
                      color="neutral"
                      variant="link"
                      :loading="project.summaryBusy"
                      :aria-label="`Повторить загрузку результата ${project.name}`"
                      @click="retrySummary(project)"
                      >Повторить</UButton
                    >
                  </div>
                </template>
                <template v-else>
                  <p :class="{ 'result-fix': project.lastRun.status === 'failed' }">
                    {{ runLabel(project.lastRun.status) }}
                  </p>
                  <template v-if="project.lastRun.status === 'running'">
                    <p class="dashboard-detail">
                      {{
                        project.lastRun.total
                          ? `Обработано ${project.lastRun.done + project.lastRun.failed} из ${project.lastRun.total}`
                          : 'Подготовка проверки'
                      }}
                    </p>
                    <UProgress
                      v-if="project.lastRun.total"
                      :model-value="
                        Math.floor(
                          ((project.lastRun.done + project.lastRun.failed) /
                            project.lastRun.total) *
                            100,
                        )
                      "
                      color="neutral"
                      size="xs"
                      class="dashboard-progress"
                      :get-value-label="progressLabel"
                    />
                  </template>
                </template>
              </div>
            </div>
            <template #footer>
              <div class="dashboard-card-footer">
                <UBadge
                  v-if="project.lastRun?.status === 'done' && project.queues"
                  :color="Object.values(project.queues).some(Boolean) ? 'error' : 'success'"
                  variant="subtle"
                >
                  {{
                    Object.values(project.queues).some(Boolean) ? 'Есть замечания' : 'Без замечаний'
                  }}
                </UBadge>
                <UBadge
                  v-else-if="project.lastRun?.status === 'running'"
                  color="neutral"
                  variant="subtle"
                >
                  Проверка выполняется
                </UBadge>
              </div>
            </template>
          </UPageCard>
        </div>

        <p v-if="busy && !projects.length && !error" class="visually-hidden" role="status">
          Загружаем проекты…
        </p>
      </section>

      <UModal v-model:open="archiveModalOpen" title="Архивировать проект?">
        <template #body>
          <p class="modal-text">
            Проект «{{ confirmArchiveProject?.name }}» исчезнет из активного списка. Восстановить
            можно во вкладке «Архив».
          </p>
          <p v-if="actionError" class="dashboard-action-error" role="alert">{{ actionError }}</p>
          <div class="toolbar spread modal-footer">
            <UButton
              color="neutral"
              variant="ghost"
              :disabled="archiveBusy"
              @click="confirmArchiveId = null"
              >Отмена</UButton
            >
            <UButton color="primary" :loading="archiveBusy" @click="confirmArchive"
              >Архивировать</UButton
            >
          </div>
        </template>
      </UModal>
      <UModal v-model:open="deleteProjectOpen" title="Удалить проект?">
        <template #body>
          <p class="modal-text">
            Проект «{{ deleteProjectTarget?.name }}» и все его документы будут удалены без
            восстановления.
          </p>
          <UInput
            v-model="deleteProjectName"
            placeholder="Введите название проекта"
            aria-label="Название проекта для подтверждения удаления"
            class="w-full"
          />
          <div class="toolbar spread modal-footer">
            <UButton
              color="neutral"
              variant="ghost"
              :disabled="deleteProjectBusy"
              @click="deleteProjectId = null"
            >
              Отмена
            </UButton>
            <UButton
              color="error"
              :loading="deleteProjectBusy"
              :disabled="deleteProjectName.trim() !== deleteProjectTarget?.name"
              @click="confirmDeleteProject"
            >
              Удалить
            </UButton>
          </div>
        </template>
      </UModal>
    </main>
  </div>
</template>

<style scoped>
.dashboard {
  max-width: 1440px;
  padding-top: 32px;
}
.dashboard-heading,
.dashboard-title {
  display: flex;
  align-items: center;
  gap: 16px;
}
.dashboard-heading {
  min-height: 44px;
  justify-content: space-between;
  margin-bottom: 24px;
}
.dashboard-title h1 {
  margin: 0;
  font-size: 28px;
  font-weight: 650;
  letter-spacing: -0.025em;
  line-height: 1.3;
}
.dashboard-count {
  color: var(--ui-text-muted);
  font-size: 14px;
  font-variant-numeric: tabular-nums;
}
.dashboard-toolbar {
  display: grid;
  grid-template-columns: 1fr minmax(220px, 300px) 184px;
  align-items: center;
  gap: 16px;
  margin-bottom: 16px;
}
.dashboard-scope {
  justify-self: start;
}
.dashboard-search,
.dashboard-sort {
  width: 100%;
  min-width: 0;
}
.dashboard-sort :deep(button) {
  width: 100%;
  min-width: 0;
}
.dashboard-panel {
  background: var(--ui-bg);
  border: 1px solid var(--ui-border-muted);
  border-radius: 8px;
  padding-inline: 20px;
  min-width: 0;
}
.dashboard-panel :deep(.dashboard-table th:first-child) {
  width: 40%;
}
.dashboard-panel :deep(.dashboard-table th:nth-child(2)) {
  width: 25%;
}
.dashboard-panel :deep(.dashboard-table th:last-child) {
  width: 44px;
}
.dashboard-panel :deep(.dashboard-table tbody tr[data-selectable='true']:hover) {
  background: var(--ui-bg-muted);
}
.dashboard-name {
  display: block;
  width: fit-content;
  max-width: 100%;
  color: var(--ui-text-highlighted);
  font-size: 17px;
  font-weight: 600;
  overflow-wrap: break-word;
}
.dashboard-name:hover {
  text-decoration: underline;
}
.dashboard-card {
  cursor: pointer;
}
.dashboard-card-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}
.dashboard-card-title {
  min-width: 0;
  flex: 1;
}
.dashboard-card-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.dashboard-card-footer {
  display: flex;
  align-items: center;
  gap: 8px;
}
.dashboard-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  margin-top: 4px;
  color: var(--ui-text-muted);
  font-size: 14px;
  font-variant-numeric: tabular-nums;
}
.dashboard-meta span {
  white-space: nowrap;
}
.dashboard-file,
.dashboard-detail {
  color: var(--ui-text-muted);
  font-size: 14px;
  margin-top: 4px;
}
.dashboard-file {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.dashboard-date {
  font-variant-numeric: tabular-nums;
}
.dashboard-muted,
.dashboard-summary-error {
  color: var(--ui-text-muted);
}
.dashboard-queues {
  display: grid;
  gap: 4px;
}
.result-ok {
  color: var(--app-success-text);
}
.result-fix {
  color: var(--app-error-text);
}
.result-review {
  color: var(--app-warning-text);
}
.result-missing {
  color: var(--ui-text-muted);
}
.dashboard-progress {
  margin-top: 8px;
  max-width: 200px;
}
.dashboard-actions {
  text-align: right;
}
.dashboard-rename {
  display: flex;
  align-items: center;
  gap: 4px;
}
.rename-input {
  min-width: 0;
  flex: 1;
}
.dashboard-skeleton {
  background: var(--ui-bg-accented);
  animation: none;
  height: 20px;
  max-width: 100%;
  border-radius: 4px;
}
.skeleton-name {
  width: 180px;
}
.skeleton-meta {
  width: 140px;
  height: 14px;
  margin-top: 8px;
}
.skeleton-date {
  width: 120px;
}
.skeleton-result {
  width: 180px;
}
.dashboard-table td.dashboard-empty {
  padding-block: 32px;
}
.dashboard-switch-loading {
  display: grid;
  min-height: clamp(320px, 45vh, 480px);
  place-items: center;
  color: var(--ui-text-muted);
}
.dashboard-empty-list {
  display: flex;
  min-height: clamp(320px, 45vh, 480px);
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding-block: 32px;
  text-align: center;
}
.dashboard-empty-icon {
  width: 32px;
  height: 32px;
  margin-bottom: 12px;
  color: var(--ui-text-muted);
}
.dashboard-empty h2 {
  font-size: 18px;
  font-weight: 600;
  margin: 0 0 8px;
}
.dashboard-empty p {
  color: var(--ui-text-muted);
  font-size: 15px;
  margin: 0 0 16px;
  max-width: 480px;
}
.dashboard-notice {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
  color: var(--ui-text-muted);
  font-size: 15px;
}
.dashboard-notice > svg {
  flex-shrink: 0;
}
.dashboard-action-error {
  color: var(--app-error-text);
  margin-bottom: 16px;
}
.modal-text {
  padding: 0 24px 8px;
  overflow-wrap: break-word;
}
.modal-footer {
  margin: 4px 24px 8px;
}
.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}
@media (max-width: 900px) {
  .dashboard-toolbar {
    grid-template-columns: 1fr 184px;
    gap: 12px 16px;
  }
  .dashboard-scope {
    grid-column: 1;
  }
  .dashboard-sort {
    grid-column: 2;
    grid-row: 1;
  }
  .dashboard-search {
    grid-column: 1 / -1;
  }
  .dashboard-panel :deep(.dashboard-table th:first-child) {
    width: 39%;
  }
}
@media (max-width: 640px) {
  .dashboard {
    padding: 20px 16px 40px;
  }
  .dashboard-heading {
    margin-bottom: 16px;
    gap: 12px;
  }
  .dashboard-title {
    display: block;
  }
  .dashboard-title h1 {
    font-size: 24px;
  }
  .dashboard-count {
    font-size: 13px;
  }
  .dashboard-toolbar {
    grid-template-columns: minmax(0, 1fr);
    gap: 12px 8px;
  }
  .dashboard-sort {
    grid-column: 1;
    grid-row: auto;
  }
  .dashboard-panel {
    padding-inline: 16px;
  }
  .dashboard-panel :deep(.dashboard-table thead) {
    display: none;
  }
  .dashboard-panel :deep(.dashboard-table table),
  .dashboard-panel :deep(.dashboard-table tbody) {
    display: block;
  }
  .dashboard-panel :deep(.dashboard-table tbody tr[data-selectable='true']) {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 44px;
    gap: 8px 12px;
    padding-block: 16px;
  }
  .dashboard-panel :deep(.dashboard-table tbody td) {
    display: block;
    min-width: 0;
    padding: 0;
  }
  .dashboard-panel :deep(.dashboard-table tbody td:nth-child(1)) {
    grid-column: 1;
    grid-row: 1;
  }
  .dashboard-panel :deep(.dashboard-table tbody td:nth-child(2)) {
    grid-column: 1 / -1;
    grid-row: 3;
  }
  .dashboard-panel :deep(.dashboard-table tbody td:nth-child(3)) {
    grid-column: 1 / -1;
    grid-row: 2;
  }
  .dashboard-panel :deep(.dashboard-table tbody td:nth-child(4)) {
    grid-column: 2;
    grid-row: 1;
  }
  .dashboard-last-check {
    display: flex !important;
    flex-wrap: wrap;
    gap: 4px 12px;
    color: var(--ui-text-muted);
    font-size: 14px;
  }
  .dashboard-last-check.no-check {
    display: none !important;
  }
  .dashboard-last-check .dashboard-file {
    margin: 0;
    max-width: 100%;
  }
  .dashboard-meta {
    gap: 4px 10px;
  }
  .dashboard-loading-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    padding-block: 20px;
    border-top: 1px solid var(--ui-border-muted);
  }
  .dashboard-loading-row td:first-child {
    grid-column: 1 / -1;
  }
  .dashboard-loading-row td:last-child {
    display: none;
  }
  .dashboard-notice {
    flex-wrap: wrap;
  }
}
</style>

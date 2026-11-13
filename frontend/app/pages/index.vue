<script setup lang="ts">
import type { ProjectDto, RunDto } from '~/generated/types.gen'
import type { TabsItem } from '@nuxt/ui'

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
  status:
    | 'running'
    | 'error'
    | 'failed'
    | 'issues'
    | 'stale'
    | 'unverified'
    | 'clear'
    | 'empty'
    | 'not-started'
  searchNames: string[]
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
const archiveBusy = ref(false)
const actionError = ref('')
const restoringId = ref<string | null>(null)

let active = true
let epoch = 0
let timer: ReturnType<typeof setTimeout> | undefined

const attention = (p: DashboardProject) =>
  ({
    error: 7,
    failed: 6,
    running: 5,
    issues: 4,
    stale: 3,
    unverified: 2,
    'not-started': 1,
    empty: 1,
    clear: 0,
  })[p.status]

const confirmArchiveProject = computed(
  () => projects.value.find((p) => p.id === confirmArchiveId.value) || null,
)

const filtered = computed(() => {
  const q = query.value.trim().toLocaleLowerCase('ru-RU')
  const list = projects.value.filter(
    (p) =>
      !q ||
      p.name.toLocaleLowerCase('ru-RU').includes(q) ||
      p.searchNames.some((name) => name.includes(q)),
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

function statusText(p: DashboardProject) {
  return {
    running: 'Проверка выполняется',
    error: 'Не удалось загрузить результаты',
    failed: 'Есть незавершённые проверки',
    issues: 'Есть замечания',
    stale: 'Есть неактуальные проверки',
    unverified: 'Есть непроверенные ВОР',
    clear: 'Проверки без замечаний',
    empty: 'Добавьте документы',
    'not-started': 'Проверки не запускались',
  }[p.status]
}

function statusClass(p: DashboardProject) {
  return p.status === 'issues' || p.status === 'error' || p.status === 'failed'
    ? 'result-fix'
    : p.status === 'stale' || p.status === 'unverified'
      ? 'result-review'
      : p.status === 'clear'
        ? 'result-ok'
        : 'dashboard-muted'
}

function countLabel(count: number, forms: [string, string, string]) {
  const last = count % 10
  const teen = count % 100 >= 11 && count % 100 <= 14
  return `${count} ${forms[teen ? 2 : last === 1 ? 0 : last >= 2 && last <= 4 ? 1 : 2]}`
}

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
  if (p.summaryBusy) return
  p.summaryBusy = true
  try {
    Object.assign(
      p,
      await loadProject({ id: p.id, name: p.name, archived: p.archived } as ProjectDto),
    )
  } catch {
    p.status = 'error'
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

async function loadProject(p: ProjectDto): Promise<DashboardProject> {
  const [runs, documents, count] = await Promise.all([
    backend.runs(p.id, 0),
    backend.catalog(p.id),
    backend.runCount(p.id),
  ])
  const vors = documents.filter((document) => document.kind === 'vor')
  const completed = vors.filter(
    (document) => document.last_run?.status === 'done' && document.run_freshness === 'current',
  )
  const summaries = await Promise.all(
    completed.map(async (document) => {
      try {
        return await readQueues(document.last_run!.run_id)
      } catch {
        return null
      }
    }),
  )
  let status: DashboardProject['status']
  if (!documents.length) status = 'empty'
  else if (vors.some((document) => document.last_run?.status === 'running')) status = 'running'
  else if (
    vors.some((document) => document.run_freshness === 'unavailable') ||
    summaries.some((summary) => !summary)
  )
    status = 'error'
  else if (
    vors.some(
      (document) => document.last_run && !['done', 'running'].includes(document.last_run.status),
    )
  )
    status = 'failed'
  else if (
    summaries.some((summary) => summary && (summary.fix || summary.review || summary.missing))
  )
    status = 'issues'
  else if (vors.some((document) => document.run_freshness === 'stale')) status = 'stale'
  else if (!vors.some((document) => document.last_run)) status = 'not-started'
  else if (
    vors.some((document) => !document.last_run) ||
    summaries.some((summary) => summary && !Object.values(summary).some(Boolean))
  )
    status = 'unverified'
  else status = 'clear'
  return {
    id: p.id,
    name: p.name,
    archived: Boolean(p.archived),
    runs: count.count,
    documents: documents.length,
    vorCount: vors.length,
    pdfCount: documents.filter((document) => document.kind === 'pdf').length,
    lastRun: runs[0] || null,
    status,
    searchNames: vors
      .flatMap((document) => [
        document.title,
        ...document.versions.map((version) => version.filename),
      ])
      .map((name) => name.toLocaleLowerCase('ru-RU')),
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
    if (rows.some((p) => p.status === 'running')) timer = setTimeout(() => load(true), 5000)
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
        <div class="dashboard-controls">
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
          <USeparator
            v-if="projects.length"
            orientation="vertical"
            decorative
            class="dashboard-separator"
          />
          <UButton
            v-if="showArchived || projects.length || busy || error"
            to="/upload"
            icon="i-lucide-plus"
            color="primary"
            class="dashboard-create"
            >Новый проект</UButton
          >
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

      <section class="dashboard-list" aria-label="Список проектов" :aria-busy="busy">
        <div
          v-if="busy && !hasLoaded && !projects.length && !error"
          class="dashboard-grid"
          aria-hidden="true"
        >
          <USkeleton v-for="n in 3" :key="n" class="dashboard-skeleton dashboard-skeleton-card" />
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
        <div v-else class="dashboard-grid" role="list" aria-label="Список проектов">
          <UPageCard
            v-for="project in filtered"
            :key="project.id"
            variant="outline"
            class="dashboard-card"
            role="listitem"
            :aria-label="`Проект ${project.name}`"
            :ui="{
              wrapper: 'min-w-0 w-full',
              header: 'mb-3 w-full',
              body: 'flex w-full min-w-0 flex-col gap-2',
              footer: 'pt-3',
            }"
            @click="openProject(project)"
          >
            <template #header>
              <div class="dashboard-card-header">
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
                    size="sm"
                    class="dashboard-card-menu"
                    :ui="{ base: 'size-8 min-h-0 min-w-0 p-0', leadingIcon: 'size-4' }"
                    :loading="restoringId === project.id"
                    :aria-label="`Действия проекта ${project.name}`"
                    @click.stop
                  />
                </UDropdownMenu>
              </div>
              <div
                class="dashboard-meta"
                role="group"
                :aria-label="`Документов: ${project.documents}, проверок: ${project.runs}`"
              >
                <UBadge color="neutral" variant="outline" size="md">
                  ВОР · {{ project.vorCount }}
                </UBadge>
                <UBadge color="neutral" variant="outline" size="md">
                  ПД · {{ project.pdfCount }}
                </UBadge>
                <UBadge color="neutral" variant="outline" size="md">
                  Проверок · {{ project.runs }}
                </UBadge>
              </div>
            </template>
            <template #body>
              <p class="dashboard-status" :class="statusClass(project)">
                {{ statusText(project) }}
              </p>
              <div v-if="project.status === 'error'" class="dashboard-summary-error" @click.stop>
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
          </UPageCard>
        </div>
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
    </main>
  </div>
</template>

<style scoped>
.dashboard {
  max-width: 1440px;
  padding-top: 32px;
  container-type: inline-size;
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
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px 16px;
  margin-bottom: 16px;
}
.dashboard-scope {
  margin-right: auto;
}
.dashboard-controls {
  display: flex;
  flex: 1 1 600px;
  flex-wrap: wrap;
  justify-content: flex-end;
  align-items: center;
  gap: 12px 16px;
  min-width: 0;
}
.dashboard-search {
  flex: 1 1 220px;
  width: 100%;
  max-width: 300px;
  min-width: 0;
}
.dashboard-sort {
  flex: 0 1 184px;
  min-width: 0;
}
.dashboard-controls :deep(.dashboard-separator) {
  height: 24px;
  flex: none;
}
.dashboard-create {
  flex: none;
}
.dashboard-sort :deep(button) {
  width: 100%;
  min-width: 0;
}
.dashboard-list {
  min-width: 0;
}
.dashboard-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 16px;
}
@media (min-width: 700px) {
  .dashboard-grid {
    grid-template-columns: repeat(2, minmax(0, 340px));
  }
}
@media (min-width: 1100px) {
  .dashboard-grid {
    grid-template-columns: repeat(3, minmax(0, 340px));
  }
}
@media (min-width: 1440px) {
  .dashboard-grid {
    grid-template-columns: repeat(4, minmax(0, 340px));
  }
}
.dashboard-name {
  display: block;
  min-width: 0;
  flex: 1;
  color: var(--ui-text-highlighted);
  font-size: 17px;
  font-weight: 600;
  overflow-wrap: break-word;
}
.dashboard-name:hover {
  text-decoration: underline;
}
.dashboard-card {
  min-width: 0;
  height: 100%;
  cursor: pointer;
}
.dashboard-card-header {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}
.dashboard-card-menu {
  margin-left: auto;
  flex-shrink: 0;
}
.dashboard-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}
.dashboard-status {
  font-size: 15px;
  font-weight: 600;
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
  max-width: 100%;
  border-radius: 8px;
}
.dashboard-skeleton-card {
  height: 190px;
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
@container (max-width: 650px) {
  .dashboard-controls {
    flex-basis: 100%;
    justify-content: flex-start;
  }
  .dashboard-search {
    flex-basis: 100%;
    max-width: none;
  }
  .dashboard-sort {
    flex: 1 1 130px;
  }
}
@container (max-width: 350px) {
  .dashboard-sort {
    flex-basis: 110px;
  }
  .dashboard-controls :deep(.dashboard-separator) {
    display: none;
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
  .dashboard-meta {
    gap: 4px 10px;
  }
  .dashboard-notice {
    flex-wrap: wrap;
  }
}
</style>

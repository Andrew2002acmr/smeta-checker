<script setup lang="ts">
import type { ProjectDto, RunDto } from '~/generated/types.gen'
import type { TableColumn } from '@nuxt/ui'
import { runLabel } from '~/utils/presentation'
const backend = useBackend(),
  route = useRoute(),
  router = useRouter(),
  projects = ref<ProjectDto[]>([]),
  runs = ref<RunDto[]>([]),
  selected = ref(''),
  offset = ref(0),
  error = ref(''),
  busy = ref(true),
  renaming = ref(false),
  renamingName = ref(''),
  renamingBusy = ref(false)
const historyColumns: TableColumn<RunDto>[] = [
  { id: 'run', accessorFn: (row: RunDto) => row.vor_filename, header: 'Проверка' },
  { accessorKey: 'created_at', header: 'Создано' },
  { accessorKey: 'status', header: 'Состояние' },
  {
    id: 'progress',
    accessorFn: (row: RunDto) => row.done + row.failed,
    header: 'Позиций обработано',
  },
  { id: 'action', header: '', enableSorting: false },
]
const historySorting = ref<any[]>([])
const historyPagination = ref({ pageIndex: 0, pageSize: 50 })
const projectName = computed(() => projects.value.find((p) => p.id === selected.value)?.name || '')
let epoch = 0
let active = true
let timer: ReturnType<typeof setTimeout> | undefined
function startRename() {
  renamingName.value = projectName.value
  renaming.value = true
}
async function saveRename() {
  const name = renamingName.value.trim()
  if (!name || !selected.value || renamingBusy.value) return
  renamingBusy.value = true
  try {
    const renamed = await backend.renameProject(selected.value, name)
    const project = projects.value.find((p) => p.id === selected.value)
    if (project) project.name = renamed.name
    renaming.value = false
  } catch (e) {
    error.value = readableError(e)
  } finally {
    renamingBusy.value = false
  }
}
function changePage(delta: number) {
  offset.value = Math.max(0, offset.value + delta)
  loadRuns()
}
function setPage(page: number) {
  offset.value = Math.max(0, (page - 1) * 50)
  loadRuns()
}
async function loadRuns(background = false) {
  clearTimeout(timer)
  const current = ++epoch
  if (!selected.value) {
    runs.value = []
    busy.value = false
    return
  }
  if (!background) busy.value = true
  error.value = ''
  try {
    const values = await backend.runs(selected.value, offset.value)
    if (active && current === epoch) {
      runs.value = values
      historyPagination.value = { pageIndex: Math.floor(offset.value / 50), pageSize: 50 }
      if (values.some((run) => run.status === 'running'))
        timer = setTimeout(() => loadRuns(true), 3000)
    }
  } catch (e) {
    if (active && current === epoch) {
      error.value = readableError(e)
      if (background) timer = setTimeout(() => loadRuns(true), 5000)
    }
  } finally {
    if (current === epoch) busy.value = false
  }
}
watch(selected, (id) => {
  offset.value = 0
  router.replace({ query: id ? { project: id } : {} })
  loadRuns()
})
async function initialize() {
  busy.value = true
  try {
    await backend.connect()
    projects.value = await backend.projects()
    const requested = typeof route.query.project === 'string' ? route.query.project : ''
    if (requested && !projects.value.some((project) => project.id === requested))
      projects.value.push(await backend.project(requested))
    if (!active) return
    const id = projects.value.some((p) => p.id === route.query.project)
      ? String(route.query.project)
      : projects.value[0]?.id || ''
    if (id === selected.value) await loadRuns()
    else selected.value = id
    if (!selected.value) busy.value = false
  } catch (e) {
    error.value = readableError(e)
    busy.value = false
  }
}
onMounted(initialize)
onBeforeUnmount(() => {
  active = false
  epoch++
  clearTimeout(timer)
})
</script>
<template>
  <div>
    <WorkspaceHeader active="projects" />
    <main id="main-content" tabindex="-1" class="shell">
      <div class="page-heading">
        <div>
          <h1>Проверки</h1>
          <div v-if="renaming" class="history-rename">
            <UInput
              v-model="renamingName"
              aria-label="Новое название проекта"
              :disabled="renamingBusy"
              autofocus
              @keydown.enter="saveRename"
              @keydown.escape="renaming = false"
            />
            <UButton
              icon="i-lucide-check"
              aria-label="Сохранить название"
              color="primary"
              :loading="renamingBusy"
              @click="saveRename"
            />
            <UButton
              icon="i-lucide-x"
              aria-label="Отменить переименование"
              color="neutral"
              variant="ghost"
              :disabled="renamingBusy"
              @click="renaming = false"
            />
          </div>
          <p v-else class="muted history-project">
            {{ projectName || 'Сохранённые запуски с исходными версиями документов' }}
            <UButton
              v-if="projectName"
              icon="i-lucide-pencil"
              :aria-label="`Переименовать ${projectName}`"
              color="neutral"
              variant="ghost"
              size="xs"
              @click="startRename"
            />
          </p>
        </div>
        <div class="heading-actions">
          <UButton v-if="selected" :to="`/projects/${selected}`" variant="outline">
            В проект
          </UButton>
        </div>
      </div>
      <div v-if="error" class="error-box" role="alert">
        {{ error }}
        <UButton variant="link" @click="selected ? loadRuns() : initialize()">Повторить</UButton>
      </div>
      <USelect
        v-if="projects.length"
        v-model="selected"
        :items="projects.map((p) => ({ label: p.name, value: p.id }))"
        aria-label="Проект истории"
        class="project-select"
      />
      <p v-if="busy" class="empty" role="status">Открываем историю…</p>
      <div v-else-if="runs.length" class="panel table-wrap" :aria-busy="busy">
        <UTable
          sticky="header"
          :data="runs"
          v-model:sorting="historySorting"
          v-model:pagination="historyPagination"
          :pagination-options="{
            manualPagination: true,
            rowCount: offset + runs.length + (runs.length === 50 ? 50 : 0),
          }"
          :columns="historyColumns"
          :on-select="(e, row) => router.push(`/audits/${row.original.run_id}`)"
          class="results max-h-[60vh] overflow-auto"
        >
          <template #run-cell="{ row }">
            <div>
              <strong class="run-name">{{ row.original.vor_filename }}</strong>
              <p class="muted">{{ projectName }}</p>
            </div>
          </template>
          <template #created_at-cell="{ row }">
            <span class="numbers">
              {{
                row.original.created_at
                  ? new Date(row.original.created_at).toLocaleString('ru-RU')
                  : '—'
              }}
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
              variant="subtle"
            >
              {{ runLabel(row.original.status) }}
            </UBadge>
          </template>
          <template #progress-cell="{ row }">
            <span class="numbers"
              >{{ row.original.done + row.original.failed }} / {{ row.original.total ?? '…' }}</span
            >
          </template>
          <template #action-cell="{ row }">
            <UButton variant="ghost" :to="`/audits/${row.original.run_id}`" @click.stop>
              Открыть проверку
            </UButton>
          </template>
        </UTable>
      </div>
      <div v-else class="empty empty-state">
        <p>Сохранённых проверок пока нет. Загрузите ВОР и проектные PDF.</p>
        <UButton to="/upload">Новая проверка</UButton>
      </div>
      <div v-if="offset || runs.length === 50" class="toolbar pagination">
        <UPagination
          :page="Math.floor(offset / 50) + 1"
          :items-per-page="50"
          :total="offset + runs.length + (runs.length === 50 ? 50 : 0)"
          @update:page="setPage"
        />
      </div>
    </main>
  </div>
</template>
<style scoped>
.heading-actions {
  display: flex;
  gap: 12px;
  align-items: center;
}
.history-project {
  display: flex;
  align-items: center;
  gap: 4px;
}
.history-rename {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
}
.project-select {
  max-width: 420px;
  margin: 20px 0;
}
.table-wrap {
  overflow: auto;
}
.run-name {
  display: block;
  color: var(--ui-text-highlighted);
  overflow-wrap: anywhere;
}
.numbers {
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.row-action {
  text-align: right;
  white-space: nowrap;
}
.run-row {
  cursor: pointer;
}
.pagination {
  margin-top: 20px;
}
.empty-state {
  display: flex;
  flex-direction: column;
  gap: 16px;
  align-items: flex-start;
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
  .results thead {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
  .results tbody {
    display: block;
  }
  .results tr {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 12px;
    padding: 20px 16px;
  }
  .results td {
    display: block;
    padding: 0;
    border: 0;
  }
  .results td:first-child,
  .row-action {
    grid-column: 1 / -1;
  }
  .results tbody tr + tr {
    border-top: 1px solid var(--ui-border);
  }
  .row-action {
    text-align: start;
  }
  .row-action > :first-child {
    width: 100%;
  }
}
</style>

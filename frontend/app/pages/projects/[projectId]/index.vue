<script setup lang="ts">
import type { DepartmentDto, ProjectDto } from '~/generated/types.gen'

const backend = useBackend(),
  route = useRoute(),
  router = useRouter()
const projectId = computed(() => String(route.params.projectId || ''))
const project = ref<ProjectDto | null>(null)
const departments = ref<DepartmentDto[]>([])
const busy = ref(true),
  error = ref(''),
  actionError = ref('')
const filtered = computed(() => departments.value)
const renamingId = ref<string | null>(null),
  nameDraft = ref(''),
  renameBusy = ref(false)
const mutationBusy = ref(false),
  actionTarget = ref<DepartmentDto | null>(null)
const confirmName = ref(''),
  actionOpen = ref(false)
const creating = ref(false),
  newDepartmentName = ref(''),
  createBusy = ref(false),
  createError = ref('')
const projectRenameOpen = ref(false),
  projectNameDraft = ref(''),
  projectBusy = ref(false),
  projectArchiveOpen = ref(false)
let active = true,
  epoch = 0
let refreshTimer: ReturnType<typeof setTimeout> | undefined

function departmentPath(item: DepartmentDto) {
  return `/projects/${projectId.value}/departments/${item.id}`
}
async function createDepartment() {
  const name = newDepartmentName.value.trim()
  if (!name || createBusy.value) return
  createBusy.value = true
  createError.value = ''
  try {
    await backend.createDepartment(projectId.value, name)
    newDepartmentName.value = ''
    creating.value = false
    await load()
  } catch (e) {
    createError.value = readableError(e)
  } finally {
    createBusy.value = false
  }
}
function cancelCreate() {
  if (createBusy.value) return
  newDepartmentName.value = ''
  createError.value = ''
  creating.value = false
}
function openDepartment(item: DepartmentDto) {
  if (renamingId.value !== item.id) void router.push(departmentPath(item))
}
function startRename(item: DepartmentDto) {
  actionError.value = ''
  renamingId.value = item.id
  nameDraft.value = item.name
}
async function saveRename(item: DepartmentDto) {
  if (!nameDraft.value.trim() || renameBusy.value) return
  renameBusy.value = true
  actionError.value = ''
  try {
    Object.assign(
      item,
      await backend.updateDepartment(projectId.value, item.id, { name: nameDraft.value.trim() }),
    )
    renamingId.value = null
  } catch (e) {
    actionError.value = readableError(e)
  } finally {
    renameBusy.value = false
  }
}
function askAction(item: DepartmentDto) {
  actionError.value = ''
  actionTarget.value = item
  confirmName.value = ''
  actionOpen.value = true
}
async function confirmAction() {
  const target = actionTarget.value
  if (!target || mutationBusy.value || confirmName.value.trim() !== target.name) return
  mutationBusy.value = true
  actionError.value = ''
  try {
    await backend.deleteDepartment(projectId.value, target.id)
    actionOpen.value = false
    await load()
  } catch (e) {
    actionError.value = readableError(e)
  } finally {
    mutationBusy.value = false
  }
}
function editProject() {
  projectNameDraft.value = project.value?.name || ''
  actionError.value = ''
  projectRenameOpen.value = true
}
async function saveProject() {
  if (!projectNameDraft.value.trim() || projectBusy.value) return
  projectBusy.value = true
  actionError.value = ''
  try {
    project.value = await backend.updateProject(projectId.value, {
      name: projectNameDraft.value.trim(),
    })
    projectRenameOpen.value = false
  } catch (e) {
    actionError.value = readableError(e)
  } finally {
    projectBusy.value = false
  }
}
async function archiveProject(archived: boolean) {
  projectBusy.value = true
  actionError.value = ''
  try {
    project.value = await backend.updateProject(projectId.value, { archived })
    projectArchiveOpen.value = false
  } catch (e) {
    actionError.value = readableError(e)
  } finally {
    projectBusy.value = false
  }
}
function departmentActions(item: DepartmentDto) {
  return [
    {
      label: 'Переименовать',
      icon: 'i-lucide-pencil',
      disabled: project.value?.archived || renameBusy.value,
      onSelect: () => startRename(item),
    },
    ...(item.is_system
      ? []
      : [
          {
            label: 'Удалить',
            icon: 'i-lucide-trash-2',
            color: 'error' as const,
            disabled: project.value?.archived || mutationBusy.value,
            onSelect: () => askAction(item),
          },
        ]),
  ]
}
const projectActions = computed(() => [
  { label: 'Переименовать', icon: 'i-lucide-pencil', onSelect: editProject },
  {
    label: project.value?.archived ? 'Восстановить' : 'Архивировать',
    icon: project.value?.archived ? 'i-lucide-archive-restore' : 'i-lucide-archive',
    onSelect: () => {
      if (project.value?.archived) void archiveProject(false)
      else projectArchiveOpen.value = true
    },
  },
])
async function load() {
  const current = ++epoch,
    id = projectId.value
  busy.value = true
  error.value = ''
  if (refreshTimer) clearTimeout(refreshTimer)
  try {
    await backend.connect()
    const [owner, items] = await Promise.all([backend.project(id), backend.departments(id)])
    if (!active || current !== epoch) return
    project.value = owner
    departments.value = items
    if (items.some((item) => item.running_count))
      refreshTimer = setTimeout(() => void load(), 15000)
  } catch (e) {
    if (active && current === epoch) error.value = readableError(e)
  } finally {
    if (active && current === epoch) busy.value = false
  }
}
watch(projectId, () => {
  renamingId.value = null
  void load()
})
onMounted(() => void load())
onBeforeUnmount(() => {
  active = false
  epoch++
  if (refreshTimer) clearTimeout(refreshTimer)
})
</script>

<template>
  <div>
    <WorkspaceHeader active="projects" />
    <main id="main-content" tabindex="-1" class="shell departments">
      <nav aria-label="Навигация">
        <UButton to="/" variant="link" color="neutral" icon="i-lucide-arrow-left"
          >Все проекты</UButton
        >
      </nav>
      <div v-if="busy && !project" class="department-loading" role="status">
        <USkeleton class="h-9 w-80 max-w-full" />
        <p class="muted">Загружаем отделы…</p>
      </div>
      <UAlert
        v-else-if="error && !project"
        color="error"
        variant="soft"
        title="Не удалось загрузить проект"
        :description="error"
        ><template #actions
          ><UButton color="error" variant="soft" @click="load">Повторить</UButton></template
        ></UAlert
      >
      <template v-else-if="project">
        <div class="page-heading department-heading">
          <div class="department-heading-title">
            <h1 class="page-title">{{ project.name }}</h1>
          </div>
        </div>
        <UAlert
          v-if="project.archived"
          color="neutral"
          variant="soft"
          title="Проект в архиве"
          description="Восстановите проект, чтобы создавать и изменять отделы, добавлять документы и запускать проверки."
          class="department-notice"
        />
        <UAlert v-if="error" color="error" variant="soft" :title="error" class="department-notice"
          ><template #actions
            ><UButton color="error" variant="soft" @click="load">Повторить</UButton></template
          ></UAlert
        >
        <UAlert
          v-if="actionError && !actionOpen && !projectRenameOpen && !projectArchiveOpen"
          color="error"
          variant="soft"
          :title="actionError"
          class="department-notice"
        />
        <section :aria-busy="busy" aria-label="Список отделов">
          <div v-if="!filtered.length && !error" class="panel department-empty">
            <UIcon name="i-lucide-folder-open" class="size-8 text-muted" />
            <h2>
              {{ 'Пока нет отделов' }}
            </h2>
            <p class="muted">
              {{ 'Создайте отдел, чтобы добавлять документы и запускать проверки.' }}
            </p>
            <UButton
              v-if="!project.archived"
              :to="`/projects/${projectId}/departments/new`"
              :disabled="project.archived"
              icon="i-lucide-plus"
              >Создать отдел</UButton
            >
          </div>
          <div v-else class="department-grid" role="list">
            <UPageCard
              v-for="item in filtered"
              :key="item.id"
              variant="outline"
              class="department-card"
              role="listitem"
              :aria-label="`Отдел ${item.name}`"
              :ui="{ wrapper: 'min-w-0 w-full', header: 'w-full', body: 'min-w-0 w-full' }"
              @click="openDepartment(item)"
            >
              <template #header>
                <div class="department-card-top">
                  <div class="department-icon">
                    <UIcon :name="`i-lucide-${item.icon}`" class="size-5" aria-hidden="true" />
                  </div>
                  <UDropdownMenu :items="departmentActions(item)" :content="{ align: 'end' }"
                    ><UButton
                      icon="i-lucide-ellipsis"
                      color="neutral"
                      variant="ghost"
                      size="sm"
                      :aria-label="`Действия отдела ${item.name}`"
                      :ui="{ base: 'size-8 min-h-0 min-w-0 p-0', leadingIcon: 'size-4' }"
                      @click.stop
                  /></UDropdownMenu>
                </div>
                <NuxtLink
                  v-if="renamingId !== item.id"
                  :to="departmentPath(item)"
                  class="department-name"
                  @click.stop
                  >{{ item.name }}</NuxtLink
                >
                <form
                  v-else
                  class="department-rename"
                  @click.stop
                  @submit.prevent="saveRename(item)"
                >
                  <UInput
                    v-model="nameDraft"
                    aria-label="Новое название отдела"
                    :maxlength="256"
                    :disabled="renameBusy"
                    autofocus
                    class="min-w-0 flex-1"
                    @keydown.escape="renamingId = null"
                  /><UButton
                    type="submit"
                    icon="i-lucide-check"
                    aria-label="Сохранить название отдела"
                    color="neutral"
                    variant="outline"
                    :disabled="!nameDraft.trim()"
                    :loading="renameBusy"
                  /><UButton
                    icon="i-lucide-x"
                    aria-label="Отменить переименование"
                    color="neutral"
                    variant="ghost"
                    :disabled="renameBusy"
                    @click="renamingId = null"
                  />
                </form>
              </template>
              <template #body
                ><div class="department-meta">
                  <UBadge color="neutral" variant="outline">ВОР · {{ item.vor_count }}</UBadge
                  ><UBadge color="neutral" variant="outline">ПД · {{ item.pdf_count }}</UBadge
                  ><UBadge color="neutral" variant="outline">Проверок · {{ item.run_count }}</UBadge
                  ><UBadge v-if="item.is_system" color="neutral" variant="soft">Системный</UBadge>
                </div></template
              >
            </UPageCard>
            <button
              v-if="!project.archived && !creating"
              type="button"
              class="department-create-card"
              role="listitem"
              aria-label="Создать новый отдел"
              @click="creating = true"
            >
              <span class="department-create-icon"><UIcon name="i-lucide-plus" /></span>
              <span>Создать новый отдел</span>
            </button>
            <form
              v-else-if="!project.archived"
              class="department-create-card department-create-form"
              role="listitem"
              @submit.prevent="createDepartment"
            >
              <UFormField label="Название отдела" class="w-full">
                <UInput
                  v-model="newDepartmentName"
                  autofocus
                  class="w-full"
                  placeholder="Введите название"
                  :maxlength="256"
                  :disabled="createBusy"
                />
              </UFormField>
              <p v-if="createError" class="text-error department-create-error" role="alert">
                {{ createError }}
              </p>
              <div class="department-create-actions">
                <UButton type="submit" :loading="createBusy" :disabled="!newDepartmentName.trim()"
                  >Создать</UButton
                >
                <UButton
                  type="button"
                  color="neutral"
                  variant="ghost"
                  :disabled="createBusy"
                  @click="cancelCreate"
                  >Отмена</UButton
                >
              </div>
            </form>
          </div>
        </section>
      </template>
      <UModal v-model:open="actionOpen" title="Удалить отдел?"
        ><template #body
          ><p class="modal-text">
            Отдел «{{ actionTarget?.name }}» будет удалён без восстановления. Можно удалить только
            пустой отдел.
          </p>
          <UFormField label="Введите название отдела" class="department-delete-field"
            ><UInput
              v-model="confirmName"
              class="w-full"
              aria-label="Название отдела для подтверждения удаления"
          /></UFormField>
          <p v-if="actionError" class="text-error mt-3" role="alert">{{ actionError }}</p>
          <div class="toolbar spread modal-footer">
            <UButton
              color="neutral"
              variant="ghost"
              :disabled="mutationBusy"
              @click="actionOpen = false"
              >Отмена</UButton
            ><UButton
              color="error"
              :loading="mutationBusy"
              :disabled="confirmName.trim() !== actionTarget?.name"
              @click="confirmAction"
              >Удалить</UButton
            >
          </div></template
        ></UModal
      >
      <UModal v-model:open="projectRenameOpen" title="Переименовать проект"
        ><template #body
          ><form @submit.prevent="saveProject">
            <UFormField label="Название проекта"
              ><UInput
                v-model="projectNameDraft"
                :maxlength="256"
                class="w-full"
                :disabled="projectBusy"
                autofocus
            /></UFormField>
            <p v-if="actionError" class="text-error mt-3" role="alert">{{ actionError }}</p>
            <div class="toolbar spread modal-footer">
              <UButton color="neutral" variant="ghost" @click="projectRenameOpen = false"
                >Отмена</UButton
              ><UButton type="submit" :disabled="!projectNameDraft.trim()" :loading="projectBusy"
                >Сохранить</UButton
              >
            </div>
          </form></template
        ></UModal
      >
      <UModal v-model:open="projectArchiveOpen" title="Архивировать проект?"
        ><template #body
          ><p class="modal-text">
            Проект «{{ project?.name }}» исчезнет из активного списка. Отделы, документы и история
            проверок сохранятся.
          </p>
          <p v-if="actionError" class="text-error" role="alert">{{ actionError }}</p>
          <div class="toolbar spread modal-footer">
            <UButton color="neutral" variant="ghost" @click="projectArchiveOpen = false"
              >Отмена</UButton
            ><UButton :loading="projectBusy" @click="archiveProject(true)">Архивировать</UButton>
          </div></template
        ></UModal
      >
    </main>
  </div>
</template>

<style scoped>
.departments {
  padding-bottom: 48px;
}
.department-heading {
  margin-top: 24px;
  align-items: flex-start;
}
.department-heading-title {
  min-width: 0;
}
.department-heading .page-title {
  overflow-wrap: anywhere;
}
.department-loading {
  display: grid;
  gap: 16px;
  margin-top: 24px;
}
.department-notice {
  margin-bottom: 20px;
}
.department-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 16px;
}
.department-create-card {
  min-height: 224px;
  display: grid;
  place-content: center;
  justify-items: center;
  gap: 12px;
  padding: 24px;
  border-width: 2px;
  border-style: dashed;
  border-color: color-mix(in srgb, var(--ui-border-accented) 70%, transparent);
  border-radius: 14px;
  color: var(--ui-text-muted);
  background: color-mix(in srgb, var(--ui-bg) 60%, transparent);
  font-weight: 600;
  transition:
    border-color 160ms ease,
    color 160ms ease,
    background 160ms ease;
}
.department-create-form {
  place-content: center stretch;
  justify-items: stretch;
  text-align: left;
  cursor: default;
}
.department-create-form:hover {
  border-color: color-mix(in srgb, var(--ui-border-accented) 70%, transparent);
  color: var(--ui-text-muted);
  background: color-mix(in srgb, var(--ui-bg) 60%, transparent);
}
.department-create-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.department-create-error {
  margin: 0;
  font-size: 13px;
}
.department-create-card:hover {
  border-color: color-mix(in srgb, var(--ui-primary) 75%, transparent);
  color: var(--ui-primary);
  background: var(--app-selected-bg);
}
.department-create-icon {
  display: grid;
  place-items: center;
  width: 44px;
  height: 44px;
  border-radius: 50%;
  background: var(--app-selected-bg);
  font-size: 20px;
}
.department-card {
  min-width: 0;
  height: 100%;
  cursor: pointer;
}
.department-card-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
  gap: 12px;
}
.department-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  border-radius: 8px;
  color: var(--app-selected-text);
  background: var(--app-selected-bg);
}
.department-name {
  display: block;
  color: var(--ui-text-highlighted);
  font-size: 17px;
  font-weight: 600;
  overflow-wrap: anywhere;
}
.department-name:hover {
  text-decoration: underline;
}
.department-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.modal-text {
  margin: 0 0 20px;
  line-height: 1.5;
}
.department-delete-field {
  margin-bottom: 24px;
}
.department-rename {
  display: flex;
  align-items: center;
  gap: 4px;
}
.department-empty {
  display: grid;
  justify-items: center;
  gap: 12px;
  padding: 40px 24px;
  text-align: center;
}
.department-empty h2,
.department-empty p {
  margin: 0;
}
@media (min-width: 700px) {
  .department-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (min-width: 1100px) {
  .department-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}
@media (max-width: 699px) {
  .department-heading {
    flex-direction: column;
    gap: 20px;
  }
}
</style>

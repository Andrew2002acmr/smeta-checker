<script setup lang="ts">
const backend = useBackend(),
  route = useRoute()
const projectId = computed(() => String(route.params.projectId || ''))
const projectName = ref(''),
  name = ref(''),
  busy = ref(false),
  error = ref('')
onMounted(async () => {
  try {
    await backend.connect()
    const project = await backend.project(projectId.value)
    projectName.value = project.name
    if (project.archived) error.value = 'Сначала восстановите проект из архива.'
  } catch (e) {
    error.value = readableError(e)
  }
})
async function create() {
  if (!name.value.trim() || busy.value) {
    if (!name.value.trim()) error.value = 'Введите название отдела.'
    return
  }
  busy.value = true
  error.value = ''
  try {
    const department = await backend.createDepartment(projectId.value, name.value.trim())
    await navigateTo(`/projects/${projectId.value}/departments/${department.id}`)
  } catch (e) {
    error.value = readableError(e)
    busy.value = false
  }
}
</script>

<template>
  <div>
    <WorkspaceHeader active="projects" />
    <main id="main-content" tabindex="-1" class="shell department-create">
      <nav aria-label="Навигация">
        <UButton
          :to="`/projects/${projectId}`"
          variant="link"
          color="neutral"
          icon="i-lucide-arrow-left"
          >Отделы проекта</UButton
        >
      </nav>
      <div class="page-heading">
        <div>
          <h1 class="page-title">Новый отдел</h1>
          <p class="muted">
            {{ projectName }} · Ведомости, ПД и проверки будут храниться внутри отдела.
          </p>
        </div>
      </div>
      <UAlert v-if="error" color="error" variant="soft" :title="error" role="alert" class="mb-5" />
      <section class="panel department-create-form" aria-label="Создание отдела">
        <form @submit.prevent="create">
          <UFormField label="Название отдела" name="name"
            ><div class="department-create-row">
              <UInput
                v-model="name"
                aria-label="Название отдела"
                placeholder="Например, Отдел промышленной безопасности"
                :maxlength="256"
                required
                :disabled="busy"
                class="flex-1 min-w-0"
              /><UButton type="submit" :loading="busy">Создать отдел</UButton>
            </div></UFormField
          >
        </form>
      </section>
    </main>
  </div>
</template>

<style scoped>
.department-create {
  max-width: 880px;
}
.department-create .page-heading {
  margin-top: 24px;
}
.department-create-form {
  padding: 24px;
}
.department-create-row {
  display: flex;
  gap: 12px;
}
@media (max-width: 699px) {
  .department-create-row {
    flex-direction: column;
  }
}
</style>

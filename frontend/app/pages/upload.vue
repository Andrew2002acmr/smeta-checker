<script setup lang="ts">
const backend = useBackend()
const route = useRoute()
const name = ref('')
const busy = ref(false)
const error = ref('')

async function create() {
  const value = name.value.trim()
  if (!value || busy.value) {
    if (!value) error.value = 'Введите название проекта.'
    return
  }
  busy.value = true
  error.value = ''
  try {
    await backend.connect()
    const project = await backend.createProject(value)
    await navigateTo(`/projects/${project.id}`)
  } catch (e) {
    error.value = readableError(e)
    busy.value = false
  }
}
</script>

<template>
  <div>
    <WorkspaceHeader active="create" />
    <main id="main-content" tabindex="-1" class="shell upload">
      <div class="page-heading">
        <div>
          <h1 class="page-title">Новый проект</h1>
          <p class="muted">
            Название проекта задаёте вы. Затем выберите отдел и добавьте ведомости и ПД.
          </p>
        </div>
      </div>

      <UAlert
        v-if="error"
        color="error"
        variant="soft"
        :title="error"
        role="alert"
        class="upload-alert"
      />

      <section class="panel upload-card" aria-label="Создание проекта">
        <form @submit.prevent="create">
          <label class="upload-label" for="project-name-input"
            >Название проекта — например, адрес объекта или шифр комплекта</label
          >
          <div class="upload-row">
            <UInput
              id="project-name-input"
              v-model="name"
              aria-label="Название проекта"
              placeholder="Например, ЖК Северный, корп. 2 — ВОР и ПД"
              required
              :disabled="busy"
              class="upload-name"
            />
            <UButton
              type="submit"
              color="primary"
              icon="i-lucide-plus"
              :loading="busy"
              data-testid="create-project"
            >
              Создать проект
            </UButton>
          </div>
        </form>
      </section>
    </main>
  </div>
</template>

<style scoped>
.upload {
  max-width: 880px;
}
.upload-alert {
  margin-bottom: 16px;
}
.upload-card {
  padding: 24px;
}
.upload-label {
  display: block;
  margin-bottom: 8px;
  color: var(--ui-text-muted);
  font-size: 14px;
}
.upload-row {
  display: flex;
  gap: 12px;
}
.upload-name {
  flex: 1;
}
@media (max-width: 700px) {
  .upload-row {
    flex-direction: column;
  }
}
</style>

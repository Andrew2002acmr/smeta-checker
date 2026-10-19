<script setup lang="ts">
import type { ProjectDto, RunDto } from '~/generated/types.gen'
import { runLabel } from '~/utils/presentation'
const backend = useBackend(),
  route = useRoute(),
  router = useRouter(),
  projects = ref<ProjectDto[]>([]),
  runs = ref<RunDto[]>([]),
  selected = ref(''),
  offset = ref(0),
  error = ref(''),
  busy = ref(true)
let epoch = 0
let active = true
let timer: ReturnType<typeof setTimeout> | undefined
function changePage(delta: number) {
  offset.value = Math.max(0, offset.value + delta)
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
  error.value = ''
  try {
    await backend.connect()
    projects.value = await backend.projects()
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
  <main class="shell">
    <header class="topbar">
      <NuxtLink to="/" class="brand">КД<span>ФП</span> · Проверка ВОР</NuxtLink
      ><UButton to="/upload">Новая проверка</UButton>
    </header>
    <h1 class="page-title">Проверки</h1>
    <p class="muted">Сохранённые запуски с исходными версиями документов</p>
    <div v-if="error" class="error-box" role="alert">
      {{ error }}
      <UButton variant="link" @click="selected ? loadRuns() : initialize()">Повторить</UButton>
    </div>
    <div v-if="projects.length" class="toolbar" style="margin: 24px 0">
      <USelect
        v-model="selected"
        :items="projects.map((p) => ({ label: p.name, value: p.id }))"
        aria-label="Проект истории"
      /><UButton :to="`/upload?project=${selected}`" variant="outline">Дополнить комплект</UButton>
    </div>
    <p v-if="busy" class="empty" role="status">Открываем историю…</p>
    <div v-else-if="runs.length" class="panel" style="overflow: auto">
      <table class="results">
        <thead>
          <tr>
            <th>Ведомость</th>
            <th>Создано</th>
            <th>Состояние</th>
            <th>Позиций обработано</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in runs" :key="r.run_id">
            <td>
              {{ r.vor_filename }}
              <p class="muted">{{ r.run_id.slice(0, 8) }}</p>
            </td>
            <td>{{ r.created_at ? new Date(r.created_at).toLocaleString('ru-RU') : '—' }}</td>
            <td>{{ runLabel(r.status) }}</td>
            <td>{{ r.done + r.failed }} / {{ r.total ?? '…' }}</td>
            <td>
              <UButton variant="link" :to="`/audits/${r.run_id}`">Открыть проверку →</UButton>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-else class="empty">Сохранённых проверок пока нет. Загрузите ВОР и проектные PDF.</p>
    <div v-if="offset || runs.length === 50" class="toolbar" style="margin-top: 20px">
      <UButton :disabled="offset === 0" @click="changePage(-50)">Назад</UButton
      ><UButton :disabled="runs.length < 50" @click="changePage(50)">Далее</UButton>
    </div>
  </main>
</template>

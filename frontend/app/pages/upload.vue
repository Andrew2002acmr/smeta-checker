<script setup lang="ts">
import type { VersionDto, PreflightResponse } from '~/generated/types.gen'
const backend = useBackend(),
  route = useRoute(),
  router = useRouter(),
  name = ref(''),
  projectId = ref(typeof route.query.project === 'string' ? route.query.project : ''),
  files = ref<File[]>([]),
  versions = ref<VersionDto[]>([]),
  selected = ref<string[]>([]),
  preflights = ref<{ filename: string; value: PreflightResponse }[]>([]),
  busy = ref(false),
  error = ref(''),
  message = ref(''),
  acknowledged = ref(false),
  input = ref<HTMLInputElement>(),
  started = ref<{ id: string; filename: string; versionId: string }[]>([])
const selectedDocs = computed(() => versions.value.filter((v) => selected.value.includes(v.id)))
const vors = computed(() => selectedDocs.value.filter((v) => v.kind === 'vor'))
const pdfs = computed(() => selectedDocs.value.filter((v) => v.kind === 'pdf'))
const critical = computed(() => preflights.value.some((p) => p.value.has_critical_error))
const payload = (id: string) => ({
  project_id: projectId.value,
  vor_version_id: id,
  pdf_version_ids: pdfs.value.map((v) => v.id),
})
function addFiles(incoming: File[]) {
  if (busy.value) return
  error.value = ''
  for (const file of incoming) {
    if (!/\.(xlsx|csv|pdf)$/i.test(file.name)) {
      error.value = 'Поддерживаются ВОР XLSX/CSV и проектные PDF.'
      continue
    }
    if (file.size > 100 * 1024 * 1024) {
      error.value = `${file.name}: файл больше 100 МБ.`
      continue
    }
    if (files.value.length >= 50) {
      error.value = 'За один раз можно добавить до 50 файлов.'
      break
    }
    if (!files.value.some((f) => f.name === file.name && f.size === file.size))
      files.value.push(file)
  }
}
function picked(event: Event) {
  const target = event.target as HTMLInputElement
  addFiles([...(target.files || [])])
  target.value = ''
}
function toggleVersion(version: VersionDto) {
  if (selected.value.includes(version.id))
    selected.value = selected.value.filter((id) => id !== version.id)
  else {
    const siblings = new Set(
      versions.value.filter((v) => v.document_id === version.document_id).map((v) => v.id),
    )
    selected.value = [...selected.value.filter((id) => !siblings.has(id)), version.id]
  }
}
async function refreshVersions() {
  versions.value = await backend.documents(projectId.value)
  const latest = new Set<string>()
  selected.value = versions.value
    .filter((v) => {
      if (latest.has(v.document_id)) return false
      latest.add(v.document_id)
      return true
    })
    .map((v) => v.id)
}
async function upload() {
  if (busy.value || !files.value.length || !name.value.trim()) return
  busy.value = true
  error.value = ''
  preflights.value = []
  started.value = []
  try {
    if (!projectId.value) {
      const project = await backend.createProject(name.value.trim())
      projectId.value = project.id
      await router.replace({ query: { project: project.id } })
    }
    for (const file of files.value) {
      message.value = 'Загружаем ' + file.name
      const form = new FormData()
      form.append('project', name.value.trim())
      form.append('project_id', projectId.value)
      form.append('kind', /\.pdf$/i.test(file.name) ? 'pdf' : 'vor')
      form.append('file', file)
      await backend.upload(form)
      files.value = files.value.filter((f) => f !== file)
    }
    await refreshVersions()
    message.value = 'Документы сохранены. Проверьте выбранные версии и привязки.'
  } catch (e) {
    error.value = readableError(e)
    if (projectId.value) await refreshVersions().catch(() => {})
  } finally {
    busy.value = false
  }
}
async function checkBindings() {
  busy.value = true
  error.value = ''
  preflights.value = []
  acknowledged.value = false
  try {
    if (pdfs.value.length + 1 > 32)
      throw Error('Для одного аудита можно выбрать ВОР и не более 31 PDF.')
    for (const vor of vors.value)
      preflights.value.push({
        filename: vor.filename,
        value: await backend.preflight(payload(vor.id)),
      })
  } catch (e) {
    error.value = readableError(e)
    preflights.value = []
  } finally {
    busy.value = false
  }
}
async function start() {
  if (busy.value || !preflights.value.length || (critical.value && !acknowledged.value)) return
  busy.value = true
  error.value = ''
  try {
    for (const vor of vors.value) {
      if (started.value.some((r) => r.versionId === vor.id)) continue
      const run = await backend.start(payload(vor.id))
      started.value.push({ id: run.run_id, filename: vor.filename, versionId: vor.id })
    }
    if (started.value.length === 1) await navigateTo('/audits/' + started.value[0]!.id)
    else await navigateTo('/history?project=' + projectId.value)
  } catch (e) {
    error.value = readableError(e)
  } finally {
    busy.value = false
  }
}
watch(
  selected,
  () => {
    preflights.value = []
    acknowledged.value = false
    started.value = []
  },
  { deep: true },
)
onMounted(async () => {
  try {
    await backend.connect()
    if (projectId.value) {
      name.value = (await backend.projects()).find((p) => p.id === projectId.value)?.name || ''
      await refreshVersions()
    }
  } catch (e) {
    error.value = readableError(e)
  }
})
</script>
<template>
  <main class="shell" style="max-width: 1100px">
    <header class="topbar">
      <NuxtLink to="/" class="brand">КД<span>ФП</span> · Проверка ВОР</NuxtLink
      ><UButton to="/history" variant="ghost" color="neutral">История</UButton>
    </header>
    <h1 class="page-title">Проверьте ведомость перед передачей</h1>
    <p class="muted">
      Добавьте ВОР и проектные PDF. Сначала проверьте ссылки, затем запустите аудит.
    </p>
    <div v-if="error" class="error-box" role="alert">{{ error }}</div>
    <label style="display: block; margin: 24px 0"
      >Название комплекта<UInput
        v-model="name"
        :disabled="!!projectId || busy"
        placeholder="Например: мост, этап 2"
        class="w-full"
        aria-label="Название комплекта"
    /></label>
    <section
      class="panel drop-zone"
      @dragover.prevent
      @drop.prevent="addFiles([...($event.dataTransfer?.files || [])])"
    >
      <h2>Добавьте документы</h2>
      <p>Ведомости XLSX/CSV и проектные PDF, до 100 МБ на файл</p>
      <UButton :disabled="busy" @click="input?.click()">Выбрать файлы</UButton
      ><input
        ref="input"
        type="file"
        multiple
        accept=".xlsx,.csv,.pdf"
        aria-label="Добавить файлы"
        style="display: none"
        @change="picked"
      />
    </section>
    <ul style="margin: 16px 0">
      <li v-for="file in files" :key="file.name" class="toolbar spread">
        <span>{{ file.name }}</span
        ><UButton :disabled="busy" variant="link" @click="files = files.filter((f) => f !== file)"
          >Убрать</UButton
        >
      </li>
    </ul>
    <UButton :loading="busy" :disabled="!files.length || !name.trim() || busy" @click="upload"
      >Сохранить документы</UButton
    >
    <p v-if="message" class="muted" role="status" style="margin: 12px 0">{{ message }}</p>
    <section v-if="versions.length" style="margin-top: 28px">
      <h2>Версии для проверки</h2>
      <p class="muted">
        Каждая выбранная ВОР создаёт отдельный сохранённый аудит с выбранными PDF.
      </p>
      <div class="panel" style="margin: 12px 0; padding: 16px">
        <label v-for="v in versions" :key="v.id" class="toolbar" style="margin: 8px 0"
          ><input
            type="checkbox"
            :checked="selected.includes(v.id)"
            :disabled="busy"
            @change="toggleVersion(v)"
          /><span>{{ v.filename }} · версия {{ v.number }}</span
          ><small class="muted">{{ v.sha256.slice(0, 10) }}</small></label
        >
      </div>
      <UButton :disabled="busy || !vors.length" :loading="busy" @click="checkBindings"
        >Проверить привязки файлов</UButton
      >
    </section>
    <section
      v-for="p in preflights"
      :key="p.filename"
      class="panel"
      style="padding: 16px; margin: 16px 0"
    >
      <h2>{{ p.filename }}</h2>
      <table class="results">
        <thead>
          <tr>
            <th>Ссылка ВОР</th>
            <th>Привязка</th>
            <th>PDF</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(r, i) in p.value.rows" :key="i">
            <td>{{ r.refs }}</td>
            <td>{{ r.status }}</td>
            <td>{{ r.pdf }}</td>
          </tr>
        </tbody>
      </table>
    </section>
    <label v-if="critical" class="toolbar" style="margin: 16px 0"
      ><input v-model="acknowledged" type="checkbox" />В привязках есть проблемы. Выполнить
      доступные проверки; остальные позиции потребуют ручной сверки.</label
    >
    <UButton
      v-if="preflights.length"
      :disabled="busy || (critical && !acknowledged)"
      :loading="busy"
      @click="start"
      >Запустить проверку</UButton
    >
    <p v-for="r in started" :key="r.id" style="margin: 12px 0">
      <NuxtLink :to="'/audits/' + r.id">{{ r.filename }} — открыть запущенную проверку →</NuxtLink>
    </p>
  </main>
</template>

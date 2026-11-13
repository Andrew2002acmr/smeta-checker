<script setup lang="ts">
const props = defineProps<{ projectId: string }>()
const backend = useBackend()
const open = ref(false)
const loading = ref(false)
const launching = ref(false)
const error = ref('')
const normativeProfile = ref<'roads' | 'electrical'>('roads')
const codexSubagents = ref(true)
const documents = ref<{ versionId: string; title: string }[]>([])
const launches = ref<Record<string, { runId?: string; error?: string }>>({})
const started = computed(() => Object.values(launches.value).filter((item) => item.runId).length)

async function loadDocuments() {
  if (loading.value) return
  loading.value = true
  error.value = ''
  try {
    documents.value = (await backend.catalog(props.projectId)).flatMap((document) =>
      document.kind === 'vor' && !document.archived && document.current_version_id
        ? [{ versionId: document.current_version_id, title: document.title }]
        : [],
    )
  } catch (cause) {
    error.value = readableError(cause)
  } finally {
    loading.value = false
  }
}

async function launch() {
  if (launching.value || loading.value) return
  launching.value = true
  try {
    for (const document of documents.value) {
      const previous = launches.value[document.versionId]
      if (previous?.runId) continue
      try {
        const run = await backend.start({
          project_id: props.projectId,
          vor_version_id: document.versionId,
          normative_profile: normativeProfile.value,
          mode: 'codex',
          codex_subagents: codexSubagents.value,
        })
        launches.value[document.versionId] = { runId: run.run_id }
      } catch (cause) {
        launches.value[document.versionId] = { error: readableError(cause) }
      }
    }
  } finally {
    launching.value = false
  }
}

watch(open, (next) => {
  if (next && !documents.value.length) void loadDocuments()
})
</script>

<template>
  <div class="mb-4">
    <UModal v-model:open="open" title="Проверка комплекта ВОР" :dismissible="!launching">
      <UButton label="Проверить весь комплект" variant="outline" icon="i-lucide-list-checks" />
      <template #body>
        <div class="grid gap-4">
          <p>Codex Agent (Experimental) запустит отдельную проверку каждой действующей ВОР.</p>
          <p v-if="loading" role="status">Загружаем ведомости…</p>
          <div v-else-if="error" role="alert">
            <p class="text-error">{{ error }}</p>
            <UButton label="Повторить" variant="link" @click="loadDocuments" />
          </div>
          <p v-else-if="!documents.length" class="muted">В проекте нет действующих ВОР.</p>
          <template v-else>
            <label class="flex flex-wrap items-center gap-2">
              Нормативный профиль
              <select v-model="normativeProfile" :disabled="launching || started > 0">
                <option value="roads">Дороги · ГЭСН27/47</option>
                <option value="electrical">Электрика · ГЭСНм08</option>
              </select>
            </label>
            <label class="flex items-center gap-2">
              <input
                v-model="codexSubagents"
                type="checkbox"
                :disabled="launching || started > 0"
              />
              С субагентами
            </label>
            <ul class="grid max-h-64 gap-2 overflow-auto">
              <li v-for="document in documents" :key="document.versionId">
                <strong>{{ document.title }}</strong>
                <template v-if="launches[document.versionId]">
                  <UButton
                    v-if="launches[document.versionId]?.runId"
                    :to="`/audits/${launches[document.versionId]?.runId}`"
                    variant="link"
                    size="sm"
                    label="Открыть проверку"
                  />
                  <p v-else class="text-error">{{ launches[document.versionId]?.error }}</p>
                </template>
              </li>
            </ul>
            <p v-if="launching || started" role="status">
              Запущено {{ started }} из {{ documents.length }}
            </p>
            <UButton
              v-if="started < documents.length"
              :label="
                started ? 'Повторить для остальных' : `Запустить проверку · ${documents.length}`
              "
              :loading="launching"
              @click="launch"
            />
          </template>
        </div>
      </template>
    </UModal>
  </div>
</template>

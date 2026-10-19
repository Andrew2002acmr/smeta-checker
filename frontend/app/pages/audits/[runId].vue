<script setup lang="ts">
import type { RunDto, SummaryDto, RowsDto } from '~/generated/types.gen'
import { queueColor, queueLabel, quantityWithUnit, runLabel } from '~/utils/presentation'
const route = useRoute(),
  router = useRouter(),
  backend = useBackend(),
  runId = computed(() => String(route.params.runId)),
  run = ref<RunDto | null>(null),
  summary = ref<SummaryDto | null>(null),
  results = ref<RowsDto | null>(null),
  error = ref(''),
  busy = ref(true),
  focus = ref('all'),
  query = ref(''),
  offset = ref(0)
const position = computed(() =>
  typeof route.query.position === 'string' && /^\d+$/.test(route.query.position)
    ? Number(route.query.position)
    : null,
)
const queues = [
  { id: 'fix', label: 'Исправить' },
  { id: 'review', label: 'Проверить вручную' },
  { id: 'missing', label: 'Проверка не выполнена' },
  { id: 'all', label: 'Все позиции' },
]
let active = true,
  timer: ReturnType<typeof setTimeout> | undefined,
  searchTimer: ReturnType<typeof setTimeout> | undefined,
  epoch = 0
async function loadRows() {
  const current = ++epoch
  busy.value = true
  try {
    const data = await backend.rows(runId.value, {
      focus: focus.value,
      q: query.value,
      offset: offset.value,
    })
    if (active && current === epoch) results.value = data
  } catch (e) {
    if (active && current === epoch) error.value = readableError(e)
  } finally {
    if (active && current === epoch) busy.value = false
  }
}
function changePage(delta: number) {
  offset.value = Math.max(0, offset.value + delta)
  loadRows()
}
async function poll() {
  clearTimeout(timer)
  error.value = ''
  try {
    const value = await backend.run(runId.value)
    if (!active) return
    run.value = value
    if (value.status === 'running') {
      timer = setTimeout(poll, 1500)
      return
    }
    summary.value = await backend.summary(runId.value)
    await loadRows()
  } catch (e) {
    if (active) {
      error.value = readableError(e) + '. Повторяем подключение…'
      timer = setTimeout(poll, 3000)
    }
  } finally {
    busy.value = false
  }
}
async function download(kind: 'excel' | 'word') {
  try {
    await backend.download(
      `${backend.audit(runId.value)}/reports/${kind}`,
      `audit-${runId.value}.${kind === 'excel' ? 'xlsx' : 'docx'}`,
    )
  } catch (e) {
    error.value = readableError(e)
  }
}
watch(focus, () => {
  offset.value = 0
  loadRows()
})
watch(query, () => {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    offset.value = 0
    loadRows()
  }, 250)
})
onMounted(async () => {
  try {
    await backend.connect()
    await poll()
  } catch (e) {
    error.value = readableError(e)
  }
})
onBeforeUnmount(() => {
  active = false
  epoch++
  clearTimeout(timer)
  clearTimeout(searchTimer)
})
</script>
<template>
  <main class="shell">
    <header class="topbar">
      <NuxtLink to="/" class="brand">КД<span>ФП</span> · Проверка ВОР</NuxtLink>
      <div class="toolbar">
        <UButton :to="'/history?project=' + (run?.project_id || '')" variant="ghost" color="neutral"
          >История</UButton
        ><UButton to="/upload">Новая проверка</UButton>
      </div>
    </header>
    <h1 class="page-title">
      {{ summary?.project.name || run?.vor_filename || 'Проверка документов' }}
    </h1>
    <p class="muted">
      {{ run?.vor_filename }} · {{ runId.slice(0, 8) }} · {{ runLabel(run?.status || '') }}
    </p>
    <div v-if="error" class="error-box" role="alert">
      {{ error }} <UButton variant="link" @click="poll">Повторить</UButton>
    </div>
    <section
      v-if="run?.status === 'running'"
      class="panel"
      style="padding: 24px; margin: 24px 0"
      role="status"
    >
      <h2>
        {{
          {
            preparing: 'Подготавливаем документы',
            auditing: 'Проверяем позиции',
            finalizing: 'Собираем отчёты',
            done: 'Завершено',
            failed: 'Технический сбой',
          }[run.stage]
        }}
      </h2>
      <p>
        Готово {{ run.done + run.failed }}{{ run.total === null ? '' : ' из ' + run.total }} позиций
      </p>
      <UProgress
        :model-value="
          run.stage === 'preparing'
            ? run.preparation_total
              ? Math.floor(((run.preparation_done ?? 0) / run.preparation_total) * 100)
              : null
            : run.stage === 'finalizing' || !run.total
              ? null
              : Math.floor(((run.done + run.failed) / run.total) * 100)
        "
        :status="run.stage === 'auditing' || !!run.preparation_total"
        aria-label="Прогресс проверки"
        style="margin: 16px 0"
      />
      <p v-if="run.stage === 'preparing'" class="muted">
        Извлекаем текст и таблицы PDF.
        <span v-if="run.preparation_total"
          >Страниц текущего PDF: {{ run.preparation_done }} из {{ run.preparation_total }}.</span
        >
        Счётчик позиций начнёт расти после подготовки документов.
      </p>
      <p v-else-if="run.stage === 'auditing'" class="muted">
        Сейчас проверяется: {{ run.running }}. В очереди: {{ run.queued }}.
      </p>
      <p class="muted">Можно закрыть страницу и вернуться к проверке через историю.</p>
    </section>
    <p v-if="run?.status === 'failed'" class="error-box">
      Запуск завершился с техническим сбоем. Сохранённые результаты доступны ниже; непроверенные
      позиции не подтверждены.
    </p>
    <template v-if="summary"
      ><div class="queues">
        <button
          v-for="item in queues"
          :key="item.id"
          class="panel queue"
          :class="{ active: focus === item.id }"
          @click="focus = item.id"
        >
          <strong>{{
            item.id === 'all'
              ? Object.values(summary.queues).reduce((a, b) => a + b, 0)
              : summary.queues[item.id] || 0
          }}</strong
          ><small>{{ item.label }}</small>
        </button>
      </div>
      <div class="toolbar spread" style="margin: 20px 0">
        <UInput v-model="query" placeholder="Работа, номер или ячейка" aria-label="Поиск позиций" />
        <div class="toolbar">
          <UButton variant="outline" @click="download('excel')">Скачать Excel</UButton
          ><UButton variant="outline" @click="download('word')">Скачать Word</UButton>
        </div>
      </div>
      <div class="panel" style="overflow: auto" :aria-busy="busy">
        <table class="results">
          <thead>
            <tr>
              <th>Где</th>
              <th>Работа</th>
              <th>Что проверить</th>
              <th>Значения</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="r in results?.rows"
              :key="r.id"
              @click="router.replace({ query: { position: r.position_index } })"
            >
              <td>
                {{ r.sheet || 'CSV' }}
                <p class="muted">{{ r.cells.quantity || 'Позиция ' + r.number }}</p>
              </td>
              <td>{{ r.number }} · {{ r.name }}</td>
              <td>
                <UBadge :color="queueColor(r.queue)" variant="subtle">{{
                  queueLabel(r.queue)
                }}</UBadge>
                <p style="margin-top: 6px">{{ r.problem }}</p>
              </td>
              <td class="numbers">
                ВОР {{ quantityWithUnit(r.declared_quantity, r.unit) }}
                <p class="muted">ПД {{ quantityWithUnit(r.found_quantity, r.found_unit) }}</p>
              </td>
              <td>
                <UButton
                  variant="link"
                  @click.stop="router.replace({ query: { position: r.position_index } })"
                  >Открыть →</UButton
                >
              </td>
            </tr>
          </tbody>
        </table>
        <p v-if="!results?.rows.length && !busy" class="empty">По этому фильтру позиций нет.</p>
      </div>
      <div v-if="results && (results.total > 100 || offset)" class="toolbar" style="margin: 20px 0">
        <UButton :disabled="offset === 0" @click="changePage(-100)">Назад</UButton
        ><span
          >{{ offset + 1 }}–{{ Math.min(offset + 100, results.total) }} из {{ results.total }}</span
        ><UButton :disabled="offset + 100 >= results.total" @click="changePage(100)">Далее</UButton>
      </div>
      <details class="details">
        <summary>Исходные версии документов</summary>
        <p v-for="doc in summary.documents" :key="doc.id">
          {{ doc.filename }} · версия {{ doc.number }} · {{ doc.sha256.slice(0, 12) }}
          <UButton
            variant="link"
            @click="
              backend
                .download(backend.contentUrl(doc.id) + '?download=true', doc.filename)
                .catch((e) => (error = readableError(e)))
            "
            >Скачать</UButton
          >
        </p>
      </details>
    </template>
    <AuditWorkspace
      v-if="position !== null && summary"
      :key="runId + ':' + position"
      :run-id="runId"
      :position-index="position"
      :documents="summary.documents"
      @close="router.replace({ query: {} })"
    />
  </main>
</template>

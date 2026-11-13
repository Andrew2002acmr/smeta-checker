<script setup lang="ts">
import { codexStatuses } from '~/utils/codexPresentation'

const props = defineProps<{ metrics: Record<string, unknown>; processed: number }>()
const numeric = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 })
const stageTimes = [
  { key: 'queue_duration_ms', label: 'Ожидание в очереди' },
  { key: 'preparation_duration_ms', label: 'Подготовка документов' },
  { key: 'document_search_duration_ms', label: 'Поиск по документам' },
  { key: 'ocr_duration_ms', label: 'Распознавание' },
  { key: 'normative_search_duration_ms', label: 'Поиск по нормативам' },
  { key: 'calculation_duration_ms', label: 'Расчёты' },
  { key: 'validation_duration_ms', label: 'Техническая валидация' },
  { key: 'persistence_duration_ms', label: 'Запись в PostgreSQL' },
]
const tokens = computed(() => dictionary(props.metrics.main_agent_tokens ?? props.metrics.tokens))
const statuses = computed(() => dictionary(props.metrics.overall_statuses))
const quota = computed(() => {
  const snapshot = dictionary(props.metrics.chatgpt_quota)
  const codex = dictionary(dictionary(snapshot.by_bucket).codex)
  const first = dictionary(dictionary(dictionary(snapshot.before).buckets).codex)
  const week = Object.entries(dictionary(first.windows)).find(
    ([, window]) => dictionary(window).windowDurationMins === 10080,
  )?.[0]
  const window = week ? dictionary(dictionary(codex.windows)[week]) : {}
  return {
    delta: window.account_delta_percentage_points,
    credits: codex.account_credit_balance_decrease,
  }
})

function dictionary(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}
function number(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) ? numeric.format(value) : '—'
}
function duration(value: unknown) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  const seconds = value / 1000
  if (seconds < 60) return `${number(seconds)} с`
  return `${number(Math.floor(seconds / 60))} мин ${number(Math.floor(seconds % 60))} с`
}
</script>

<template>
  <section class="panel mb-4 grid gap-3" aria-label="Производительность">
    <h2 class="font-semibold">Производительность</h2>
    <dl class="grid grid-cols-2 gap-3 md:grid-cols-4">
      <div>
        <dt class="muted">Общее время</dt>
        <dd>{{ duration(metrics.total_duration_ms) }}</dd>
      </div>
      <div>
        <dt class="muted">Обработано позиций</dt>
        <dd>{{ number(metrics.processed_positions ?? processed) }}</dd>
      </div>
      <div>
        <dt class="muted">Позиций в минуту</dt>
        <dd>{{ number(metrics.positions_per_minute) }}</dd>
      </div>
      <div>
        <dt class="muted">Заключений в минуту</dt>
        <dd>{{ number(metrics.conclusions_per_minute) }}</dd>
      </div>
      <div>
        <dt class="muted">Работа Codex</dt>
        <dd>{{ duration(metrics.agent_execution_duration_ms) }}</dd>
      </div>
      <div>
        <dt class="muted">Запущено субагентов</dt>
        <dd>{{ number(metrics.subagents_started) }}</dd>
      </div>
      <div>
        <dt class="muted">Модель</dt>
        <dd>{{ metrics.model || '—' }}</dd>
      </div>
      <div>
        <dt class="muted">Токены главного агента, вход / выход</dt>
        <dd>
          {{ number(metrics.input_tokens ?? tokens.input_tokens) }} /
          {{ number(metrics.output_tokens ?? tokens.output_tokens) }}
        </dd>
      </div>
    </dl>
    <ul v-if="Object.keys(statuses).length" class="flex flex-wrap gap-2">
      <li v-for="(status, key) in codexStatuses" :key="key">
        <UBadge :color="status.color" variant="soft">
          {{ status.label }} · {{ number(statuses[key]) }}
        </UBadge>
      </li>
    </ul>
    <details class="border-t border-default pt-3">
      <summary class="cursor-pointer">Длительность этапов и расход ресурсов</summary>
      <dl class="mt-3 grid grid-cols-2 gap-3 md:grid-cols-4">
        <div v-for="item in stageTimes" :key="item.key">
          <dt class="muted">{{ item.label }}</dt>
          <dd>{{ duration(metrics[item.key]) }}</dd>
        </div>
        <div>
          <dt class="muted">Среднее по ведомости на позицию</dt>
          <dd>{{ duration(metrics.mean_position_duration_ms ?? metrics.mean_position_ms) }}</dd>
        </div>
        <div>
          <dt class="muted">Обращения к локальным инструментам</dt>
          <dd>{{ number(metrics.tool_calls) }}</dd>
        </div>
        <div>
          <dt class="muted">Ответы модели по журналу</dt>
          <dd>{{ number(metrics.model_response_count) }}</dd>
        </div>
        <div>
          <dt class="muted">Изменение недельной квоты аккаунта</dt>
          <dd>{{ typeof quota.delta === 'number' ? `${number(quota.delta)} п. п.` : '—' }}</dd>
        </div>
        <div>
          <dt class="muted">Уменьшение баланса кредитов</dt>
          <dd>{{ typeof quota.credits === 'string' ? number(Number(quota.credits)) : '—' }}</dd>
        </div>
      </dl>
    </details>
  </section>
</template>

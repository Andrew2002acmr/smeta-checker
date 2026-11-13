<script setup lang="ts">
import { z } from 'zod'
import type { RunDto } from '~/generated/types.gen'

const props = defineProps<{ run: RunDto }>()
const backend = useBackend()
const open = ref(false)
const loading = ref(false)
const comparing = ref(false)
const error = ref('')
const candidates = ref<RunDto[]>([])
const otherId = ref('')
const fields = [
  { key: 'processed_positions', label: 'Обработано позиций' },
  { key: 'confirmed', label: 'Подтверждено' },
  { key: 'mismatch', label: 'Доказано расхождений' },
  { key: 'needs_review', label: 'Требуется ручная проверка' },
  { key: 'not_checked', label: 'Не проверено' },
  { key: 'confirmed_calculations', label: 'Подтверждено расчётов' },
  { key: 'checked_normative_conditions', label: 'Проверено нормативных условий' },
  { key: 'verified_sample_quality', label: 'Корректность проверенной выборки' },
  { key: 'total_duration_ms', label: 'Время выполнения, мс' },
  { key: 'positions_per_minute', label: 'Позиций в минуту' },
  { key: 'input_tokens', label: 'Токены ввода' },
  { key: 'output_tokens', label: 'Токены вывода' },
  { key: 'monetary_cost', label: 'Денежные расходы' },
] as const
const metrics = z.object({
  run_id: z.string(),
  mode: z.enum(['pipeline', 'codex']),
  subagents: z.boolean().nullable(),
  processed_positions: z.number(),
  confirmed: z.number().nullable(),
  mismatch: z.number().nullable(),
  needs_review: z.number().nullable(),
  not_checked: z.number().nullable(),
  confirmed_calculations: z.number().nullable(),
  checked_normative_conditions: z.number().nullable(),
  verified_sample_quality: z.number().nullable(),
  total_duration_ms: z.number().nullable(),
  positions_per_minute: z.number().nullable(),
  input_tokens: z.number().nullable(),
  output_tokens: z.number().nullable(),
  monetary_cost: z.number().nullable(),
})
const schema = z.object({
  normative_document_count: z.number(),
  runs: z.tuple([metrics, metrics]),
})
const comparison = ref<z.infer<typeof schema> | null>(null)
const numeric = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 })

function displayValue(value: number | null) {
  return value === null ? 'Нет данных' : numeric.format(value)
}

function runName(mode?: string, subagents?: boolean | null) {
  if (mode !== 'codex') return 'Стандартный pipeline'
  return subagents === false ? 'Codex без субагентов' : 'Codex с субагентами'
}

async function loadCandidates() {
  loading.value = true
  error.value = ''
  try {
    candidates.value = (
      await backend.runs(props.run.project_id, 0, props.run.department_id || undefined)
    ).filter(
      (run) =>
        run.status === 'done' &&
        run.run_id !== props.run.run_id &&
        run.vor_version_id === props.run.vor_version_id,
    )
  } catch (cause) {
    error.value = readableError(cause)
  } finally {
    loading.value = false
  }
}

async function compare() {
  if (!otherId.value || comparing.value) return
  comparing.value = true
  error.value = ''
  comparison.value = null
  try {
    const query = new URLSearchParams({ other_run_id: otherId.value })
    comparison.value = await backend.json(
      `${backend.audit(props.run.run_id)}/codex-comparison?${query}`,
      schema,
    )
  } catch (cause) {
    error.value = readableError(cause)
  } finally {
    comparing.value = false
  }
}

watch(open, (next) => {
  if (next) void loadCandidates()
})
</script>

<template>
  <UModal v-model:open="open" title="Сравнение проверок" :ui="{ content: 'sm:max-w-2xl' }">
    <UButton label="Сравнить запуски" variant="outline" icon="i-lucide-chart-no-axes-combined" />
    <template #body>
      <div class="grid gap-3">
        <p v-if="loading" role="status">Загружаем историю проверок…</p>
        <p v-else-if="!candidates.length && !error" class="muted">
          В последних 50 запусках отдела нет другой завершённой проверки этой редакции ВОР.
        </p>
        <label v-else-if="candidates.length" class="grid gap-1">
          Другой завершённый запуск
          <select v-model="otherId" :disabled="comparing" @change="comparison = null">
            <option disabled value="">Выберите запуск</option>
            <option v-for="item in candidates" :key="item.run_id" :value="item.run_id">
              {{ runName(item.mode, item.codex_subagents) }} ·
              {{
                item.created_at ? new Date(item.created_at).toLocaleString('ru-RU') : item.run_id
              }}
            </option>
          </select>
        </label>
        <p v-if="error" role="alert" class="text-error">{{ error }}</p>
        <UButton
          v-if="candidates.length"
          label="Сравнить"
          :disabled="!otherId"
          :loading="comparing"
          class="justify-self-start"
          @click="compare"
        />
        <div v-if="comparison" class="overflow-x-auto">
          <p class="muted">
            Совпали версии ВОР, ПД и {{ comparison.normative_document_count }} нормативных
            оригиналов.
          </p>
          <table class="mt-2 w-full text-left text-sm">
            <thead>
              <tr class="border-b border-default">
                <th scope="col" class="p-2">Показатель</th>
                <th v-for="item in comparison.runs" :key="item.run_id" scope="col" class="p-2">
                  {{ runName(item.mode, item.subagents) }}
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="field in fields" :key="field.key" class="border-b border-default">
                <th scope="row" class="p-2 font-normal">{{ field.label }}</th>
                <td v-for="item in comparison.runs" :key="item.run_id" class="p-2 tabular-nums">
                  {{ displayValue(item[field.key]) }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
  </UModal>
</template>

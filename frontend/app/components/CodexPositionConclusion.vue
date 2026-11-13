<script setup lang="ts">
import type { Citation, PositionConclusion, VersionDto } from '~/generated/types.gen'
import { codexDirections, codexFindings, codexStatuses } from '~/utils/codexPresentation'
import { zCodexCoefficientRun } from '~/generated/zod.gen'

const props = defineProps<{
  runId: string
  result: PositionConclusion
  documents: VersionDto[]
  coefficientAvailable?: boolean
}>()
const emit = defineEmits<{
  projectSource: [documentId: string, page: number, highlight?: number[] | null]
}>()
const backend = useBackend()
const basis = ref('')
const includeRelated = ref(false)
const submitting = ref(false)
const clarificationError = ref('')
const activeFinding = ref<number | null>(null)
const findingBasis = ref('')

function findingIndex(key: (typeof codexDirections)[number]['key'], ordinal: number) {
  if (ordinal === 0) return codexDirections.findIndex((direction) => direction.key === key)
  let seen = 0
  const index = (props.result.parameter_findings ?? []).findIndex(
    (item) => item.direction === key && ++seen === ordinal,
  )
  return index === -1 ? -1 : index + codexDirections.length
}

function openClarification(index: number) {
  activeFinding.value = index
  findingBasis.value = ''
  clarificationError.value = ''
}

async function recheckFinding(index: number) {
  if (submitting.value || index < 0) return
  submitting.value = true
  clarificationError.value = ''
  try {
    const result = await backend.json(
      `${backend.audit(props.runId)}/positions/${props.result.position_index}/codex-clarification`,
      zCodexCoefficientRun,
      { method: 'POST', body: JSON.stringify({ basis: findingBasis.value, finding_index: index }) },
    )
    await navigateTo(`/audits/${result.run_id}`)
  } catch (error) {
    clarificationError.value =
      error instanceof Error ? error.message : 'Не удалось создать перепроверку.'
  } finally {
    submitting.value = false
  }
}

async function recheckCoefficient() {
  submitting.value = true
  clarificationError.value = ''
  try {
    const result = await backend.json(
      `${backend.audit(props.runId)}/positions/${props.result.position_index}/codex-coefficient`,
      zCodexCoefficientRun,
      {
        method: 'POST',
        body: JSON.stringify({ basis: basis.value, include_related: includeRelated.value }),
      },
    )
    await navigateTo(`/audits/${result.run_id}`)
  } catch (error) {
    clarificationError.value =
      error instanceof Error ? error.message : 'Не удалось создать перепроверку.'
  } finally {
    submitting.value = false
  }
}

watch(
  () => [props.runId, props.result.position_index],
  () => {
    activeFinding.value = null
    findingBasis.value = ''
    basis.value = ''
    includeRelated.value = false
    clarificationError.value = ''
  },
)

function citationTitle(source: Citation) {
  const name = props.documents.find((document) => document.id === source.document_id)?.filename
  if (source.kind === 'vor') return `${name ?? 'ВОР'} · ${source.sheet} · строка ${source.row}`
  return `${name ?? (source.kind === 'normative' ? 'Нормативный документ' : 'ПД')} · стр. ${source.page}`
}

function citationUrl(source: Citation) {
  if (source.kind === 'normative')
    return `${backend.audit(props.runId)}/codex-sources/${encodeURIComponent(source.document_id)}#page=${source.page}`
  return `${backend.contentUrl(source.document_id)}?download=true`
}
</script>

<template>
  <section
    aria-label="Направления проверки Codex"
    class="grid min-w-0 gap-3 [overflow-wrap:anywhere]"
  >
    <p class="muted">Конструкция или группа работ · {{ result.group }}</p>
    <UCard v-for="{ key, label } in codexDirections" :key="key" class="min-w-0">
      <h2 class="font-semibold">{{ label }}</h2>
      <div
        v-for="(finding, ordinal) in codexFindings(result, key)"
        :key="ordinal"
        class="mt-3 grid min-w-0 gap-3"
        :class="{ 'border-t border-default pt-3': ordinal > 0 }"
      >
        <div class="flex flex-wrap items-center gap-2">
          <p class="font-medium">{{ finding.parameter }}</p>
          <UBadge
            :label="codexStatuses[finding.status].label"
            :color="codexStatuses[finding.status].color"
            variant="soft"
          />
        </div>
        <p>{{ finding.conclusion }}</p>
        <dl class="grid gap-1 text-sm">
          <div v-if="finding.vor_value" class="flex flex-wrap gap-x-2">
            <dt class="muted">ВОР</dt>
            <dd>{{ finding.vor_value }} {{ finding.unit }}</dd>
          </div>
          <div v-if="finding.source_value" class="flex flex-wrap gap-x-2">
            <dt class="muted">По источнику</dt>
            <dd>{{ finding.source_value }} {{ finding.unit }}</dd>
          </div>
          <div v-if="finding.calculation" class="flex flex-wrap gap-x-2">
            <dt class="muted">Расчёт</dt>
            <dd>
              {{ finding.calculation.expression }} = {{ finding.calculation.result }}
              {{ finding.unit }}
            </dd>
          </div>
          <div v-if="finding.calculation?.operand_sources?.length">
            <dt class="muted">Источники чисел по порядку</dt>
            <dd>
              {{ finding.calculation?.operand_sources?.map((index) => index + 1).join(', ') }}
            </dd>
          </div>
          <div v-if="finding.deviation" class="flex flex-wrap gap-x-2">
            <dt class="muted">Отклонение от ВОР</dt>
            <dd>{{ finding.deviation }} {{ finding.unit }}</dd>
          </div>
        </dl>
        <div v-if="finding.missing_facts" class="grid gap-1 border-l-2 border-warning pl-3">
          <p><strong>Требуется</strong> {{ finding.missing_facts.required }}</p>
          <p><strong>Почему</strong> {{ finding.missing_facts.reason }}</p>
          <p><strong>Уже установлено</strong> {{ finding.missing_facts.established }}</p>
          <form
            v-if="activeFinding === findingIndex(key, ordinal)"
            class="mt-2 grid gap-2"
            @submit.prevent="recheckFinding(findingIndex(key, ordinal))"
          >
            <label :for="`finding-basis-${key}-${ordinal}`">Документ, пункт или пояснение</label>
            <UTextarea
              :id="`finding-basis-${key}-${ordinal}`"
              v-model="findingBasis"
              :minlength="4"
              :maxlength="2000"
              required
            />
            <UButton type="submit" :loading="submitting" :disabled="findingBasis.trim().length < 4"
              >Перепроверить позицию</UButton
            >
            <p v-if="clarificationError" role="alert">{{ clarificationError }}</p>
          </form>
          <UButton
            v-else
            variant="link"
            :disabled="submitting"
            @click="openClarification(findingIndex(key, ordinal))"
            >Уточнить этот параметр</UButton
          >
        </div>
        <ul v-if="finding.sources.length" class="grid gap-2 border-t border-default pt-2">
          <li v-for="(source, index) in finding.sources" :key="index">
            <UButton
              v-if="source.kind === 'project' && source.page !== null"
              :label="citationTitle(source)"
              variant="link"
              icon="i-lucide-file-text"
              class="max-w-full whitespace-normal text-left"
              @click="emit('projectSource', source.document_id, source.page, source.highlight)"
            />
            <UButton
              v-else
              :label="citationTitle(source)"
              :to="citationUrl(source)"
              target="_blank"
              rel="noopener"
              variant="link"
              icon="i-lucide-file-text"
              class="max-w-full whitespace-normal text-left"
              external
            />
            <blockquote class="muted pl-3 text-sm">{{ index + 1 }}. {{ source.quote }}</blockquote>
          </li>
        </ul>
      </div>
    </UCard>
    <details v-if="coefficientAvailable">
      <summary class="cursor-pointer font-semibold">Уточнить основание коэффициента 1,26</summary>
      <form class="mt-2 grid gap-2" @submit.prevent="recheckCoefficient">
        <label for="coefficient-basis">Укажите документ и пункт или пояснение сметчика</label>
        <UTextarea
          id="coefficient-basis"
          v-model="basis"
          required
          :minlength="4"
          :maxlength="2000"
        />
        <UCheckbox
          v-model="includeRelated"
          label="Основание относится также к связанным строкам этой группы"
        />
        <p v-if="clarificationError" role="alert">{{ clarificationError }}</p>
        <UButton type="submit" :loading="submitting" :disabled="basis.trim().length < 4"
          >Перепроверить</UButton
        >
      </form>
    </details>
  </section>
</template>

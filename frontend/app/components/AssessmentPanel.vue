<template>
  <section v-if="assessment" aria-label="Единая оценка позиции" class="aw-assessment">
    <h2 class="aw-section-title">Единая оценка · {{ shortRevision }}</h2>
    <div class="aw-assessment-badges">
      <UBadge
        :label="`ПД · ${directionLabel(assessment.project?.state)}`"
        :color="directionColor(assessment.project?.state)"
        variant="solid"
      />
      <UBadge
        :label="`Нормативы · ${directionLabel(assessment.normative?.state)}`"
        :color="directionColor(assessment.normative?.state)"
        variant="soft"
      />
      <UBadge
        :label="`Расчёты · ${directionLabel(assessment.relationships?.state)}`"
        :color="directionColor(assessment.relationships?.state)"
        variant="soft"
      />
      <UBadge
        :label="`Полнота · ${completionLabel}`"
        :color="assessment.completion === 'complete' ? 'success' : 'warning'"
        variant="soft"
      />
    </div>
    <p v-if="ownQuantityText" class="muted">{{ ownQuantityText }}</p>
    <p v-if="(assessment.open_gaps ?? []).length" class="muted">
      Открытые вопросы: {{ (assessment.open_gaps ?? []).join(', ') }}
    </p>
    <p v-else-if="assessment.completion === 'complete'" class="muted">
      Все обязательные вопросы закрыты.
    </p>
  </section>
</template>

<script setup lang="ts">
import type { PositionAssessment } from '~/generated/types.gen'
import { ownQuantityText as ownQuantityLabel } from '~/utils/presentation'

const props = defineProps<{ assessment: PositionAssessment | null | undefined }>()

const assessment = computed(() => props.assessment ?? null)
const shortRevision = computed(() => (assessment.value?.revision ?? '').slice(0, 12))

function directionLabel(state: string | undefined) {
  return state === 'confirmed'
    ? 'подтверждено'
    : state === 'possible_mismatch'
      ? 'расхождение'
      : state === 'needs_facts'
        ? 'нужны факты'
        : state === 'not_applicable'
          ? 'неприменимо'
          : 'ожидает'
}

function directionColor(state: string | undefined) {
  return state === 'confirmed'
    ? 'success'
    : state === 'possible_mismatch'
      ? 'error'
      : state === 'needs_facts'
        ? 'warning'
        : 'neutral'
}

const completionLabel = computed(() => {
  const completion = assessment.value?.completion ?? 'unknown'
  return completion === 'complete'
    ? 'завершена'
    : completion === 'incomplete'
      ? 'не завершена'
      : completion === 'pending'
        ? 'идёт'
        : 'неизвестна'
})

const ownQuantityText = computed(() => ownQuantityLabel(assessment.value?.own_quantity?.state))
</script>

<style scoped>
.aw-assessment-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 8px 0;
}
</style>

<script setup lang="ts">
import type { NormativeReviewDto, NormativeEvidenceAnchor } from '~/generated/types.gen'
import { evidenceText } from '~/utils/evidenceText'
import { firstSentence, unitText } from '~/utils/presentation'
import { normativeDecision, type DecisionRow } from '~/utils/normativeDecision'
import {
  cardTitle,
  cardValue,
  cardQuote,
  cardExplanation,
  sourceAnchor,
  sourceSection,
  compactText,
  formulaPreview,
} from '~/utils/normativePresentation'
const props = defineProps<{
  runId: string
  positionIndex: number
  question: string
  initial?: NormativeReviewDto | null
  automaticBusy?: boolean
  row?: DecisionRow
}>()
const backend = useBackend()
const emit = defineEmits<{
  completed: [review: NormativeReviewDto]
  specifyBasis: []
  findNorm: []
}>()
const review = ref<NormativeReviewDto | null>(props.initial ?? null)
const busy = ref(false),
  error = ref(''),
  pages = ref('')
const result = computed(() => review.value?.payload.position_check)
const composition = computed(() => result.value?.quantity_composition)
const compositionUnit = computed(() =>
  unitText(composition.value?.unit).replace('м2', 'м²').replace('м3', 'м³'),
)
function showOperands(index?: number) {
  const value = composition.value
  if (!value) return
  const terms = index === undefined ? value.terms : value.terms.filter((_, i) => i === index)
  openSource(value.source_id, '', [
    {
      source_id: value.source_id,
      quote: terms.map((t) => t.value).join(' + '),
      rects: terms.map((t) => t.rect),
    },
  ])
}
const decimalLabel = (text: string) => text.replace(/\./g, ',')
const decision = computed(() =>
  result.value ? normativeDecision(result.value, props.row, props.automaticBusy) : null,
)
function showProof() {
  const card = result.value?.parameter_cards?.find(
    (c) => c.required !== false && c.state === 'confirmed' && c.source_ids?.length,
  )
  const item = result.value?.items.find(
    (i) =>
      i.state === 'confirmed' && (i.project_source_ids.length || i.normative_source_ids.length),
  )
  const id = card?.source_ids?.[0] || item?.project_source_ids[0] || item?.normative_source_ids[0]
  if (id) openSource(id, card?.quote, card?.source_anchors)
}
const hasProof = computed(
  () =>
    result.value?.parameter_cards?.some(
      (c) => c.required !== false && c.state === 'confirmed' && c.source_ids?.length,
    ) ||
    result.value?.items.some(
      (i) =>
        i.state === 'confirmed' && (i.project_source_ids.length || i.normative_source_ids.length),
    ),
)
const selectedSource = ref<{
  url: string
  label: string
  page: number
  section: string
  focus: string
  quote: string
  rects?: number[][]
} | null>(null)
const stepLabels: Record<string, string> = {
  confirmed: 'Проверено',
  needs_facts: 'Уточнить',
  possible_mismatch: 'Есть различие',
}
const normativeReferences = computed(() => {
  const reasons = new Map<string, string>()
  for (const item of result.value?.items ?? [])
    for (const id of item.normative_source_ids)
      if (!reasons.has(id)) reasons.set(id, item.explanation)
  return [...reasons].flatMap(([id, explanation]) => {
    const source = result.value?.sources.find((s) => s.id === id && s.kind === 'normative')
    return source
      ? [
          {
            source,
            explanation: compactText(firstSentence(evidenceText(explanation)), 150),
            quote: compactText(
              evidenceText(source.text).replace(/^\s*\d+(?:\.\d+)+[.)]?\s*/, ''),
              170,
            ),
          },
        ]
      : []
  })
})
function openSource(id: string, quote = '', anchors: NormativeEvidenceAnchor[] = []) {
  const source = result.value?.sources.find((s) => s.id === id)
  if (!source) return
  const anchor = anchors.find((a) => a.source_id === id)
  selectedSource.value = {
    url: sourceLink(id).split('#')[0]!,
    label: source.label,
    page: source.page,
    section: sourceSection(source),
    focus: anchor?.rects?.length ? '' : sourceAnchor(source, anchor?.quote || quote),
    quote: anchor?.quote || quote,
    rects: anchor?.rects,
  }
}
const dateLabel = computed(() =>
  review.value ? new Date(review.value.created_at).toLocaleString('ru-RU') : '',
)
const labels: Record<string, string> = {
  confirmed: 'Подтверждён аспект',
  needs_facts: 'Нужно уточнить',
  possible_mismatch: 'Возможное расхождение',
}
watch(
  () => props.initial,
  (value) => {
    review.value = value ?? null
  },
)
function sourceLabel(id: string) {
  const source = result.value?.sources.find((s) => s.id === id)
  return source ? `${source.label} · стр. ${source.page}` : id
}
function sourceLink(id: string) {
  const source = result.value?.sources.find((s) => s.id === id)
  if (!source || !review.value) return '#'
  return source.kind === 'project'
    ? `/api/v1/documents/${source.version_id}/content#page=${source.page}`
    : `${backend.audit(props.runId)}/positions/${props.positionIndex}/normative/${review.value.id}/sources/${source.source_id}#page=${source.page}`
}
function parsePages(value: string) {
  const values: number[] = []
  for (const part of value.split(/[,;\s]+/).filter(Boolean)) {
    const match = part.match(/^(\d+)(?:-(\d+))?$/)
    if (!match) throw Error('Страницы задаются числами, например: 50, 88-89')
    const start = Number(match[1]),
      end = Number(match[2] ?? match[1])
    if (start < 1 || end > 10000 || end < start || end - start > 5)
      throw Error('Укажите до шести физических страниц PDF')
    for (let page = start; page <= end; page++) values.push(page)
  }
  const unique = [...new Set(values)]
  if (unique.length > 6) throw Error('Для одной проверки можно выбрать до шести страниц')
  return unique
}
async function check() {
  error.value = ''
  let selected: number[]
  try {
    selected = parsePages(pages.value)
  } catch (e) {
    error.value = readableError(e)
    return
  }
  busy.value = true
  try {
    review.value = await backend.checkNormativePosition(props.runId, props.positionIndex, {
      question: props.question,
      pages: selected,
    })
    emit('completed', review.value)
  } catch (e) {
    error.value = readableError(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section aria-label="Проверка одной позиции с нормативами" class="single-check">
    <details class="single-controls" :open="!result">
      <summary>{{ result ? 'Перепроверить позицию' : 'Проверить позицию' }}</summary>
      <p>Текущая позиция и связанные строки ВОР.</p>
      <label
        >Страницы ПД для проверки<input
          v-model="pages"
          aria-label="Страницы ПД для проверки"
          placeholder="Автоматически или, например: 50, 88-89"
          :disabled="busy"
      /></label>
      <UButton type="button" :loading="busy" :disabled="busy || automaticBusy" @click="check"
        >Проверить только эту позицию</UButton
      >
    </details>
    <p v-if="busy" role="status">
      Читаем выбранные страницы и нормативы. Для сканов выполняется OCR, затем один запрос к модели…
    </p>
    <p v-if="error" role="alert">{{ error }}</p>
    <div v-if="result" class="single-result">
      <section
        v-if="decision"
        aria-label="Итог и действия"
        class="decision"
        :data-decision="decision.state"
      >
        <h3>{{ decision.title }}</h3>
        <p>{{ decision.explanation }}</p>
        <section
          v-if="composition?.terms.length"
          class="quantity-composition"
          aria-label="Расчёт количества по таблице ПД"
        >
          <strong>{{
            composition.parent_position_index != null
              ? `Площадь связанной работы №${composition.parent_position_index + 1}`
              : 'Количество из таблицы ПД'
          }}</strong>
          <div class="quantity-equation">
            <template v-for="(term, i) in composition.terms" :key="i">
              <span v-if="i" aria-hidden="true">+</span>
              <UButton
                variant="soft"
                :aria-label="`Показать ${term.label}: ${decimalLabel(term.value)} в ПД`"
                @click="showOperands(i)"
              >
                <span
                  ><small>{{ term.label }}</small
                  ><b>{{ decimalLabel(term.value) }}</b></span
                >
              </UButton>
            </template>
            <span
              >= <b>{{ decimalLabel(composition.total) }} {{ compositionUnit }}</b></span
            >
          </div>
          <p>
            В ВОР{{
              composition.parent_position_index != null
                ? ` по работе №${composition.parent_position_index + 1}`
                : ''
            }}: <strong>{{ decimalLabel(composition.declared_value) }} {{ compositionUnit }}</strong
            >.
            {{
              composition.state === 'confirmed'
                ? 'Сумма совпадает' +
                  (composition.total !== composition.rounded_total ? ' после округления.' : '.')
                : 'Сумма отличается — сверьте исходные ячейки.'
            }}
          </p>
          <UButton variant="outline" @click="showOperands()"
            >Показать оба числа в ПД · стр. {{ composition.page }}</UButton
          >
        </section>
        <div v-if="decision.confirmed.length" class="decision-confirmed">
          <strong>Что подтверждено</strong>
          <ul>
            <li v-for="text in decision.confirmed.slice(0, 4)" :key="text">✓ {{ text }}</li>
          </ul>
          <UButton v-if="hasProof" variant="link" @click="showProof"
            >Показать подтверждение</UButton
          >
        </div>
        <div v-if="decision.state !== 'ok' && decision.state !== 'pending'">
          <strong>Что сделать</strong>
          <article
            v-for="issue in decision.issues.slice(0, 3)"
            :key="issue.card.id"
            class="decision-issue"
          >
            <strong>{{ issue.title }}</strong>
            <p>{{ issue.detail }}</p>
            <UButton
              v-if="issue.card.source_ids?.[0]"
              variant="link"
              @click="
                openSource(issue.card.source_ids[0], issue.card.quote, issue.card.source_anchors)
              "
              >Сверить с источником</UButton
            >
          </article>
          <UButton
            v-if="decision.issues.some((i) => i.action === 'basis')"
            variant="outline"
            @click="emit('specifyBasis')"
            >Указать верное основание в ПД</UButton
          >
          <UButton
            v-if="decision.issues.some((i) => i.action === 'norm')"
            variant="outline"
            @click="emit('findNorm')"
            >Найти норматив</UButton
          >
          <p v-if="!decision.issues.length">
            Откройте найденное основание и сверьте его с этой строкой ВОР. Если выбрана другая
            работа — укажите правильный фрагмент ПД.
          </p>
          <UButton v-if="!decision.issues.length" variant="outline" @click="emit('specifyBasis')"
            >Указать верное основание в ПД</UButton
          >
          <p class="muted">
            После уточнения основания перепроверьте только эту позицию. Изменения в ВОР вносит
            инженер.
          </p>
        </div>
      </section>
      <details class="decision-evidence">
        <summary>Почему такой итог · расчёт и источники</summary>
        <section
          v-if="result.parameter_cards?.length"
          aria-label="Проверяемые параметры"
          class="parameter-cards"
        >
          <h3>Цепочка проверки</h3>
          <ol class="parameter-chain">
            <li
              v-for="(card, index) in result.parameter_cards"
              :key="card.id"
              :data-state="card.state"
            >
              <span
                class="step-number"
                :aria-label="`Шаг ${index + 1}: ${stepLabels[card.state]}`"
                >{{ index + 1 }}</span
              >
              <div class="step-content">
                <div class="parameter-heading">
                  <strong>{{ cardTitle(card) }}</strong
                  ><span class="step-status">{{ stepLabels[card.state] }}</span>
                </div>
                <div class="step-value">{{ cardValue(card) }}</div>
                <blockquote v-if="cardQuote(card)">{{ cardQuote(card) }}</blockquote>
                <blockquote
                  v-else-if="card.role === 'calculation' && card.expression"
                  class="calculation-quote"
                >
                  {{ formulaPreview(card.expression) }}
                </blockquote>
                <p>{{ cardExplanation(card) }}</p>
                <p v-if="card.required === false" class="text-muted text-sm">
                  Связанная позиция материала — проверяется отдельно и не меняет статус этой работы.
                </p>
                <a
                  v-for="id in card.source_ids ?? []"
                  :key="id"
                  class="source-anchor"
                  :href="sourceLink(id)"
                  @click.prevent="openSource(id, card.quote, card.source_anchors)"
                  >{{ sourceLabel(id) }} ↗</a
                >
                <details
                  class="parameter-more"
                  v-if="
                    card.conditions?.length ||
                    card.reason.length > cardExplanation(card).length + 20 ||
                    (card.quote?.length ?? 0) > 190 ||
                    (card.expression?.length ?? 0) > 160
                  "
                >
                  <summary>Подробнее</summary>
                  <p>{{ card.reason }}</p>
                  <p v-for="condition in card.conditions ?? []" :key="condition">{{ condition }}</p>
                  <p v-if="card.quote" class="source-text">{{ card.quote }}</p>
                  <p v-if="card.expression" class="source-text">{{ card.expression }}</p>
                </details>
              </div>
            </li>
          </ol>
          <section
            v-if="normativeReferences.length"
            aria-label="Нормативные пункты"
            class="normative-links"
          >
            <h4>Нормативные пункты</h4>
            <article v-for="ref in normativeReferences.slice(0, 2)" :key="ref.source.id">
              <a
                class="source-anchor"
                :href="sourceLink(ref.source.id)"
                @click.prevent="openSource(ref.source.id, ref.source.text)"
                >{{ sourceLabel(ref.source.id) }} ↗</a
              >
              <blockquote>{{ ref.quote }}</blockquote>
              <p>{{ ref.explanation }}</p>
            </article>
            <details v-if="normativeReferences.length > 2">
              <summary>Ещё источники · {{ normativeReferences.length - 2 }}</summary>
              <a
                v-for="ref in normativeReferences.slice(2)"
                :key="ref.source.id"
                class="source-anchor"
                :href="sourceLink(ref.source.id)"
                @click.prevent="openSource(ref.source.id, ref.source.text)"
                >{{ sourceLabel(ref.source.id) }} ↗</a
              >
            </details>
          </section>
        </section>
      </details>
      <details class="full-analysis">
        <summary>Полный разбор и источники</summary>
        <strong>Результат точечной проверки</strong>
        <p>{{ evidenceText(result.summary) }}</p>
        <p class="muted">
          Отдельный результат от {{ dateLabel }}. Пакетная проверка не перезаписывается.
        </p>
        <article v-for="(item, i) in result.items" :key="i">
          <strong>{{ item.topic }} · {{ labels[item.state] }}</strong>
          <p>{{ evidenceText(item.explanation) }}</p>
          <a
            v-for="id in [...item.project_source_ids, ...item.normative_source_ids]"
            :key="id"
            :href="sourceLink(id)"
            @click.prevent="openSource(id)"
            target="_blank"
            rel="noopener"
            >{{ sourceLabel(id) }}</a
          >
        </article>
        <details>
          <summary>Основания и OCR · {{ result.sources.length }}</summary>
          <div v-for="source in result.sources" :key="source.id" class="check-source">
            <a
              :href="sourceLink(source.id)"
              @click.prevent="openSource(source.id)"
              target="_blank"
              rel="noopener"
              >{{ sourceLabel(source.id) }}</a
            >
            <small>{{
              source.origin === 'native'
                ? 'Текст PDF'
                : source.origin === 'human'
                  ? 'Основание, подтверждённое пользователем'
                  : source.origin.startsWith('ocr')
                    ? 'OCR — сверить с изображением'
                    : 'Извлечённый нормативный текст'
            }}</small>
            <a
              v-if="source.image_key"
              :href="`${backend.audit(runId)}/positions/${positionIndex}/normative/${review?.id}/check-images/${source.id}`"
              target="_blank"
              rel="noopener"
              >Изображение, использованное для OCR</a
            >
            <small v-if="source.engine">{{ source.engine }}</small>
            <a
              v-if="source.words_key"
              :href="`${backend.audit(runId)}/positions/${positionIndex}/normative/${review?.id}/check-ocr/${source.id}`"
              target="_blank"
              rel="noopener"
              >Слова, уверенность и координаты OCR · JSON</a
            >
            <p class="source-text">{{ evidenceText(source.text) }}</p>
          </div>
        </details>
        <details>
          <summary>Связанные строки ВОР · {{ result.related_positions.length }}</summary>
          <p v-for="related in result.related_positions" :key="String(related.index)">
            Строка {{ related.row }} · {{ related.work }} · {{ related.quantity }}
            {{ related.unit }} · {{ related.formula }}
          </p>
        </details>
        <p v-for="warning in result.telemetry.warnings ?? []" :key="warning">{{ warning }}</p>
        <small
          >{{ result.telemetry.model }} · запросов: {{ result.telemetry.requests }} ·
          {{ result.telemetry.elapsed_seconds }} с. Вывод требует проверки инженером.</small
        >
      </details>
    </div>
    <NormativeSourceViewer :source="selectedSource" @close="selectedSource = null" />
  </section>
</template>

<style scoped>
.quantity-composition {
  border: 1px solid var(--ui-border);
  border-radius: 0.5rem;
  padding: 0.75rem;
  margin: 1rem 0;
}
.quantity-equation {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.35rem;
  margin-top: 0.5rem;
  font-variant-numeric: tabular-nums;
}
.quantity-equation small,
.quantity-equation b {
  display: block;
}
.quantity-equation small {
  font-weight: 400;
}
.decision {
  border: 1px solid var(--ui-border);
  border-left: 4px solid var(--color-amber-600);
  border-radius: 0.5rem;
  padding: 1rem;
  margin: 0.75rem 0;
  overflow-wrap: anywhere;
}
.decision[data-decision='ok'] {
  border-left-color: var(--color-green-600);
}
.decision[data-decision='fix'] {
  border-left-color: var(--color-red-600);
}
.decision[data-decision='pending'] {
  border-left-color: var(--ui-primary);
}
.decision h3 {
  font-size: 1.15rem;
  font-weight: 650;
  margin-bottom: 0.5rem;
}
.decision p {
  margin: 0.5rem 0;
}
.decision :deep(button) {
  margin: 0.25rem 0.5rem 0.25rem 0;
}
.decision-confirmed {
  margin: 1rem 0;
}
.decision-confirmed li {
  margin: 0.3rem 0;
}
.decision-issue {
  padding: 0.75rem 0;
  border-bottom: 1px solid var(--ui-border);
}
.decision-evidence {
  margin: 1rem 0;
}
.decision-evidence > summary {
  cursor: pointer;
  color: var(--ui-primary);
  margin-bottom: 1rem;
}
.parameter-cards {
  min-width: 0;
}
.parameter-cards h3 {
  font-size: 1rem;
  font-weight: 600;
  margin: 0 0 0.75rem;
}
.single-controls {
  margin-bottom: 1rem;
  font-size: 0.875rem;
}
.parameter-chain {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 1rem;
}
.parameter-chain > li {
  display: grid;
  grid-template-columns: 1.75rem minmax(0, 1fr);
  gap: 0.65rem;
  position: relative;
}
.parameter-chain > li:not(:last-child)::after {
  content: '';
  position: absolute;
  left: 0.85rem;
  top: 2rem;
  bottom: -0.75rem;
  width: 1px;
  background: var(--ui-border);
}
.step-number {
  border: 1px solid var(--ui-border);
  border-radius: 50%;
  width: 1.75rem;
  height: 1.75rem;
  display: grid;
  place-items: center;
  font-size: 0.8rem;
}
.parameter-heading {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.25rem 0.5rem;
}
.step-status {
  margin-left: auto;
  font-size: 0.75rem;
  color: var(--ui-text-muted);
}
[data-state='confirmed'] .step-status,
[data-state='confirmed'] .step-number {
  color: var(--ui-success);
}
[data-state='needs_facts'] .step-status {
  color: var(--ui-warning);
}
[data-state='possible_mismatch'] .step-status {
  color: var(--ui-error);
}
.step-value {
  color: var(--ui-text-muted);
  font-size: 0.875rem;
}
.step-content {
  min-width: 0;
  overflow-wrap: anywhere;
}
.single-check .step-content p,
.single-check .normative-links p {
  margin-block: 0.3rem;
  font-size: 0.875rem;
}
.single-check blockquote {
  margin: 0.5rem 0;
  padding-left: 0.65rem;
  border-left: 2px solid var(--ui-primary);
  color: var(--ui-text-toned);
  font-size: 0.875rem;
  overflow-wrap: anywhere;
}
.source-anchor {
  display: block;
  font-size: 0.8125rem;
  margin-top: 0.35rem;
}
.parameter-more {
  margin-top: 0.35rem;
  font-size: 0.8125rem;
}
.normative-links {
  display: grid;
  gap: 0.75rem;
  margin-block: 1rem;
}
.normative-links h4 {
  margin: 0;
}
.full-analysis {
  border-top: 1px solid var(--ui-border);
  padding-top: 0.75rem;
  margin-top: 1rem;
}
.single-check {
  border-block: 1px solid var(--ui-border);
  margin-block: 1rem;
  padding-block: 1rem;
  overflow-wrap: anywhere;
}
.single-check p {
  margin-block: 0.5rem;
}
.single-check label {
  display: grid;
  gap: 0.3rem;
  margin-block: 0.7rem;
}
.single-check input {
  width: 100%;
  border: 1px solid var(--ui-border);
  border-radius: 0.4rem;
  padding: 0.5rem;
}
.single-result article,
.check-source {
  border-top: 1px solid var(--ui-border);
  padding-block: 0.7rem;
}
.single-result a,
.check-source small {
  display: block;
  margin-block: 0.3rem;
}
.single-result details {
  margin-block: 0.7rem;
}
.source-text {
  white-space: pre-wrap;
  max-height: 24rem;
  overflow-y: auto;
}
</style>

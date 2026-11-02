<script setup lang="ts">
import type {
  NormativeFacts,
  NormativeReviewDto,
  NormativeCitation,
  NormativeCandidate,
  NormativeLibrary,
  NormativeStage,
} from '~/generated/types.gen'
import { quantityText } from '~/utils/presentation'
import { evidenceText } from '~/utils/evidenceText'
import type { DecisionRow } from '~/utils/normativeDecision'
const props = defineProps<{ runId: string; positionIndex: number; row?: DecisionRow }>()
const emit = defineEmits<{ validated: []; specifyBasis: [] }>()
const searchTools = ref<HTMLDetailsElement>(),
  queryInput = ref<HTMLInputElement>()
async function focusNormSearch() {
  if (searchTools.value) searchTools.value.open = true
  await nextTick()
  queryInput.value?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  queryInput.value?.focus({ preventScroll: true })
}
function checked(row: NormativeReviewDto) {
  latestCheck.value = row
  emit('validated')
}
const backend = useBackend()
const loaded = ref(false),
  busy = ref(false),
  saving = ref(false),
  error = ref(''),
  message = ref('')
const query = ref(''),
  kind = ref<'GESN' | 'SP' | 'GOST' | 'PUE' | 'ALL'>('GESN')
const profile = ref<'electrical' | 'roads'>('electrical')
const library = ref<NormativeLibrary | null>(null)
const latestCheck = ref<NormativeReviewDto | null>(null)
const automaticStage = ref<NormativeStage | null>(null)
const panelOpen = ref(false)
const automaticBusy = computed(() =>
  ['pending', 'dispatched', 'running'].includes(automaticStage.value?.status ?? ''),
)
let refreshTimer: ReturnType<typeof setInterval> | undefined
let refreshing = false
onMounted(() => {
  void load()
  refreshTimer = setInterval(async () => {
    if (!panelOpen.value || !loaded.value || !automaticBusy.value || refreshing) return
    refreshing = true
    try {
      const state = await backend.normative(props.runId, props.positionIndex)
      automaticStage.value = state.automatic_stage ?? null
      latestCheck.value = state.latest_check ?? null
      emit('validated')
    } catch {
      // A temporary poll failure must not clear the saved result or engineer's form.
    } finally {
      refreshing = false
    }
  }, 4000)
})
onBeforeUnmount(() => clearInterval(refreshTimer))
const mode = ref<'lexical' | 'context'>('lexical')
const facts = ref<NormativeFacts>({}),
  origins = ref<Record<string, string>>({})
const heightIncluded = ref('unknown'),
  separateConnection = ref('unknown')
const result = ref<NormativeReviewDto | null>(null),
  saved = ref<NormativeReviewDto | null>(null)
const selected = ref<string[]>([]),
  comment = ref(''),
  lastForm = ref('')
const labels: Record<string, string> = {
  input: 'Входные токены',
  output: 'Выходные токены',
  total: 'Всего токенов',
  reasoning: 'Токены рассуждений',
  candidate: 'Вариант',
  needs_facts: 'Нужны уточнения',
  possible_mismatch: 'Параметры не совпали',
  possible_duplicate: 'Проверить двойной учёт',
  match: 'Совпадает',
  mismatch: 'Не совпадает',
  unknown: 'Нет данных',
  not_applicable: 'Условия не выполнены',
  included_height_exceeded: 'Превышена высота, уже учтённая разделом сборника',
  height_m: 'Высота',
  separate_connection_position: 'Есть отдельная позиция подключения проводников',
  family: 'Семейство сборника',
  collection: 'Номер сборника',
  system: 'Система: силовая, контактная сеть, освещение или автоматика',
  laid_pipes_blocks_boxes: 'В проложенных трубах, блоках и коробах',
  trench: 'В траншее',
  tray: 'В лотках',
  open: 'Открыто',
}
const fields = [
  { id: 'voltage_kv', label: 'Напряжение, кВ' },
  { id: 'mass_kg_per_m', label: 'Масса кабеля, кг/м' },
  { id: 'quantity', label: 'Количество ВОР' },
  { id: 'unit', label: 'Единица ВОР' },
  { id: 'height_m', label: 'Высота монтажа, м' },
  { id: 'cross_section_mm2', label: 'Сечение, мм²' },
] as const
function requestBody() {
  return {
    query: query.value,
    kind: kind.value,
    mode: mode.value,
    facts: {
      ...facts.value,
      included_height_exceeded:
        heightIncluded.value === 'unknown' ? null : heightIncluded.value === 'yes',
      separate_connection_position:
        separateConnection.value === 'unknown' ? null : separateConnection.value === 'yes',
    },
  }
}
const dirty = computed(() => !!result.value && JSON.stringify(requestBody()) !== lastForm.value)
function restore(row: NormativeReviewDto) {
  result.value = row
  mode.value = row.payload.mode ?? 'lexical'
  query.value = row.payload.query
  kind.value = row.payload.kind as typeof kind.value
  facts.value = { ...row.payload.facts }
  origins.value = row.payload.fact_origins
  heightIncluded.value =
    facts.value.included_height_exceeded == null
      ? 'unknown'
      : facts.value.included_height_exceeded
        ? 'yes'
        : 'no'
  separateConnection.value =
    facts.value.separate_connection_position == null
      ? 'unknown'
      : facts.value.separate_connection_position
        ? 'yes'
        : 'no'
  selected.value =
    saved.value?.payload.search_id === row.id ? [...(saved.value.payload.selected_ids ?? [])] : []
  lastForm.value = JSON.stringify(requestBody())
}
async function load(event?: Event) {
  panelOpen.value = event ? (event.target as HTMLDetailsElement).open : true
  if (!panelOpen.value || loaded.value || busy.value) return
  error.value = ''
  busy.value = true
  try {
    const state = await backend.normative(props.runId, props.positionIndex)
    profile.value = state.profile ?? 'electrical'
    library.value = state.library ?? null
    latestCheck.value = state.latest_check ?? null
    automaticStage.value = state.automatic_stage ?? null
    saved.value =
      (state.selection?.payload.kb.profile ?? 'electrical') === profile.value
        ? (state.selection ?? null)
        : null
    query.value = state.work_name.slice(0, 1800)
    facts.value = { ...state.facts }
    origins.value = state.fact_origins
    if (
      state.latest_search &&
      (state.latest_search.payload.kb.profile ?? 'electrical') === profile.value
    )
      restore(state.latest_search)
    comment.value = saved.value?.payload.comment ?? ''
    loaded.value = true
  } catch (e) {
    error.value = readableError(e)
  } finally {
    busy.value = false
  }
}
async function search(searchMode: 'lexical' | 'context' = 'lexical') {
  if (busy.value) return
  mode.value = searchMode
  error.value = ''
  message.value = ''
  busy.value = true
  try {
    restore(await backend.searchNormative(props.runId, props.positionIndex, requestBody()))
  } catch (e) {
    error.value = readableError(e)
  } finally {
    busy.value = false
  }
}
async function save() {
  if (!result.value || dirty.value) return
  error.value = ''
  message.value = ''
  saving.value = true
  try {
    saved.value = await backend.selectNormative(props.runId, props.positionIndex, {
      search_id: result.value.id,
      norm_ids: selected.value,
      comment: comment.value,
    })
    message.value = 'Выбор инженера сохранён для этой позиции и версии нормативной базы.'
  } catch (e) {
    error.value = readableError(e)
  } finally {
    saving.value = false
  }
}
function link(citation: NormativeCitation, review = result.value) {
  return review
    ? `${backend.audit(props.runId)}/positions/${props.positionIndex}/normative/${review.id}/sources/${citation.source_id}#page=${citation.page}`
    : '#'
}
function candidateColor(item: NormativeCandidate) {
  return item.status === 'possible_mismatch' ? ('warning' as const) : ('neutral' as const)
}
const savedCandidates = computed(
  () =>
    saved.value?.payload.candidates.filter((c) =>
      saved.value?.payload.selected_ids?.includes(c.id),
    ) ?? [],
)
</script>

<template>
  <details class="normative-panel" open @toggle="load">
    <summary>
      Итог проверки и нормативы ·
      {{
        loaded && profile === 'roads'
          ? 'Дорожные работы'
          : loaded
            ? 'Электрика'
            : 'Документы проекта'
      }}
    </summary>
    <section aria-label="Нормативный подбор" class="normative-body">
      <p v-if="automaticBusy" role="status">
        Нормативный разбор выполняется отдельно. Сопоставление остальных позиций продолжается.
      </p>
      <p v-else-if="automaticStage?.status === 'failed'" role="status">
        {{ automaticStage.message }}
      </p>
      <p v-else-if="automaticStage?.status === 'done' && !latestCheck" role="status">
        Нормативный этап завершён: для автоматического вывода недостаточно источников. Можно
        уточнить основание и проверить позицию вручную.
      </p>
      <p class="muted">
        {{
          profile === 'roads'
            ? 'Дорожная одежда, благоустройство и ГЭСН27/47.'
            : 'Пилот: кабельные работы и ГЭСНм08.'
        }}
        СП и ГОСТ — требования проекта отдельно от расценок.
      </p>
      <p v-if="error" class="error-box" role="alert">{{ error }}</p>
      <p v-if="message" role="status">{{ message }}</p>
      <details v-if="library" class="normative-library">
        <summary>Документы проекта · {{ library.sources.length }}</summary>
        <p>
          Закреплённый набор источников. Полноту изменений и применимость к дате проекта нужно
          сверить.
        </p>
        <p v-for="source in library.sources" :key="source.id">
          <a
            :href="`${backend.audit(runId)}/positions/${positionIndex}/normative/library/${source.id}`"
            target="_blank"
            rel="noopener"
            >{{ source.designation }}</a
          >
          · {{ source.pages }} стр.
          <small v-if="source.readable_pages === 0"> · скан: текст не индексирован</small>
        </p>
        <p v-if="library.gaps.length">
          Пробелы и ограничения: {{ library.gaps.length }}. Полный реестр и источники сохранены с
          выпуском базы.
        </p>
      </details>
      <NormativePositionCheck
        v-if="loaded"
        :key="`${runId}:${positionIndex}`"
        :run-id="runId"
        :position-index="positionIndex"
        :question="query"
        :initial="latestCheck"
        :automatic-busy="automaticBusy"
        :row="row"
        @completed="checked"
        @specify-basis="emit('specifyBasis')"
        @find-norm="focusNormSearch"
      />
      <div v-if="savedCandidates.length" class="normative-saved">
        <strong>Выбор инженера · {{ savedCandidates.length }}</strong>
        <p v-for="check in saved?.payload.checks ?? []" :key="check">{{ check }}</p>
        <p v-for="item in savedCandidates" :key="item.id">{{ item.code }} · {{ item.title }}</p>
        <p class="muted">Выпуск {{ saved?.payload.kb.release_id }} · {{ saved?.actor }}</p>
      </div>
      <details v-if="loaded" ref="searchTools" class="normative-tools">
        <summary>Подобрать или уточнить норматив</summary>
        <form @submit.prevent="search('lexical')">
          <label
            >Работа или код нормы<input
              v-model="query"
              ref="queryInput"
              aria-label="Работа или код нормы"
              required
              maxlength="2000"
          /></label>
          <label
            >Назначение поиска<select v-model="kind" aria-label="Назначение нормативного поиска">
              <option value="GESN">
                {{
                  profile === 'roads'
                    ? 'ГЭСН27/47 · работы и ресурсы'
                    : 'ГЭСНм08 · работы и ресурсы'
                }}
              </option>
              <option value="ALL">Требования и технические части · все источники</option>
              <option value="SP">СП · требования проекта</option>
              <option value="GOST">ГОСТ · требования проекта</option>
              <option value="PUE">ПУЭ · требования проекта</option>
            </select></label
          >
          <details class="normative-facts">
            <summary>Параметры работы · уточнить условия</summary>
            <div class="normative-grid">
              <label
                >Система<select v-model="facts.system" aria-label="Система работ">
                  <option value="">Не установлена</option>
                  <option value="power">Силовые сети</option>
                  <option value="contact_network">Контактная сеть</option>
                  <option value="lighting">Освещение</option>
                  <option value="automation">Автоматика</option>
                </select></label
              >
              <label
                >Способ прокладки<select v-model="facts.placement" aria-label="Способ прокладки">
                  <option value="">Не установлен</option>
                  <option value="laid_pipes_blocks_boxes">В трубах, блоках, коробах</option>
                  <option value="trench">В траншее</option>
                  <option value="tray">На лотках</option>
                  <option value="open">Открыто</option>
                </select></label
              >
              <label v-for="field in fields" :key="field.id"
                >{{ field.label
                }}<input
                  v-model="facts[field.id]"
                  :aria-label="field.label"
                  :inputmode="field.id === 'unit' ? 'text' : 'decimal'"
                  :placeholder="field.id === 'unit' ? 'м, шт., …' : 'Не установлено'"
                /><small v-if="origins[field.id]">{{ origins[field.id] }}</small></label
              >
              <label
                >Дата проекта<input
                  v-model="facts.project_date"
                  type="date"
                  aria-label="Дата проекта"
              /></label>
            </div>
            <label
              >Высота превышает уже учтённую разделом сборника?<select
                v-model="heightIncluded"
                aria-label="Превышена учтённая высота"
              >
                <option value="unknown">Не установлено — сверить техчасть</option>
                <option value="yes">Да, подтверждено инженером</option>
                <option value="no">Нет</option>
              </select></label
            >
            <label
              >Есть отдельная позиция подключения проводников?<select
                v-model="separateConnection"
                aria-label="Отдельная позиция подключения"
              >
                <option value="unknown">Не установлено</option>
                <option value="yes">Да</option>
                <option value="no">Нет</option>
              </select></label
            >
          </details>
          <UButton type="submit" :loading="busy">Найти нормативные варианты</UButton>
          <UButton type="button" variant="outline" :disabled="busy" @click="search('context')">
            Подобрать по контексту
          </UButton>
          <p class="muted">
            Вопрос можно переформулировать своими словами. Контекстный подбор использует модель: до
            двух запросов с текстом ВОР и основаниями ПД. Исходное название не меняется.
          </p>
        </form>
      </details>
      <p v-if="busy" role="status" class="muted">
        {{
          mode === 'context'
            ? 'Сопоставляем контекст работы и нормативные варианты…'
            : 'Читаем локальную нормативную базу…'
        }}
      </p>
      <template v-if="result">
        <section
          v-if="result.payload.context_analysis"
          aria-label="Контекст нормативного поиска"
          class="normative-saved"
        >
          <strong>Как понята работа</strong>
          <p>
            {{
              result.payload.context_analysis.interpretation || 'Контекстный анализ не завершён.'
            }}
          </p>
          <p v-if="result.payload.context_analysis.queries?.length">
            Запросы к базе: {{ result.payload.context_analysis.queries?.join('; ') }}
          </p>
          <ul v-if="result.payload.context_analysis.missing_facts?.length">
            <li v-for="fact in result.payload.context_analysis.missing_facts" :key="fact">
              Уточнить: {{ fact }}
            </li>
          </ul>
          <p
            v-for="warning in result.payload.context_analysis.warnings"
            :key="warning"
            class="normative-hint"
          >
            {{ warning }}
          </p>
          <small
            >{{ result.payload.context_analysis.model }} · запросов:
            {{ result.payload.context_analysis.requests }} ·
            {{ result.payload.context_analysis.elapsed_seconds }} с</small
          >
          <details>
            <summary>Контекст и происхождение результата</summary>
            <p>
              Использованы строка и примечание ВОР, указанные параметры и основания ПД ниже.
              Автоматические цитаты требуют проверки.
            </p>
            <p v-for="(value, key) in result.payload.context_analysis.usage" :key="key">
              {{ labels[key] || key }}: {{ value }}
            </p>
            <a
              v-if="result.payload.context_analysis.trace_url"
              :href="result.payload.context_analysis.trace_url"
              target="_blank"
              rel="noopener"
              >Трасса запроса</a
            >
          </details>
        </section>
        <p class="muted">
          Выпуск {{ result.payload.kb.release_id }} · SHA
          {{ result.payload.kb.sha256?.slice(0, 12) }} · редакция и применимость требуют сверки.
        </p>
        <p v-for="check in result.payload.checks" :key="check" class="normative-hint">
          {{ check }}
        </p>
        <p v-if="!result.payload.candidates.length">
          Подходящие варианты не найдены. Уточните код, формулировку или параметры; это не
          доказывает отсутствия нормы.
        </p>
        <article
          v-for="item in result.payload.candidates"
          :key="item.id"
          class="normative-candidate"
        >
          <label class="normative-choice"
            ><input
              v-model="selected"
              type="checkbox"
              :disabled="busy || saving"
              :value="item.id"
              :aria-label="`Выбрать ${item.code}`"
            /><strong>{{ item.code }} · {{ item.title }}</strong></label
          >
          <UBadge :color="candidateColor(item)" variant="subtle">{{ labels[item.status] }}</UBadge>
          <p v-if="item.context_reason" class="normative-text">
            <strong>Почему предложено:</strong> {{ evidenceText(item.context_reason) }}
          </p>
          <p>
            {{ item.designation
            }}<template v-if="item.unit_raw"> · Измеритель: {{ item.unit_raw }}</template>
          </p>
          <p class="muted">
            {{
              item.extraction_status === 'definition_reviewed'
                ? 'Определение нормы сверено с оригиналом; ресурсы проверены частично.'
                : 'Автоматическое извлечение — требуется сверка с оригиналом.'
            }}
          </p>
          <p v-for="(citation, i) in item.citations" :key="i">
            <a :href="link(citation)" target="_blank" rel="noopener"
              >{{ citation.designation }} · {{ citation.label }} · физическая стр.
              {{ citation.page }}</a
            >
          </p>
          <ul v-if="item.checks.length">
            <li v-for="check in item.checks" :key="check.label">
              {{ check.label }}: {{ labels[check.state] }} · ВОР:
              {{ labels[check.actual || ''] || check.actual || 'не задано' }}, норма:
              {{ labels[check.expected || ''] || check.expected || 'не извлечено'
              }}<span v-if="check.norm_quantity">
                · {{ quantityText(check.norm_quantity) }} измерителя нормы</span
              >
            </li>
          </ul>
          <details v-if="item.missing_facts.length">
            <summary>Что уточнить · {{ item.missing_facts.length }}</summary>
            <ul>
              <li v-for="field in item.missing_facts" :key="field">{{ labels[field] || field }}</li>
            </ul>
          </details>
          <details v-if="item.composition.length">
            <summary>Состав работ · {{ item.composition.length }}</summary>
            <p v-for="(operation, i) in item.composition" :key="i">{{ operation }}</p>
          </details>
          <details v-if="item.resources.length">
            <summary>Ресурсы · {{ item.resources.length }}</summary>
            <div v-for="(resource, i) in item.resources" :key="i" class="normative-resource">
              <strong>{{ resource.code }} · {{ resource.name }}</strong>
              <p>
                {{
                  resource.quantity !== null
                    ? quantityText(resource.quantity)
                    : resource.quantity_kind === 'by_project'
                      ? 'По проекту (П)'
                      : resource.quantity_kind === 'blank' || resource.quantity_kind === 'dash'
                        ? 'Пустое значение — не ноль'
                        : `Не распознано: ${resource.raw_quantity}`
                }}
                {{ resource.unit }}
              </p>
              <small>{{
                resource.status === 'source_reviewed'
                  ? 'Ячейка сверена по оригиналу'
                  : 'Ячейка требует сверки'
              }}</small
              ><a
                v-for="(citation, j) in resource.citations"
                :key="j"
                :href="link(citation)"
                target="_blank"
                rel="noopener"
              >
                · стр. {{ citation.page }}</a
              >
            </div>
          </details>
          <details v-if="item.rules.length">
            <summary>Коэффициенты и условия · {{ item.rules.length }}</summary>
            <div v-for="rule in item.rules" :key="rule.id" class="normative-resource">
              <strong>{{ rule.title }}</strong>
              <p>
                {{ labels[rule.state] || 'Условия требуют сверки'
                }}<span v-if="rule.factor">
                  · Возможный коэффициент {{ quantityText(rule.factor) }}</span
                >
              </p>
              <p v-if="rule.target">
                {{
                  rule.target === 'worker_labor'
                    ? 'К затратам труда рабочих'
                    : 'Область влияния сверить по пункту'
                }}
              </p>
              <p>{{ rule.message }}</p>
              <p v-if="rule.missing_facts.length">
                Не установлено: {{ rule.missing_facts.map((f) => labels[f] || f).join('; ') }}.
              </p>
              <a
                v-for="(citation, i) in rule.citations"
                :key="i"
                :href="link(citation)"
                target="_blank"
                rel="noopener"
                >{{ citation.designation }} · стр. {{ citation.page }}
              </a>
            </div>
          </details>
          <details v-if="item.text">
            <summary>Текст пункта</summary>
            <p class="normative-text">{{ item.text }}</p>
          </details>
        </article>
        <details v-if="result.payload.project_citations.length">
          <summary>Основания из ПД · {{ result.payload.project_citations.length }}</summary>
          <div
            v-for="(citation, i) in result.payload.project_citations"
            :key="i"
            class="normative-resource"
          >
            <a
              :href="`/api/v1/documents/${citation.version_id}/content#page=${citation.page}`"
              target="_blank"
              rel="noopener"
              >{{ citation.filename }} · стр. {{ citation.page }}</a
            ><small>
              ·
              {{
                citation.origin === 'human'
                  ? 'Основание подтверждено инженером'
                  : 'Автоматическая цитата, требует сверки'
              }}</small
            >
            <p class="normative-text">{{ evidenceText(citation.quote) }}</p>
          </div>
        </details>
        <label
          >Примечание инженера<textarea
            v-model="comment"
            maxlength="2000"
            aria-label="Примечание к нормативному выбору"
          />
        </label>
        <p v-if="dirty" class="normative-hint">
          Параметры изменены. Повторите поиск перед сохранением выбора.
        </p>
        <UButton :disabled="dirty || busy" :loading="saving" @click="save"
          >Сохранить выбор инженера · {{ selected.length }}</UButton
        >
        <p class="muted">
          Выбор сохраняется отдельно от автоматического заключения. Коэффициенты не применяются и
          смета не изменяется автоматически.
        </p>
      </template>
    </section>
  </details>
</template>

<style scoped>
.normative-panel {
  border: 1px solid var(--ui-border);
  border-radius: 8px;
  padding: 12px;
  background: var(--ui-bg-elevated);
  min-width: 0;
  overflow-wrap: anywhere;
}
summary {
  cursor: pointer;
  color: var(--ui-primary);
  padding: 6px 0;
}
.normative-body,
form {
  display: grid;
  gap: 12px;
  margin-top: 10px;
}
label {
  display: grid;
  gap: 4px;
  font-size: 13px;
}
input:not([type='checkbox']),
select,
textarea {
  width: 100%;
  min-width: 0;
  border: 1px solid var(--ui-border);
  border-radius: 6px;
  padding: 8px;
  background: var(--ui-bg);
  color: var(--ui-text);
}
.normative-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
.normative-facts label {
  margin: 8px 0;
}
.normative-candidate {
  border-top: 1px solid var(--ui-border);
  padding-top: 12px;
  display: grid;
  gap: 8px;
}
.normative-choice {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}
.normative-choice input {
  margin-top: 4px;
}
.normative-saved,
.normative-hint {
  background: var(--ui-bg-muted);
  padding: 8px;
  border-radius: 6px;
}
.normative-resource {
  padding: 8px 0;
  border-bottom: 1px solid var(--ui-border);
}
.normative-text {
  white-space: pre-wrap;
  font-size: 13px;
}
ul {
  padding-left: 18px;
  list-style: disc;
}
a {
  color: var(--ui-primary);
  text-decoration: underline;
}
small {
  color: var(--ui-text-muted);
}
@media (max-width: 600px) {
  .normative-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>

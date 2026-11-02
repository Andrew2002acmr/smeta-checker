<script setup lang="ts">
import type {
  NormativeCandidate,
  NormativeCorpusAdminDto,
  NormativeManagerDto,
  NormativeVorCheckDto,
  NormativeVorRowDto,
} from '~/generated/types.gen'

type Profile = 'electrical' | 'roads'
type Section = 'search' | 'vor' | 'manager'

const backend = useBackend()
const section = ref<Section>('search')
const profile = ref<Profile>('electrical')
const kind = ref<'GESN' | 'SP' | 'GOST' | 'PUE' | 'ALL'>('GESN')
const query = ref('')
const searchBusy = ref(false)
const searchError = ref('')
const candidates = ref<NormativeCandidate[]>([])
const searchIdentity = ref<Record<string, string> | null>(null)
const searched = ref(false)

const vorInput = ref<HTMLInputElement | null>(null)
const vorFile = ref<File | null>(null)
const vorName = ref('')
const vorBusy = ref(false)
const vorDrag = ref(false)
const vorError = ref('')
const vorResult = ref<NormativeVorCheckDto | null>(null)
const rowFilter = ref('')
const statusFilter = ref<'all' | 'matched' | 'review' | 'issues'>('all')

const manager = ref<NormativeManagerDto | null>(null)
const managerBusy = ref(false)
const managerError = ref('')
const managerLoaded = ref(false)
const expandedProfile = ref<Profile | null>(null)

const tabs: { id: Section; label: string; icon: string; description: string }[] = [
  {
    id: 'search',
    label: 'Поиск нормативов',
    icon: 'i-lucide-search',
    description: 'Найдите расценку по коду или названию работы.',
  },
  {
    id: 'vor',
    label: 'Проверка ВОР по ГЭСН',
    icon: 'i-lucide-file-check-2',
    description: 'Сопоставьте позиции ВОР с кодами и измерителями норм.',
  },
  {
    id: 'manager',
    label: 'Сборники',
    icon: 'i-lucide-library',
    description: 'Каталог PDF, выпуски, полнота импорта и ошибки распознавания.',
  },
]
const kindItems = [
  { label: 'ГЭСН', value: 'GESN' },
  { label: 'СП', value: 'SP' },
  { label: 'ГОСТ', value: 'GOST' },
  { label: 'ПУЭ', value: 'PUE' },
  { label: 'Все требования', value: 'ALL' },
]
const profileItems = [
  { label: 'Электрика', value: 'electrical' },
  { label: 'Дорожные работы', value: 'roads' },
]
const visibleRows = computed(() => {
  const q = rowFilter.value.trim().toLocaleLowerCase('ru')
  return (vorResult.value?.rows ?? []).filter((row) => {
    const status = row.status
    const statusMatches =
      statusFilter.value === 'all' ||
      (statusFilter.value === 'matched' && status === 'matched') ||
      (statusFilter.value === 'review' && status === 'review') ||
      (statusFilter.value === 'issues' &&
        ['unit_mismatch', 'name_mismatch', 'not_found'].includes(status))
    const queryMatches =
      !q ||
      `${row.number} ${row.work_name} ${row.source_code} ${row.candidates.map((item) => `${item.code} ${item.title}`).join(' ')}`
        .toLocaleLowerCase('ru')
        .includes(q)
    return statusMatches && queryMatches
  })
})
const vorCounts = computed(() => {
  const rows = vorResult.value?.rows ?? []
  return {
    matched: rows.filter((row) => row.status === 'matched').length,
    review: rows.filter((row) => row.status === 'review').length,
    issues: rows.filter((row) =>
      ['unit_mismatch', 'name_mismatch', 'not_found'].includes(row.status),
    ).length,
  }
})

function profileName(value: Profile) {
  return value === 'roads' ? 'Дорожные нормы' : 'Электротехнические нормы'
}

async function searchNorms() {
  const text = query.value.trim()
  if (text.length < 2 || searchBusy.value) return
  searchBusy.value = true
  searchError.value = ''
  searched.value = true
  try {
    const response = await backend.searchNormativeLibrary(profile.value, {
      query: text,
      kind: kind.value,
      mode: 'lexical',
      facts: {},
    })
    candidates.value = response.candidates
    searchIdentity.value = response.identity
  } catch (error) {
    searchError.value = readableError(error)
    candidates.value = []
  } finally {
    searchBusy.value = false
  }
}

function chooseProfile(value: string) {
  if (value !== 'electrical' && value !== 'roads') return
  profile.value = value
  candidates.value = []
  searched.value = false
  searchIdentity.value = null
  searchError.value = ''
}

function sourceUrl(sourceId: string, forProfile = profile.value) {
  return backend.normativeSourceUrl(forProfile, sourceId)
}

function errorMessage(error: unknown) {
  return readableError(error)
}

function rowStatus(row: NormativeVorRowDto) {
  const labels: Record<NormativeVorRowDto['status'], string> = {
    matched: 'Код и единица найдены',
    unit_mismatch: 'Единица не совпадает',
    name_mismatch: 'Название стоит сверить',
    review: 'Нужна ручная сверка',
    not_found: 'Кандидаты не найдены',
  }
  return labels[row.status]
}

function rowColor(row: NormativeVorRowDto): 'success' | 'warning' | 'error' | 'neutral' {
  if (row.status === 'matched') return 'success'
  if (
    row.status === 'unit_mismatch' ||
    row.status === 'name_mismatch' ||
    row.status === 'not_found'
  )
    return 'error'
  return 'warning'
}

async function sendVor(file: File) {
  if (!/\.xlsx$/i.test(file.name)) {
    vorError.value = 'Загрузите ведомость в формате XLSX.'
    return
  }
  if (file.size > 100 * 1024 * 1024) {
    vorError.value = 'Размер файла не должен превышать 100 МБ.'
    return
  }
  vorFile.value = file
  vorName.value = file.name
  vorBusy.value = true
  vorError.value = ''
  vorResult.value = null
  const form = new FormData()
  form.append('file', file)
  form.append('profile', profile.value)
  try {
    vorResult.value = await backend.checkNormativeVor(form)
  } catch (error) {
    vorError.value = errorMessage(error)
  } finally {
    vorBusy.value = false
  }
}

function onFilePicked(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (file) void sendVor(file)
}

function onDrop(event: DragEvent) {
  event.preventDefault()
  vorDrag.value = false
  const files = [...(event.dataTransfer?.files ?? [])]
  if (files.length !== 1) {
    vorError.value = 'Перетащите один файл ВОР в формате XLSX.'
    return
  }
  void sendVor(files[0]!)
}

function clearVor() {
  vorFile.value = null
  vorName.value = ''
  vorResult.value = null
  vorError.value = ''
  rowFilter.value = ''
  statusFilter.value = 'all'
}

async function loadManager(force = false) {
  if (managerBusy.value || (managerLoaded.value && !force)) return
  managerBusy.value = true
  managerError.value = ''
  try {
    manager.value = await backend.normativeManager()
    managerLoaded.value = true
  } catch (error) {
    managerError.value = errorMessage(error)
  } finally {
    managerBusy.value = false
  }
}

function activate(value: Section) {
  section.value = value
  if (value === 'manager') void loadManager()
}

function compactReason(reason: string) {
  const known: Record<string, string> = {
    resource_quantity_unparsed: 'Не распознано количество ресурса',
    unit_unparsed: 'Не распознан измеритель',
    source_text_missing: 'Нет читаемого текста источника',
  }
  return known[reason] ?? reason.replaceAll('_', ' ')
}

function profileData(value: Profile): NormativeCorpusAdminDto | undefined {
  return manager.value?.profiles.find((item) => item.profile === value)
}

function fileSize(bytes: number) {
  if (bytes >= 1024 * 1024) {
    return `${(bytes / (1024 * 1024)).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} МБ`
  }
  return `${Math.max(1, Math.round(bytes / 1024))} КБ`
}

function openPdf(profileValue: Profile, sourceId: string, page?: number | null) {
  const suffix = page ? `#page=${page}` : ''
  window.open(sourceUrl(sourceId, profileValue) + suffix, '_blank', 'noopener')
}

function openCandidateCitation(sourceId: string, page: number) {
  openPdf(profile.value, sourceId, page)
}
</script>

<template>
  <section class="normative-workbench" aria-label="Проверка по нормативам">
    <div class="workbench-heading">
      <div>
        <h1>Проверка по нормативам</h1>
        <p class="muted">Поиск расценок, сверка ВОР и контроль подключённых корпусов.</p>
      </div>
      <UBadge color="success" variant="soft" label="Локальная нормативная база" />
    </div>

    <div class="workbench-tabs" role="tablist" aria-label="Разделы проверки по нормативам">
      <button
        v-for="tab in tabs"
        :key="tab.id"
        type="button"
        role="tab"
        :aria-selected="section === tab.id"
        :aria-controls="`normative-panel-${tab.id}`"
        :id="`normative-tab-${tab.id}`"
        :class="{ 'is-active': section === tab.id }"
        @click="activate(tab.id)"
      >
        <UIcon :name="tab.icon" aria-hidden="true" />
        <span>{{ tab.label }}</span>
      </button>
    </div>

    <section
      v-if="section === 'search'"
      id="normative-panel-search"
      role="tabpanel"
      aria-labelledby="normative-tab-search"
      class="workbench-panel"
    >
      <div class="section-intro">
        <div>
          <h2>Поиск нормативов</h2>
          <p class="muted">
            Введите код ГЭСН или название работы. Поиск выполняется по выбранному корпусу.
          </p>
        </div>
      </div>
      <form class="search-form" @submit.prevent="searchNorms">
        <USelect
          v-model="profile"
          :items="profileItems"
          aria-label="Нормативный корпус"
          class="profile-select"
          @update:model-value="chooseProfile"
        />
        <USelect
          v-model="kind"
          :items="kindItems"
          aria-label="Тип нормативного источника"
          class="kind-select"
        />
        <UInput
          v-model="query"
          icon="i-lucide-search"
          placeholder="Код ГЭСН или название работы"
          aria-label="Код или название работы"
          class="query-input"
        />
        <UButton
          type="submit"
          icon="i-lucide-search"
          :loading="searchBusy"
          :disabled="query.trim().length < 2"
          >Найти</UButton
        >
      </form>
      <p v-if="searchError" class="error-box" role="alert">{{ searchError }}</p>
      <div v-if="searchBusy" class="empty" role="status">Ищем в нормативном корпусе…</div>
      <div v-else-if="searched && candidates.length === 0" class="empty" role="status">
        Ничего не найдено. Попробуйте изменить запрос или выбрать другой тип источника.
      </div>
      <div v-else-if="candidates.length" class="search-results" aria-live="polite">
        <div class="result-summary">
          <span
            >Найдено вариантов: <strong>{{ candidates.length }}</strong></span
          >
          <span v-if="searchIdentity" class="muted">Выпуск {{ searchIdentity.release_id }}</span>
        </div>
        <article v-for="item in candidates" :key="item.id" class="norm-card">
          <div class="norm-card-top">
            <div>
              <p class="norm-code">{{ item.code || 'Пункт документа' }}</p>
              <h3>{{ item.title }}</h3>
            </div>
            <UBadge
              :color="
                item.status === 'possible_mismatch'
                  ? 'warning'
                  : item.status === 'candidate'
                    ? 'success'
                    : 'neutral'
              "
              variant="soft"
              :label="
                item.status === 'possible_mismatch'
                  ? 'Параметры не совпали'
                  : item.status === 'needs_facts'
                    ? 'Нужны уточнения'
                    : 'Кандидат'
              "
            />
          </div>
          <div class="norm-meta">
            <span><strong>Измеритель:</strong> {{ item.unit_raw || 'не распознан' }}</span>
            <span><strong>Сборник:</strong> {{ item.designation }}</span>
            <span v-if="item.edition_status"
              ><strong>Редакция:</strong> {{ item.edition_status }}</span
            >
          </div>
          <div v-if="item.checks.length" class="norm-conditions">
            <p
              v-for="check in item.checks"
              :key="check.label"
              :class="{ 'is-mismatch': check.state === 'mismatch' }"
            >
              <UIcon
                :name="
                  check.state === 'match'
                    ? 'i-lucide-circle-check'
                    : check.state === 'mismatch'
                      ? 'i-lucide-circle-x'
                      : 'i-lucide-circle-help'
                "
                aria-hidden="true"
              />
              {{ check.label }}: {{ check.actual ?? 'не указано'
              }}<template v-if="check.expected"> · условие нормы {{ check.expected }}</template>
            </p>
          </div>
          <p v-if="item.missing_facts.length" class="muted norm-missing">
            Для проверки применения: {{ item.missing_facts.join(' · ') }}.
          </p>
          <details v-if="item.composition.length" class="norm-details">
            <summary>Состав работ</summary>
            <ul>
              <li v-for="(operation, index) in item.composition" :key="index">{{ operation }}</li>
            </ul>
          </details>
          <div class="norm-citations">
            <button
              v-for="citation in item.citations"
              :key="`${citation.source_id}:${citation.page}`"
              type="button"
              class="source-link"
              @click="openCandidateCitation(citation.source_id, citation.page)"
            >
              <UIcon name="i-lucide-file-text" aria-hidden="true" /> {{ citation.designation }} ·
              стр. {{ citation.page }}
            </button>
            <span v-if="!item.citations.length" class="muted">Страница источника не указана.</span>
          </div>
        </article>
      </div>
      <p class="workbench-footnote">
        Поиск выдаёт кандидатов. Редакцию и применимость нормы к объекту нужно подтвердить по
        источнику.
      </p>
    </section>

    <section
      v-else-if="section === 'vor'"
      id="normative-panel-vor"
      role="tabpanel"
      aria-labelledby="normative-tab-vor"
      class="workbench-panel"
    >
      <div class="section-intro">
        <div>
          <h2>Проверка ВОР по ГЭСН</h2>
          <p class="muted">
            Сверяем код, измеритель и наименование с выбранным корпусом. Результат требует проверки
            инженером.
          </p>
        </div>
        <USelect
          v-model="profile"
          :items="profileItems"
          aria-label="Корпус для проверки ВОР"
          class="profile-select"
          :disabled="vorBusy"
        />
      </div>
      <p v-if="vorError" class="error-box" role="alert">{{ vorError }}</p>
      <div
        v-if="!vorResult"
        class="vor-drop"
        :class="{ 'is-dragging': vorDrag }"
        @dragenter.prevent="vorDrag = true"
        @dragover.prevent="vorDrag = true"
        @dragleave.prevent="vorDrag = false"
        @drop="onDrop"
      >
        <UIcon name="i-lucide-upload-cloud" class="drop-icon" aria-hidden="true" />
        <h3>{{ vorBusy ? 'Проверяем ведомость…' : 'Перетащите файл ВОР сюда' }}</h3>
        <p class="muted">XLSX · до 100 МБ · исходный файл не сохраняется</p>
        <input
          ref="vorInput"
          type="file"
          accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
          class="file-input"
          aria-label="Выбрать файл ВОР"
          @change="onFilePicked"
        />
        <UButton
          icon="i-lucide-file-spreadsheet"
          color="neutral"
          variant="outline"
          :loading="vorBusy"
          :disabled="vorBusy"
          @click="vorInput?.click()"
          >Добавить ВОР</UButton
        >
      </div>
      <template v-else>
        <div class="vor-file-line">
          <UIcon name="i-lucide-file-spreadsheet" aria-hidden="true" />
          <strong>{{ vorName }}</strong>
          <span class="muted">{{ profileName(vorResult.profile) }}</span>
          <UButton color="neutral" variant="ghost" size="sm" icon="i-lucide-x" @click="clearVor"
            >Другой файл</UButton
          >
        </div>
        <div class="vor-summary">
          <button
            type="button"
            :class="{ active: statusFilter === 'all' }"
            @click="statusFilter = 'all'"
          >
            <strong>{{ vorResult.total_positions }}</strong
            ><span>Всего позиций</span>
          </button>
          <button
            type="button"
            :class="{ active: statusFilter === 'matched' }"
            @click="statusFilter = 'matched'"
          >
            <strong>{{ vorCounts.matched }}</strong
            ><span>Код и единица найдены</span>
          </button>
          <button
            type="button"
            :class="{ active: statusFilter === 'review' }"
            @click="statusFilter = 'review'"
          >
            <strong>{{ vorCounts.review }}</strong
            ><span>Нужна сверка</span>
          </button>
          <button
            type="button"
            :class="{ active: statusFilter === 'issues' }"
            @click="statusFilter = 'issues'"
          >
            <strong>{{ vorCounts.issues }}</strong
            ><span>Расхождения / без кандидата</span>
          </button>
        </div>
        <p v-if="vorResult.truncated" class="warning-box" role="status">
          Показаны первые {{ vorResult.returned_positions }} строк из
          {{ vorResult.total_positions }}. Для безопасности обработка ограничена 200 позициями за
          файл.
        </p>
        <div class="results-toolbar">
          <UInput
            v-model="rowFilter"
            icon="i-lucide-search"
            placeholder="Найти позицию или норму…"
            aria-label="Поиск по результатам сверки"
          />
          <span class="muted">{{ visibleRows.length }} строк</span>
        </div>
        <div v-if="!visibleRows.length" class="empty">В выбранном фильтре строк нет.</div>
        <article v-for="row in visibleRows" :key="row.index" class="vor-row">
          <div class="vor-row-heading">
            <UBadge color="neutral" variant="soft" :label="row.number || String(row.index + 1)" />
            <div class="vor-work">
              <strong>{{ row.work_name }}</strong
              ><span class="muted">{{ row.volume }} {{ row.unit }}</span>
            </div>
            <UBadge :color="rowColor(row)" variant="soft" :label="rowStatus(row)" />
          </div>
          <div class="vor-checks">
            <span v-if="row.source_code"
              ><strong>Код ВОР:</strong> {{ row.source_code }} ·
              {{ row.code_match ? 'найден' : 'не найден' }}</span
            >
            <span v-if="row.unit_match !== null"
              ><strong>Единица:</strong> {{ row.unit_match ? 'совпадает' : 'расхождение' }}</span
            >
            <span v-if="row.name_similarity !== null"
              ><strong>Сходство названия:</strong> {{ row.name_similarity }}% · справочно</span
            >
          </div>
          <div v-if="row.candidates.length" class="vor-candidates">
            <div v-for="candidate in row.candidates" :key="candidate.id" class="vor-candidate">
              <div>
                <strong>{{ candidate.code }}</strong> · {{ candidate.title
                }}<small>{{ candidate.unit_raw }} · {{ candidate.designation }}</small>
              </div>
              <button
                v-for="citation in candidate.citations.slice(0, 2)"
                :key="`${citation.source_id}:${citation.page}`"
                type="button"
                class="source-link"
                @click="openPdf(vorResult.profile, citation.source_id, citation.page)"
              >
                Источник · стр. {{ citation.page }}
              </button>
            </div>
          </div>
          <p v-else class="muted">По названию работы совпадений в выбранном корпусе не найдено.</p>
        </article>
      </template>
      <p class="workbench-footnote">
        Сходство наименований — лексическая подсказка. Автоматическое совпадение не подтверждает
        инженерную применимость расценки.
      </p>
    </section>

    <section
      v-else
      id="normative-panel-manager"
      role="tabpanel"
      aria-labelledby="normative-tab-manager"
      class="workbench-panel"
    >
      <div class="section-intro">
        <div>
          <h2>Сборники</h2>
          <p class="muted">
            Каталог исходных PDF по типам и разделам. Файлы из текущих выпусков отмечены отдельно.
          </p>
        </div>
        <UButton
          icon="i-lucide-refresh-cw"
          color="neutral"
          variant="outline"
          :loading="managerBusy"
          @click="loadManager(true)"
          >Обновить</UButton
        >
      </div>
      <p v-if="managerError" class="error-box" role="alert">{{ managerError }}</p>
      <div v-if="managerBusy && !manager" class="empty" role="status">
        Загружаем сведения о корпусах…
      </div>
      <div v-else-if="manager" class="manager-profiles">
        <section v-for="item in manager.profiles" :key="item.profile" class="manager-card">
          <div class="manager-card-heading">
            <div>
              <p class="eyebrow">
                {{ item.profile === 'roads' ? 'ДОРОЖНЫЙ ПРОФИЛЬ' : 'ЭЛЕКТРОТЕХНИЧЕСКИЙ ПРОФИЛЬ' }}
              </p>
              <h3>{{ item.title }}</h3>
            </div>
            <UBadge color="success" variant="soft" :label="`Выпуск ${item.identity.release_id}`" />
          </div>
          <div class="manager-metrics">
            <div>
              <strong>{{ item.counts.sources }}</strong
              ><span>источников</span>
            </div>
            <div>
              <strong>{{ item.counts.pages }}</strong
              ><span>страниц</span>
            </div>
            <div>
              <strong>{{ item.counts.norms }}</strong
              ><span>норм</span>
            </div>
            <div>
              <strong>{{ item.counts.review_queue }}</strong
              ><span>ошибок распознавания</span>
            </div>
          </div>
          <p class="muted">
            SHA-256 выпуска: <code>{{ item.identity.sha256 }}</code>
          </p>
          <button
            type="button"
            class="manager-expand"
            :aria-expanded="expandedProfile === item.profile"
            @click="expandedProfile = expandedProfile === item.profile ? null : item.profile"
          >
            <span>Редакции, источники и ошибки</span
            ><UIcon
              :name="
                expandedProfile === item.profile ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'
              "
              aria-hidden="true"
            />
          </button>
          <div v-if="expandedProfile === item.profile" class="manager-details">
            <h4>Редакции корпуса</h4>
            <div v-for="release in item.releases" :key="release.release_id" class="release-row">
              <div>
                <strong>{{ release.release_id }}</strong
                ><span
                  >{{ release.observed_on || 'Дата не указана' }} ·
                  {{ release.parser_version }}</span
                >
              </div>
              <UBadge
                :color="
                  release.integrity === 'ok' && release.foreign_keys === 'ok' ? 'success' : 'error'
                "
                variant="soft"
                :label="
                  release.current ? 'Текущий · целостность OK' : `Архив · ${release.integrity}`
                "
              />
            </div>
            <p class="muted">
              Инженерная проверка:
              {{
                item.releases.find((release) => release.current)?.engineering_validation ??
                'нет данных'
              }}. Полнота редакций не подтверждается одной проверкой целостности.
            </p>
            <details class="manager-list">
              <summary>Источники · {{ item.sources.length }}</summary>
              <ul>
                <li v-for="source in item.sources" :key="source.id">
                  <a
                    :href="sourceUrl(source.id, item.profile)"
                    class="source-link manager-source-link"
                    :aria-label="`Открыть PDF: ${source.designation}`"
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    <UIcon name="i-lucide-file-text" aria-hidden="true" />
                    {{ source.designation }}</a
                  ><span
                    >{{ source.pages }} стр. · читаемый текст:
                    {{ source.readable_pages }} стр.</span
                  >
                </li>
              </ul>
            </details>
            <details class="manager-list">
              <summary>Пробелы корпуса · {{ item.gaps.length }}</summary>
              <ul>
                <li v-for="(gap, index) in item.gaps" :key="index">
                  <strong>{{ gap.designation || 'Пробел' }}</strong
                  ><span>{{ gap.reason }}</span>
                </li>
              </ul>
            </details>
            <details class="manager-list">
              <summary>
                Очередь распознавания · первые {{ item.review_queue.length }} из
                {{ item.review_queue_total }}
              </summary>
              <ul>
                <li v-for="issue in item.review_queue" :key="issue.id">
                  <button
                    v-if="issue.source_id"
                    type="button"
                    class="source-link"
                    @click="openPdf(item.profile, issue.source_id, issue.page)"
                  >
                    {{ issue.designation || issue.entity_id
                    }}<template v-if="issue.page"> · стр. {{ issue.page }}</template></button
                  ><strong v-else>{{ issue.entity_id }}</strong
                  ><span>{{ compactReason(issue.reason) }}</span>
                </li>
              </ul>
            </details>
          </div>
        </section>
      </div>
      <section v-if="manager?.collections.length" class="manager-catalog">
        <div class="manager-catalog-heading">
          <h3>Каталог PDF</h3>
          <p class="muted">
            Источники из папки <code>base</code>. Совпадающие по SHA-256 с нормативной базой
            помечены как импортированные.
          </p>
        </div>
        <details v-for="group in manager.collections" :key="group.kind" class="manager-list">
          <summary>{{ group.kind }} · {{ group.file_count }} сборников</summary>
          <details
            v-for="section in group.sections"
            :key="section.title"
            class="manager-catalog-section"
          >
            <summary>{{ section.title }} · {{ section.files.length }}</summary>
            <ul>
              <li v-for="file in section.files" :key="file.id">
                <a
                  :href="backend.normativeCatalogPdfUrl(file.id)"
                  class="source-link manager-source-link"
                  :aria-label="`Открыть PDF: ${file.designation}`"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <UIcon name="i-lucide-file-text" aria-hidden="true" />
                  {{ file.designation }}
                </a>
                <span class="catalog-file-meta">
                  {{ fileSize(file.size_bytes) }}
                  <UBadge v-if="file.imported" color="success" variant="soft" label="Уже в базе" />
                </span>
              </li>
            </ul>
          </details>
        </details>
      </section>
      <p v-else-if="manager && !manager.collections.length" class="empty">
        Каталог PDF не найден. Подключите папку base к API и обновите страницу.
      </p>
      <section class="manager-import">
        <div>
          <UIcon name="i-lucide-shield-check" aria-hidden="true" />
          <div>
            <h3>Добавление в нормативную базу</h3>
            <p>
              PDF, помещённый в соответствующий раздел папки base, появится в каталоге после
              обновления. Чтобы искать его нормы, документ нужно включить в проверенный выпуск
              нормативной базы через импорт.
            </p>
          </div>
        </div>
        <UBadge color="neutral" variant="soft" label="Импорт через сервер" />
      </section>
      <p class="workbench-footnote">
        Статус «целостность OK» проверяет файлы и структуру базы, но не подтверждает актуальность
        редакций или применимость норм.
      </p>
    </section>
  </section>
</template>

<style scoped>
.normative-workbench {
  padding-top: 24px;
}
.workbench-heading,
.section-intro,
.manager-card-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
}
.workbench-heading {
  margin-bottom: 22px;
}
.workbench-heading h1 {
  margin: 0 0 6px;
  font-size: 28px;
  font-weight: 750;
  letter-spacing: -0.025em;
  line-height: 1.2;
}
.workbench-heading p,
.section-intro p {
  margin: 0;
}
.workbench-tabs {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  padding: 6px;
  margin-bottom: 18px;
  border: 1px solid var(--ui-border);
  border-radius: 10px;
  background: var(--ui-bg-muted);
}
.workbench-tabs button {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 9px;
  min-height: 44px;
  padding: 9px 12px;
  border: 1px solid transparent;
  border-radius: 7px;
  background: transparent;
  color: var(--ui-text-muted);
  font: inherit;
  font-size: 13px;
  font-weight: 650;
  cursor: pointer;
}
.workbench-tabs button:hover {
  color: var(--ui-text-highlighted);
}
.workbench-tabs button.is-active {
  border-color: var(--ui-border);
  background: var(--ui-bg);
  color: var(--app-selected-text);
  box-shadow: 0 1px 2px rgb(0 0 0 / 5%);
}
.workbench-tabs :deep(svg) {
  width: 17px;
  height: 17px;
}
.workbench-panel {
  padding: 22px;
  border: 1px solid var(--ui-border);
  border-radius: 10px;
  background: var(--ui-bg);
}
.section-intro {
  align-items: flex-start;
  margin-bottom: 18px;
}
.section-intro h2 {
  margin: 0 0 5px;
  font-size: 19px;
  font-weight: 700;
}
.search-form {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-bottom: 18px;
}
.profile-select {
  width: 180px;
  flex: 0 0 auto;
}
.kind-select {
  width: 155px;
  flex: 0 0 auto;
}
.query-input {
  flex: 1;
  min-width: 160px;
}
.search-results {
  display: grid;
  gap: 12px;
}
.result-summary {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  font-size: 13px;
}
.norm-card,
.vor-row,
.manager-card {
  border: 1px solid var(--ui-border);
  border-radius: 8px;
  background: var(--ui-bg);
}
.norm-card {
  padding: 16px;
}
.norm-card-top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.norm-code {
  margin: 0 0 4px;
  color: var(--ui-primary);
  font-size: 12px;
  font-weight: 750;
}
.norm-card h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 650;
  line-height: 1.45;
}
.norm-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 18px;
  margin-top: 12px;
  color: var(--ui-text-muted);
  font-size: 12px;
}
.norm-meta strong {
  color: var(--ui-text);
}
.norm-conditions {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 5px 14px;
  margin-top: 10px;
}
.norm-conditions p {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin: 0;
  color: var(--ui-text-muted);
  font-size: 12px;
}
.norm-conditions :deep(svg) {
  flex: 0 0 14px;
  width: 14px;
  height: 14px;
  margin-top: 2px;
  color: var(--ui-success);
}
.norm-conditions p.is-mismatch :deep(svg) {
  color: var(--ui-error);
}
.norm-missing {
  margin: 10px 0 0;
  font-size: 12px;
}
.norm-details {
  margin-top: 12px;
  font-size: 13px;
}
.norm-details summary,
.manager-list summary {
  color: var(--ui-primary);
  font-weight: 650;
  cursor: pointer;
}
.norm-details ul {
  margin: 8px 0 0;
  padding-left: 20px;
  color: var(--ui-text-muted);
}
.norm-citations {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 14px;
  margin-top: 13px;
  padding-top: 11px;
  border-top: 1px solid var(--ui-border-muted);
}
.source-link {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--ui-primary);
  font: inherit;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}
.source-link:hover {
  text-decoration: underline;
}
.source-link :deep(svg) {
  width: 14px;
  height: 14px;
}
.workbench-footnote {
  margin: 16px 0 0;
  color: var(--ui-text-muted);
  font-size: 12px;
  line-height: 1.6;
}
.vor-drop {
  min-height: 270px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 30px 20px;
  border: 1px dashed var(--ui-border-accented);
  border-radius: 9px;
  text-align: center;
  transition:
    background 0.15s,
    border-color 0.15s;
}
.vor-drop.is-dragging {
  border-color: var(--ui-primary);
  background: var(--app-selected-bg);
}
.vor-drop h3 {
  margin: 0;
  font-size: 17px;
  font-weight: 700;
}
.vor-drop p {
  margin: 0 0 6px;
  font-size: 13px;
}
.drop-icon {
  width: 38px;
  height: 38px;
  color: var(--ui-primary);
}
.file-input {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
}
.vor-file-line {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 50px;
  padding: 8px 12px;
  border: 1px solid var(--ui-border);
  border-radius: 7px;
}
.vor-file-line > strong {
  min-width: 0;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.vor-summary {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  margin: 14px 0;
  border: 1px solid var(--ui-border);
  border-radius: 8px;
  overflow: hidden;
}
.vor-summary button {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 13px 16px;
  border: 0;
  border-left: 1px solid var(--ui-border-muted);
  background: transparent;
  color: var(--ui-text-muted);
  text-align: left;
  cursor: pointer;
}
.vor-summary button:first-child {
  border-left: 0;
}
.vor-summary button.active {
  background: var(--app-selected-bg);
  color: var(--app-selected-text);
}
.vor-summary strong {
  color: var(--ui-text-highlighted);
  font-size: 23px;
  font-variant-numeric: tabular-nums;
}
.vor-summary span {
  font-size: 11px;
  line-height: 1.4;
}
.results-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.results-toolbar :deep(input) {
  min-width: 220px;
}
.vor-row {
  padding: 14px;
  margin-top: 10px;
}
.vor-row-heading {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}
.vor-work {
  display: flex;
  flex: 1;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
  font-size: 13px;
}
.vor-work strong {
  line-height: 1.5;
}
.vor-work span {
  font-size: 12px;
}
.vor-checks {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  margin: 10px 0 0 34px;
  color: var(--ui-text-muted);
  font-size: 11px;
}
.vor-candidates {
  display: grid;
  gap: 7px;
  margin: 11px 0 0 34px;
}
.vor-candidate {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 9px 10px;
  border-radius: 6px;
  background: var(--ui-bg-muted);
  font-size: 12px;
  line-height: 1.5;
}
.vor-candidate > div {
  min-width: 0;
}
.vor-candidate small {
  display: block;
  color: var(--ui-text-muted);
}
.vor-row > p {
  margin: 10px 0 0 34px;
  font-size: 12px;
}
.manager-profiles {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}
.manager-card {
  min-width: 0;
  padding: 16px;
}
.manager-card-heading {
  align-items: flex-start;
}
.manager-card-heading h3 {
  margin: 0;
  font-size: 17px;
}
.eyebrow {
  margin: 0 0 5px;
  color: var(--ui-text-muted);
  font-size: 10px;
  font-weight: 750;
  letter-spacing: 0.08em;
}
.manager-metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 6px;
  margin: 16px 0 10px;
}
.manager-metrics div {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
}
.manager-metrics strong {
  font-size: 19px;
  font-variant-numeric: tabular-nums;
}
.manager-metrics span {
  color: var(--ui-text-muted);
  font-size: 10px;
  line-height: 1.3;
}
.manager-card > p {
  overflow-wrap: anywhere;
  font-size: 10px;
}
.manager-card code {
  font-size: 10px;
}
.manager-expand {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 10px 0 0;
  border: 0;
  border-top: 1px solid var(--ui-border-muted);
  background: transparent;
  color: var(--ui-primary);
  font: inherit;
  font-size: 12px;
  font-weight: 650;
  text-align: left;
  cursor: pointer;
}
.manager-details {
  padding-top: 10px;
}
.manager-details h4 {
  margin: 10px 0 7px;
  font-size: 13px;
}
.release-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 7px 0;
  border-bottom: 1px solid var(--ui-border-muted);
  font-size: 11px;
}
.release-row div {
  display: flex;
  min-width: 0;
  flex-direction: column;
}
.release-row span {
  color: var(--ui-text-muted);
}
.manager-details > p {
  font-size: 11px;
}
.manager-list {
  padding: 9px 0;
  border-top: 1px solid var(--ui-border-muted);
  font-size: 12px;
}
.manager-catalog {
  margin: 22px 0 8px;
}
.manager-catalog-heading {
  margin-bottom: 8px;
}
.manager-catalog-heading h3 {
  margin: 0 0 4px;
  font-size: 15px;
}
.manager-catalog-heading p {
  margin: 0;
  font-size: 12px;
}
.manager-catalog-section {
  margin: 8px 0 0 12px;
  padding-left: 12px;
  border-left: 1px solid var(--ui-border-muted);
}
.manager-list ul {
  display: grid;
  gap: 8px;
  max-height: 240px;
  overflow: auto;
  margin: 9px 0 0;
  padding: 0;
  list-style: none;
}
.manager-list li {
  display: grid;
  gap: 2px;
}
.manager-list li span {
  color: var(--ui-text-muted);
  font-size: 11px;
}
.manager-source-link {
  max-width: 100%;
  white-space: normal;
  overflow-wrap: anywhere;
  line-height: 1.5;
}
.catalog-file-meta {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.manager-import {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  margin-top: 14px;
  padding: 16px;
  border: 1px solid var(--ui-border);
  border-radius: 8px;
  background: var(--ui-bg-muted);
}
.manager-import > div {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}
.manager-import > div > :deep(svg) {
  width: 22px;
  height: 22px;
  flex: 0 0 auto;
  color: var(--ui-primary);
}
.manager-import h3 {
  margin: 0 0 4px;
  font-size: 14px;
}
.manager-import p {
  margin: 0;
  color: var(--ui-text-muted);
  font-size: 12px;
  line-height: 1.55;
}
.empty {
  padding: 24px 12px;
  color: var(--ui-text-muted);
  text-align: center;
}
@media (max-width: 900px) {
  .workbench-tabs button {
    justify-content: flex-start;
    font-size: 12px;
  }
  .manager-profiles {
    grid-template-columns: 1fr;
  }
}
@media (max-width: 700px) {
  .workbench-heading,
  .section-intro {
    align-items: flex-start;
    flex-direction: column;
  }
  .workbench-heading h1 {
    font-size: 24px;
  }
  .workbench-tabs {
    grid-template-columns: 1fr;
  }
  .workbench-tabs button {
    justify-content: flex-start;
  }
  .search-form {
    align-items: stretch;
    flex-wrap: wrap;
  }
  .profile-select,
  .kind-select {
    width: calc(50% - 5px);
  }
  .query-input {
    flex-basis: 100%;
    order: -1;
  }
  .vor-summary {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .vor-summary button:nth-child(3) {
    border-left: 0;
    border-top: 1px solid var(--ui-border-muted);
  }
  .vor-summary button:nth-child(4) {
    border-top: 1px solid var(--ui-border-muted);
  }
  .vor-file-line {
    flex-wrap: wrap;
  }
  .vor-file-line > strong {
    flex-basis: calc(100% - 34px);
  }
  .vor-file-line > .muted {
    margin-left: 30px;
  }
  .vor-row-heading {
    flex-wrap: wrap;
  }
  .vor-row-heading > :last-child {
    margin-left: 34px;
  }
  .vor-candidates,
  .vor-checks {
    margin-left: 0;
  }
  .vor-candidate {
    flex-direction: column;
  }
  .manager-import {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>

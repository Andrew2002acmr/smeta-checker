<script setup lang="ts">
import { onMounted } from 'vue'
import type { TableColumn, TabsItem } from '@nuxt/ui'
import type {
  NormativeCandidate,
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
watch(section, (value) => {
  if (import.meta.client) localStorage.setItem('normative-section', value)
  if (value === 'manager') void loadManager()
})
onMounted(() => {
  if (import.meta.client) {
    const saved = localStorage.getItem('normative-section') as Section | null
    if (saved && ['search', 'vor', 'manager'].includes(saved)) {
      section.value = saved
    }
  }
})
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
const catalogQuery = ref('')
const catalogType = ref('all')
const catalogPage = ref(1)
const catalogPageSize = 20
const catalogContainer = ref<HTMLElement | null>(null)

type CatalogRow = {
  designation: string
  kind: string
  number: string
  title: string
  url: string
  imported: boolean
}

const catalogRows = computed<CatalogRow[]>(() => {
  const hashes = new Set<string>()
  const rows: CatalogRow[] = []
  for (const group of manager.value?.collections ?? []) {
    for (const entry of group.sections) {
      for (const file of entry.files) {
        if (file.sha256) hashes.add(file.sha256)
        const match = file.designation.match(
          /^(?:Сборник\s+)?((?:ГЭСН[а-я]*|СП|ГОСТ(?:\s+Р)?)\s*\d[\w.-]*)\s+(.+)$/i,
        )
        rows.push({
          designation: file.designation,
          kind: group.kind,
          number: match?.[1] ?? '—',
          title: match?.[2] ?? file.designation,
          url: backend.normativeCatalogPdfUrl(file.id),
          imported: file.imported,
        })
      }
    }
  }
  const kindLabels: Record<string, string> = {
    GESN: 'ГЭСН',
    GOST: 'ГОСТ',
    SP: 'СП',
    PUE: 'ПУЭ',
  }
  for (const corpus of manager.value?.profiles ?? []) {
    for (const source of corpus.sources) {
      if (source.sha256 && hashes.has(source.sha256)) continue
      if (source.sha256) hashes.add(source.sha256)
      rows.push({
        designation: source.designation,
        kind: kindLabels[source.kind ?? ''] ?? (source.kind || 'Не указан'),
        number: source.designation,
        title: 'Наименование не указано',
        url: backend.normativeSourceUrl(corpus.profile, source.id),
        imported: true,
      })
    }
  }
  return rows
})
const catalogTypes = computed(() => [
  { label: 'Все типы документов', value: 'all' },
  ...[...new Set(catalogRows.value.map((row) => row.kind))]
    .toSorted((a, b) => a.localeCompare(b, 'ru'))
    .map((value) => ({ label: value, value })),
])
const filteredCatalog = computed(() => {
  const term = catalogQuery.value.trim().toLocaleLowerCase('ru').replace(/\s+/g, '')
  return catalogRows.value.filter(
    (row) =>
      (catalogType.value === 'all' || row.kind === catalogType.value) &&
      (!term ||
        `${row.designation} ${row.kind}`
          .toLocaleLowerCase('ru')
          .replace(/\s+/g, '')
          .includes(term)),
  )
})
const catalogPageRows = computed(() =>
  filteredCatalog.value.slice(
    (catalogPage.value - 1) * catalogPageSize,
    catalogPage.value * catalogPageSize,
  ),
)
watch([catalogQuery, catalogType], () => {
  catalogPage.value = 1
})
watch(catalogPage, async () => {
  await nextTick()
  catalogContainer.value?.querySelector('.catalog-table')?.scrollTo({ top: 0 })
})
watch(
  () => filteredCatalog.value.length,
  (count) => {
    catalogPage.value = Math.min(catalogPage.value, Math.max(1, Math.ceil(count / catalogPageSize)))
  },
)
const catalogColumns: TableColumn<CatalogRow>[] = [
  { accessorKey: 'number', header: 'Обозначение' },
  { accessorKey: 'title', header: 'Наименование' },
]

const tabs: TabsItem[] = [
  { value: 'search', label: 'Поиск нормативов', icon: 'i-lucide-search' },
  { value: 'vor', label: 'Проверка ВОР по ГЭСН', icon: 'i-lucide-file-check-2' },
  { value: 'manager', label: 'Документы', icon: 'i-lucide-library' },
]
const activeTab = computed({
  get: () => section.value,
  set: (value: string | number) => {
    if (value === 'search' || value === 'vor' || value === 'manager') activate(value)
  },
})
const kindItems = [
  { label: 'ГЭСН', value: 'GESN' },
  { label: 'СП', value: 'SP' },
  { label: 'ГОСТ', value: 'GOST' },
  { label: 'ПУЭ', value: 'PUE' },
  { label: 'Все требования', value: 'ALL' },
]
const profileItems = [
  { label: 'Электротехнические нормы', value: 'electrical' },
  { label: 'Дорожные нормы', value: 'roads' },
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
    if (force) {
      const toast = useToast()
      toast.add({
        title: 'Список обновлён',
        description: 'Новые редакции не загружались.',
        color: 'neutral',
        icon: 'i-lucide-refresh-cw',
      })
    }
  } catch (error) {
    managerError.value =
      error instanceof TypeError
        ? 'Проверьте подключение и повторите попытку.'
        : errorMessage(error)
  } finally {
    managerBusy.value = false
  }
}

function pluralize(count: number) {
  const mod10 = count % 10
  const mod100 = count % 100
  if (mod10 === 1 && mod100 !== 11) return ''
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return 'а'
  return 'ов'
}
function activate(value: Section) {
  section.value = value
}

function fileSize(bytes: number) {
  if (bytes >= 1024 * 1024) {
    return `${(bytes / (1024 * 1024)).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} МБ`
  }
  return `${Math.max(1, Math.round(bytes / 1024))} КБ`
}
function openCatalogRow(event: Event, file: CatalogRow) {
  if ((event.target as HTMLElement | null)?.closest('a')) return
  window.open(file.url, '_blank', 'noopener')
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
    </div>

    <UTabs
      v-model="activeTab"
      :items="tabs"
      aria-label="Разделы проверки по нормативам"
      class="workbench-tabs"
      :ui="{
        list: 'workbench-tab-list',
        indicator: 'bg-default ring ring-default shadow-sm',
        trigger: 'workbench-tab-trigger',
        content: 'workbench-tab-content',
      }"
    >
      <template #content>
        <section v-if="section === 'search'" class="workbench-panel">
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
              <span v-if="searchIdentity" class="muted"
                >Выпуск {{ searchIdentity.release_id }}</span
              >
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
              <section
                v-if="item.composition.length"
                class="norm-details"
                aria-label="Состав работ"
              >
                <h4>Состав работ</h4>
                <ul>
                  <li v-for="(operation, index) in item.composition" :key="index">
                    {{ operation }}
                  </li>
                </ul>
              </section>
              <div class="norm-citations">
                <button
                  v-for="citation in item.citations"
                  :key="`${citation.source_id}:${citation.page}`"
                  type="button"
                  class="source-link"
                  @click="openCandidateCitation(citation.source_id, citation.page)"
                >
                  <UIcon name="i-lucide-file-text" aria-hidden="true" />
                  {{ citation.designation }} · стр. {{ citation.page }}
                </button>
                <span v-if="!item.citations.length" class="muted"
                  >Страница источника не указана.</span
                >
              </div>
            </article>
          </div>
          <p class="workbench-footnote">
            Поиск выдаёт кандидатов. Редакцию и применимость нормы к объекту нужно подтвердить по
            источнику.
          </p>
        </section>

        <section v-else-if="section === 'vor'" class="workbench-panel">
          <div class="section-intro">
            <div>
              <p class="muted">Проверьте позиции ВОР на соответствие ГЭСН</p>
            </div>
            <div class="profile-field">
              <span class="profile-label">Нормативная база</span>
              <div class="profile-select-wrap">
                <USelect
                  v-model="profile"
                  :items="profileItems"
                  aria-label="Нормативная база для проверки ВОР"
                  class="profile-select"
                  :style="{ width: '260px' }"
                  :disabled="vorBusy"
                />
              </div>
            </div>
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
            <h3>{{ vorBusy ? 'Проверяем ведомость…' : 'Перетащите файл сюда' }}</h3>
            <p class="muted">XLSX · до 100 МБ · проверка начнётся автоматически</p>
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
              color="primary"
              variant="solid"
              :loading="vorBusy"
              :disabled="vorBusy"
              @click="vorInput?.click()"
              >Выбрать файл</UButton
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
              {{ vorResult.total_positions }}. Для безопасности обработка ограничена 200 позициями
              за файл.
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
                <UBadge
                  color="neutral"
                  variant="soft"
                  :label="row.number || String(row.index + 1)"
                />
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
                  ><strong>Единица:</strong>
                  {{ row.unit_match ? 'совпадает' : 'расхождение' }}</span
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
              <p v-else class="muted">
                По названию работы совпадений в выбранном корпусе не найдено.
              </p>
            </article>
          </template>
        </section>

        <section v-else class="workbench-panel manager-panel">
          <div class="section-intro">
            <div>
              <h2>
                Нормативные документы
                <span v-if="manager"
                  >· {{ catalogRows.length }} документ{{ pluralize(catalogRows.length) }}</span
                >
              </h2>
            </div>
          </div>
          <p v-if="managerError" class="error-box" role="alert">
            Не удалось обновить сведения: {{ managerError }}
            <template v-if="manager">Показаны ранее загруженные данные.</template>
          </p>
          <div v-if="managerBusy && !manager" class="empty" role="status">
            Загружаем каталог и сведения о базе…
          </div>
          <template v-if="manager">
            <div v-if="manager.profile_errors.length" class="error-box" role="alert">
              <p v-for="issue in manager.profile_errors" :key="issue.profile">
                {{ profileName(issue.profile) }}: {{ issue.message }}
              </p>
              <p>Доступные документы показаны ниже. Сведения о базе могут быть неполными.</p>
            </div>
            <section
              ref="catalogContainer"
              class="manager-catalog"
              aria-label="Каталог нормативных документов"
            >
              <div v-if="catalogRows.length" class="catalog-toolbar">
                <UInput
                  v-model="catalogQuery"
                  icon="i-lucide-search"
                  placeholder="Обозначение или название"
                  aria-label="Поиск документа"
                  class="catalog-search"
                />
                <USelectMenu
                  v-model="catalogType"
                  :items="catalogTypes"
                  value-key="value"
                  :search-input="false"
                  aria-label="Тип документа"
                  class="catalog-type"
                />
                <UButton
                  icon="i-lucide-refresh-cw"
                  color="neutral"
                  variant="outline"
                  size="sm"
                  :loading="managerBusy"
                  @click="loadManager(true)"
                  >Обновить</UButton
                >
              </div>
              <div v-if="catalogRows.length" class="catalog-layout">
                <div class="catalog-content">
                  <UTable
                    :data="catalogPageRows"
                    :columns="catalogColumns"
                    sticky="header"
                    :on-select="(event, row) => openCatalogRow(event, row.original)"
                    :ui="{
                      base: 'table-fixed',
                      tr: 'data-[selectable=true]:cursor-pointer',
                      th: 'text-muted font-normal',
                      td: 'whitespace-normal',
                    }"
                    class="catalog-table"
                    empty="Документы не найдены. Попробуйте изменить поиск или тип."
                  >
                    <template #number-cell="{ row }">
                      <a
                        :href="row.original.url"
                        :aria-label="`Открыть PDF: ${row.original.designation}`"
                        target="_blank"
                        rel="noopener noreferrer"
                        class="catalog-title"
                        >{{ row.original.number }}</a
                      >
                    </template>
                    <template #title-cell="{ row }">
                      <span class="catalog-name">{{ row.original.title }}</span>
                    </template>
                  </UTable>
                  <div class="catalog-pagination">
                    <template v-if="filteredCatalog.length">
                      <span aria-live="polite">
                        {{ (catalogPage - 1) * catalogPageSize + 1 }}–{{
                          Math.min(catalogPage * catalogPageSize, filteredCatalog.length)
                        }}
                        из {{ filteredCatalog.length }}
                      </span>
                      <UPagination
                        v-model:page="catalogPage"
                        :total="filteredCatalog.length"
                        :items-per-page="catalogPageSize"
                        :sibling-count="1"
                        aria-label="Страницы каталога"
                      />
                    </template>
                  </div>
                </div>
              </div>
              <p v-if="!catalogRows.length" class="empty" role="status">
                В нормативной базе пока нет документов. Обратитесь к администратору базы.
              </p>
              <p v-else-if="!filteredCatalog.length" class="empty" role="status">
                По запросу ничего не найдено. Измените поиск или сбросьте фильтр типа.
              </p>
            </section>
          </template>
        </section>
      </template>
    </UTabs>
  </section>
</template>

<style scoped>
.normative-workbench {
  display: flex;
  height: 100%;
  min-height: 0;
  flex-direction: column;
  padding-top: 24px;
}
.workbench-heading,
.section-intro {
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
  display: flex;
  flex: 1;
  min-height: 0;
  flex-direction: column;
}
.workbench-tabs :deep(.workbench-tab-list) {
  flex-shrink: 0;
  padding: 6px;
  margin-bottom: 18px;
  border: 1px solid var(--ui-border);
  border-radius: 10px;
}
.workbench-tabs :deep(.workbench-tab-trigger) {
  min-height: 44px;
  cursor: pointer;
}
.workbench-tabs :deep(.workbench-tab-trigger[data-state='active']) {
  color: var(--app-selected-text);
}
.workbench-tabs :deep(.workbench-tab-content) {
  display: flex;
  flex: 1;
  min-height: 0;
  flex-direction: column;
  outline: none;
}
.workbench-panel {
  padding: 22px;
  border: 1px solid var(--ui-border);
  border-radius: 10px;
  background: var(--ui-bg);
}
.manager-panel {
  display: flex;
  flex: 1;
  min-height: 0;
  flex-direction: column;
}
.profile-field {
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: flex-end;
}
.profile-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--ui-text);
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
.profile-select-wrap {
  width: 260px;
}
.profile-select {
  display: flex !important;
  width: 100% !important;
  min-width: 0;
  max-width: none;
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
.vor-row {
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
.norm-details h4 {
  margin: 0;
  font-size: 13px;
  font-weight: 650;
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
  min-height: 160px;
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
.section-intro h2 span {
  color: var(--ui-text-muted);
  font-size: 13px;
  font-weight: 400;
}
.catalog-toolbar {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
}
.catalog-search {
  flex: 1;
  min-width: 0;
}
.catalog-type {
  width: 220px;
  flex-shrink: 0;
}
.catalog-layout {
  display: flex;
  flex: 1;
  min-height: 0;
  gap: 20px;
}
.catalog-content {
  display: flex;
  flex: 1;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
}
.catalog-title {
  color: var(--ui-primary);
  font-weight: 600;
  text-decoration: none;
}
.catalog-title:hover {
  text-decoration: underline;
}
.catalog-name {
  display: block;
  margin-top: 2px;
  color: var(--ui-text-muted);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.manager-catalog :deep(th) {
  padding-block: 8px;
}
.manager-catalog :deep(td) {
  padding-block: 6px;
}
.manager-catalog :deep(th:first-child) {
  width: 18%;
}
.manager-catalog :deep(th:last-child) {
  width: 110px;
}
.manager-catalog :deep(tbody tr[data-selectable='true']:hover) {
  background: var(--ui-bg-muted);
}
.manager-catalog {
  display: flex;
  flex: 1;
  min-height: 0;
  flex-direction: column;
}
.manager-catalog :deep(.catalog-table) {
  flex: 1;
  min-height: 0;
  overflow: auto;
}
.catalog-pagination {
  display: flex;
  flex-wrap: wrap;
  min-height: 44px;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding-top: 12px;
  border-top: 1px solid var(--ui-border-muted);
}
.catalog-pagination > span {
  color: var(--ui-text-muted);
  font-size: 13px;
}
.empty {
  padding: 24px 12px;
  color: var(--ui-text-muted);
  text-align: center;
}
@media (max-height: 700px) {
  .normative-workbench {
    padding-top: 8px;
  }
  .workbench-heading {
    margin-bottom: 10px;
  }
  .workbench-heading p {
    display: none;
  }
  .workbench-tabs :deep(.workbench-tab-list) {
    margin-bottom: 10px;
    padding: 4px;
  }
  .workbench-panel {
    padding: 14px;
  }
  .section-intro {
    margin-bottom: 10px;
  }
  .catalog-toolbar {
    margin-bottom: 8px;
  }
  .catalog-pagination {
    padding-top: 6px;
  }
}
@media (max-width: 700px) {
  .normative-workbench {
    padding-top: 8px;
  }
  .workbench-heading {
    margin-bottom: 12px;
  }
  .workbench-heading,
  .section-intro {
    align-items: flex-start;
    flex-direction: column;
  }
  .workbench-heading h1 {
    font-size: 24px;
  }
  .workbench-tabs :deep(.workbench-tab-trigger) {
    justify-content: center;
    gap: 4px;
    padding: 6px 4px;
    font-size: 11px;
    line-height: 1.2;
    text-align: center;
  }
  .workbench-tabs :deep(svg) {
    flex-shrink: 0;
  }
  .search-form {
    align-items: stretch;
    flex-wrap: wrap;
  }
  .profile-select-wrap,
  .profile-select,
  .kind-select {
    width: calc(50% - 5px);
    min-width: 0;
    max-width: none;
  }
  .query-input {
    flex-basis: 100%;
    order: -1;
  }
  .catalog-toolbar {
    flex-direction: column;
  }
  .catalog-type {
    width: 100%;
  }
  .manager-catalog :deep(.catalog-table thead) {
    display: none;
  }
  .manager-catalog :deep(.catalog-table table),
  .manager-catalog :deep(.catalog-table tbody) {
    display: block;
  }
  .manager-catalog :deep(.catalog-table tbody tr[data-selectable='true']) {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 6px 12px;
    padding: 16px 4px;
  }
  .manager-catalog :deep(.catalog-table tbody td) {
    display: block;
    min-width: 0;
    padding: 0;
  }
  .manager-catalog :deep(.catalog-table tbody td:nth-child(1)) {
    grid-column: 1;
    grid-row: 1;
  }
  .manager-catalog :deep(.catalog-table tbody td:nth-child(2)) {
    grid-column: 1 / -1;
    grid-row: 2;
  }
  .manager-catalog :deep(.catalog-table tbody td:nth-child(3)) {
    grid-column: 2;
    grid-row: 1;
    text-align: right;
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
}
</style>

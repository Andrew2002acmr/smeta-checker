<script setup lang="ts">
import type { TableColumn } from '@nuxt/ui'
import type { FormatDto, FormatIssueDto } from '~/generated/types.gen'

const props = defineProps<{
  result: FormatDto
  versionId?: string
  filename?: string
  uploadFile?: File | null
}>()

const backend = useBackend()
const level = ref<'all' | 'error' | 'warning'>('all')
const query = ref('')
const openGroups = ref<Set<string>>(new Set())
const downloading = ref<'excel' | 'word' | null>(null)
const downloadError = ref('')

interface Group {
  key: string
  title: string
  hint: string
  errors: number
  items: FormatIssueDto[]
}

const groups = computed<Group[]>(() => {
  const order: Group[] = []
  const byKey = new Map<string, Group>()
  for (const issue of props.result.issues) {
    const key = issue.group || `${issue.type}:misc`
    let group = byKey.get(key)
    if (!group) {
      group = {
        key,
        title: issue.group_title || 'Прочие замечания',
        hint: issue.group_hint || '',
        errors: 0,
        items: [],
      }
      byKey.set(key, group)
      order.push(group)
    }
    group.items.push(issue)
    if (issue.status === 'Ошибка') group.errors++
  }
  return order
})

const visibleGroups = computed(() =>
  groups.value
    .map((group) => ({
      ...group,
      items: group.items.filter((issue) => {
        if (level.value === 'error' && issue.status !== 'Ошибка') return false
        if (level.value === 'warning' && issue.status !== 'Внимание') return false
        const q = query.value.trim().toLowerCase()
        if (!q) return true
        return (
          issue.coord.toLowerCase().includes(q) ||
          issue.error.toLowerCase().includes(q) ||
          issue.expected.toLowerCase().includes(q)
        )
      }),
    }))
    .filter((group) => group.items.length > 0),
)

function toggleGroup(key: string) {
  const next = new Set(openGroups.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  openGroups.value = next
}

function groupColor(group: Group) {
  return group.errors > 0 ? 'error' : 'warning'
}

async function download(kind: 'excel' | 'word') {
  if (downloading.value) return
  downloading.value = kind
  downloadError.value = ''
  try {
    const stem = (props.filename || 'ведомость').replace(/\.xlsx$/i, '')
    const name = `${stem} — оформление.${kind === 'excel' ? 'xlsx' : 'docx'}`
    const sheet = encodeURIComponent(props.result.sheet || '')
    if (props.versionId) {
      const path =
        kind === 'excel'
          ? `/api/v1/documents/${props.versionId}/format-report`
          : `/api/v1/documents/${props.versionId}/format-word`
      await backend.download(`${path}?sheet=${sheet}`, name)
    } else if (props.uploadFile) {
      const path = kind === 'excel' ? '/api/v1/format-report' : '/api/v1/format-word'
      const form = new FormData()
      form.append('file', props.uploadFile)
      await backend.downloadPost(`${path}?sheet=${sheet}`, form, name)
    }
  } catch (e) {
    downloadError.value = readableError(e)
  } finally {
    downloading.value = null
  }
}

const formatColumns: TableColumn<FormatIssueDto>[] = [
  { accessorKey: 'coord', header: 'Ячейка' },
  { accessorKey: 'error', header: 'Что не так' },
]
const canDownload = computed(() => Boolean(props.versionId || props.uploadFile))

watch(
  () => props.result,
  () => {
    openGroups.value = new Set()
    query.value = ''
    level.value = 'all'
    downloadError.value = ''
  },
)
</script>

<template>
  <div v-if="!result.issues.length" class="format-success" role="status">
    <UIcon name="i-lucide-circle-check" class="format-success-icon" aria-hidden="true" />
    <div>
      <p class="format-success-title">Оформление соответствует шаблону</p>
      <p class="muted">
        Лист «{{ result.sheet }}»: шапка, ячейки и формулы проверены, замечаний нет.
      </p>
    </div>
  </div>

  <div v-else class="format-table-wrap">
    <p class="muted">
      Лист «{{ result.sheet }}»: ошибок {{ result.error_count }}, замечаний
      {{ result.warning_count }}. Группы упорядочены по важности: сначала позиции и формулы, затем
      разделы, затем шапка.
    </p>

    <div class="toolbar format-filters">
      <USelect
        v-model="level"
        :items="[
          { label: 'Всё', value: 'all' },
          { label: 'Только ошибки', value: 'error' },
          { label: 'Только замечания', value: 'warning' },
        ]"
        aria-label="Показать ошибки или замечания"
        class="format-level"
      />
      <UInput
        v-model="query"
        icon="i-lucide-search"
        placeholder="Найти ячейку или текст…"
        aria-label="Поиск по замечаниям"
        class="format-search"
      />
      <template v-if="canDownload">
        <UButton
          icon="i-lucide-file-spreadsheet"
          color="neutral"
          variant="outline"
          size="sm"
          :loading="downloading === 'excel'"
          :disabled="downloading !== null"
          aria-label="Скачать Excel с подсвеченными ячейками"
          @click="download('excel')"
          >Excel</UButton
        >
        <UButton
          icon="i-lucide-file-text"
          color="neutral"
          variant="outline"
          size="sm"
          :loading="downloading === 'word'"
          :disabled="downloading !== null"
          aria-label="Скачать Word-отчёт для внешних проверяющих"
          @click="download('word')"
          >Word</UButton
        >
      </template>
    </div>
    <p v-if="downloadError" class="error-box" role="alert">{{ downloadError }}</p>

    <div v-for="group in visibleGroups" :key="group.key" class="format-group">
      <button
        type="button"
        class="format-group-head"
        :aria-expanded="openGroups.has(group.key)"
        @click="toggleGroup(group.key)"
      >
        <UBadge
          :color="groupColor(group)"
          variant="soft"
          size="sm"
          :label="String(group.items.length)"
        />
        <span class="format-group-title">{{ group.title }}</span>
        <UIcon
          :name="openGroups.has(group.key) ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
          aria-hidden="true"
        />
      </button>
      <p class="muted format-group-hint">{{ group.hint }}</p>
      <UTable
        sticky="header"
        v-if="openGroups.has(group.key)"
        :data="group.items"
        :columns="formatColumns"
        class="results max-h-[60vh] overflow-auto"
      >
        <template #coord-cell="{ row }">
          <span class="numbers">{{ row.original.coord }}</span>
        </template>
        <template #error-cell="{ row }">
          {{ row.original.error || '—' }}
        </template>
        <template #expected-cell="{ row }">
          {{ row.original.expected || '—' }}
        </template>
      </UTable>
    </div>
    <p v-if="!visibleGroups.length" class="empty" role="status">
      По фильтру ничего не найдено. Ослабьте условия поиска.
    </p>
  </div>
</template>

<style scoped>
.format-success {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  border: 1px solid var(--ui-border);
  border-radius: 8px;
  padding: 16px;
  background: color-mix(in srgb, var(--ui-success) 8%, transparent);
}
.format-success-icon {
  width: 24px;
  height: 24px;
  color: var(--ui-success);
  flex-shrink: 0;
}
.format-success-title {
  font-weight: 650;
  margin: 0 0 4px;
}
.format-table-wrap {
  overflow-x: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.format-filters {
  flex-wrap: wrap;
}
.format-level {
  min-width: 170px;
}
.format-search {
  min-width: 200px;
  flex: 1;
}
.format-group {
  border: 1px solid var(--ui-border);
  border-radius: 8px;
  padding: 8px 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.format-group-head {
  display: flex;
  align-items: center;
  gap: 8px;
  background: none;
  border: none;
  padding: 4px 0;
  cursor: pointer;
  text-align: left;
  color: inherit;
  font: inherit;
}
.format-group-title {
  font-weight: 600;
  flex: 1;
}
.format-group-hint {
  margin: 0;
}
.format-cell {
  overflow-wrap: anywhere;
  min-width: 160px;
}
</style>

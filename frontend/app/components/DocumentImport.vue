<script setup lang="ts">
import type { DocumentDto } from '~/generated/types.gen'
import { collectDropFiles } from '~/utils/dropFiles'

interface PendingFile {
  key: string
  file: File
  kind: 'vor' | 'pdf'
  target: string
  status: 'pending' | 'uploading' | 'done' | 'error'
  progress: number
  errorMessage?: string
}

const props = withDefaults(
  defineProps<{
    projectId: string
    projectName: string
    departmentId?: string
    documents: DocumentDto[]
    targetDocumentId?: string
  }>(),
  { targetDocumentId: undefined },
)
const emit = defineEmits<{ saved: [documentId?: string]; changed: [] }>()

const backend = useBackend()
const pending = ref<PendingFile[]>([])
const errors = ref<string[]>([])
const saving = ref(false)
const collecting = ref(false)
const status = ref('')
const dragActive = ref(false)

const filesInput = ref<HTMLInputElement | null>(null)
const folderInput = ref<HTMLInputElement | null>(null)
let dragDepth = 0
let seq = 0

const lockedDocument = computed(() =>
  props.targetDocumentId
    ? props.documents.find((d) => d.id === props.targetDocumentId) || null
    : null,
)

function kindLabel(kind: string): string {
  return kind === 'pdf' ? 'ПД' : 'ВОР'
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} Б`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} КБ`
  return `${(bytes / (1024 * 1024)).toFixed(1)} МБ`
}

function kindOf(filename: string): 'vor' | 'pdf' | null {
  if (/\.pdf$/i.test(filename)) return 'pdf'
  if (/\.(xlsx|csv)$/i.test(filename)) return 'vor'
  return null
}

function fileKey(file: File): string {
  const rel = (file as File & { webkitRelativePath?: string }).webkitRelativePath || ''
  return `${rel}::${file.name}::${file.size}::${file.lastModified}`
}

function targetOptions(kind: 'vor' | 'pdf', filename: string) {
  const same = filename.toLocaleLowerCase('ru-RU')
  return [
    { label: 'Новый документ', value: 'new' },
    ...props.documents
      .filter((d) => d.kind === kind && !d.archived)
      .map((d) => ({
        label:
          d.versions.some((v) => v.filename.toLocaleLowerCase('ru-RU') === same) &&
          d.id !== props.targetDocumentId
            ? `${d.title} · имя файла совпадает`
            : d.title,
        value: d.id,
      })),
  ]
}

function addFiles(incoming: File[]) {
  if (saving.value) return
  for (const file of incoming) {
    const kind = kindOf(file.name)
    if (!kind) {
      errors.value.push(`${file.name}: поддерживаются ВОР XLSX/CSV и проектные PDF.`)
      continue
    }
    if (file.size > 100 * 1024 * 1024) {
      errors.value.push(`${file.name}: файл больше 100 МБ.`)
      continue
    }
    if (pending.value.length >= 50) {
      errors.value.push('За один раз можно добавить до 50 файлов.')
      break
    }
    if (pending.value.some((pendingFile) => pendingFile.file === file)) continue
    if (props.targetDocumentId) {
      const locked = props.documents.find((d) => d.id === props.targetDocumentId)
      if (locked && locked.kind !== kind) {
        errors.value.push(`${file.name}: документ принимает только ${kindLabel(locked.kind)}.`)
        continue
      }
      if (pending.value.length > 0) {
        errors.value.push(`${file.name}: новая ревизия — один файл за раз.`)
        continue
      }
      pending.value.push({
        key: `locked-${seq++}`,
        file,
        kind,
        target: props.targetDocumentId,
        status: 'pending',
        progress: 0,
      })
      continue
    }
    pending.value.push({
      key: `file-${seq++}-${fileKey(file)}`,
      file,
      kind,
      target: 'new',
      status: 'pending',
      progress: 0,
    })
  }
}

function removePending(target: PendingFile) {
  if (saving.value || collecting.value) return
  pending.value = pending.value.filter((p) => p !== target)
}

function onFilesPicked(event: Event) {
  if (saving.value || collecting.value) return
  const list = (event.target as HTMLInputElement | null)?.files
  if (list) addFiles(Array.from(list))
  if (event.target instanceof HTMLInputElement) event.target.value = ''
}

function onDragEnter(event: DragEvent) {
  if (saving.value || collecting.value) return
  event.preventDefault()
  dragDepth += 1
  dragActive.value = true
}

function onDragOver(event: DragEvent) {
  if (saving.value || collecting.value) return
  event.preventDefault()
  dragActive.value = true
}

function onDragLeave(event: DragEvent) {
  event.preventDefault()
  dragDepth = Math.max(0, dragDepth - 1)
  if (dragDepth === 0) dragActive.value = false
}

async function onDrop(event: DragEvent) {
  dragDepth = 0
  dragActive.value = false
  if (saving.value || collecting.value) return
  const source = event.dataTransfer
  if (!source) return
  collecting.value = true
  try {
    addFiles(await collectDropFiles(source))
  } catch {
    errors.value.push('Не удалось прочитать перетащенную папку. Попробуйте ещё раз.')
  } finally {
    collecting.value = false
  }
}

async function save() {
  if (saving.value || !pending.value.length) return
  saving.value = true
  errors.value = []
  let savedId: string | undefined
  try {
    const queue = [...pending.value].filter((item) => item.status !== 'done')
    for (const item of queue) {
      item.status = 'uploading'
      item.progress = 30
      status.value = `Сохраняем ${item.file.name}…`
      const form = new FormData()
      form.append('project_id', props.projectId)
      if (props.departmentId) form.append('department_id', props.departmentId)
      form.append('kind', item.kind)
      form.append('file', item.file)
      if (item.target === 'new') form.append('new_document', 'true')
      else form.append('document_id', item.target)
      try {
        item.progress = 70
        const reply = await backend.upload(form)
        savedId = reply.document_id
        item.status = 'done'
        item.progress = 100
        emit('changed')
        window.setTimeout(() => {
          pending.value = pending.value.filter((p) => p !== item)
        }, 900)
      } catch (e) {
        item.status = 'error'
        item.errorMessage = readableError(e)
        errors.value.push(`${item.file.name}: ${readableError(e)}`)
      }
    }
  } finally {
    status.value = ''
    saving.value = false
  }
  if (pending.value.every((item) => item.status === 'done' || item.status === 'error')) {
    if (pending.value.filter((item) => item.status !== 'done').length === 0) emit('saved', savedId)
  }
}
</script>

<template>
  <section aria-label="Импорт документов">
    <UAlert
      v-if="errors.length"
      color="error"
      variant="soft"
      title="Часть файлов не сохранена"
      role="alert"
      class="import-alert"
    >
      <template #description>
        <ul class="import-errors">
          <li v-for="(message, index) in errors" :key="index">{{ message }}</li>
        </ul>
      </template>
    </UAlert>

    <p v-if="lockedDocument" class="muted import-lock" role="note">
      Новая ревизия документа «{{ lockedDocument.title }}» ({{ kindLabel(lockedDocument.kind) }}).
      Выберите один файл.
    </p>

    <div
      class="import-drop"
      :class="{ 'import-drop-active': dragActive }"
      role="region"
      aria-label="Перетащите папку или файлы"
      @dragenter="onDragEnter"
      @dragover="onDragOver"
      @dragleave="onDragLeave"
      @drop.prevent="onDrop($event)"
    >
      <UIcon name="i-lucide-upload-cloud" class="import-drop-icon" aria-hidden="true" />
      <p class="import-drop-title">Перетащите папку или файлы сюда</p>
      <p class="muted">XLSX/CSV и PDF · до 100 МБ · до 50 файлов</p>
      <p v-if="collecting || status" class="muted" role="status">
        {{ collecting ? 'Читаем содержимое папки…' : status }}
      </p>
      <div class="toolbar import-pick">
        <input
          id="import-files-input"
          ref="filesInput"
          type="file"
          aria-label="Добавить файлы"
          :multiple="!targetDocumentId"
          accept=".xlsx,.csv,.pdf"
          class="import-hidden-input"
          tabindex="-1"
          :disabled="saving || collecting"
          @change="onFilesPicked"
        />
        <input
          v-if="!targetDocumentId"
          id="import-folder-input"
          ref="folderInput"
          type="file"
          aria-label="Добавить папку"
          multiple
          webkitdirectory
          class="import-hidden-input"
          tabindex="-1"
          :disabled="saving || collecting"
          @change="onFilesPicked"
        />
        <UButton
          color="neutral"
          variant="outline"
          icon="i-lucide-files"
          :disabled="saving || collecting"
          @click="filesInput?.click()"
          >Добавить файлы</UButton
        >
        <UButton
          v-if="!targetDocumentId"
          color="neutral"
          variant="outline"
          icon="i-lucide-folder-open"
          :disabled="saving || collecting"
          @click="folderInput?.click()"
          >Добавить папку</UButton
        >
      </div>
    </div>

    <ul v-if="pending.length" class="import-docs" aria-label="Файлы к сохранению">
      <li v-for="item in pending" :key="item.key" class="import-doc">
        <UIcon
          :name="item.kind === 'pdf' ? 'i-lucide-file-text' : 'i-lucide-file-spreadsheet'"
          aria-hidden="true"
        />
        <div class="import-doc-main">
          <span class="import-doc-name">{{ item.file.name }}</span>
          <span class="muted import-doc-meta"
            >{{ kindLabel(item.kind) }} · {{ formatSize(item.file.size) }}</span
          >
          <UProgress
            v-if="item.status === 'uploading'"
            :model-value="item.progress"
            size="xs"
            class="import-progress"
          />
          <span v-if="item.status === 'done'" class="muted import-status">Сохранено</span>
          <span v-if="item.status === 'error'" class="error-text import-status">{{
            item.errorMessage
          }}</span>
          <USelect
            v-if="!targetDocumentId"
            v-model="item.target"
            :items="targetOptions(item.kind, item.file.name)"
            :aria-label="`Назначение ${item.file.name}`"
            :disabled="saving"
            class="import-doc-target"
          />
        </div>
        <UButton
          variant="link"
          color="neutral"
          :disabled="saving || collecting"
          :aria-label="'Убрать ' + item.file.name"
          @click="removePending(item)"
          >Убрать</UButton
        >
      </li>
    </ul>
    <p v-else class="empty" role="status">Файлы не выбраны.</p>

    <div class="toolbar spread import-footer">
      <span class="muted">{{ pending.length ? `К сохранению: ${pending.length}` : '' }}</span>
      <UButton
        color="primary"
        icon="i-lucide-save"
        :loading="saving || collecting"
        :disabled="!pending.length || saving || collecting"
        @click="save"
        >Сохранить документы</UButton
      >
    </div>
  </section>
</template>

<style scoped>
.import-alert {
  margin-bottom: 12px;
}
.import-errors {
  margin: 4px 0 0;
  padding-left: 18px;
}
.import-lock {
  margin: 0 0 12px;
}
.import-drop {
  border: 1px dashed var(--ui-border);
  border-radius: var(--app-radius, 12px);
  padding: 24px 16px;
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}
.import-drop-active {
  border-color: var(--ui-primary);
  background: var(--ui-bg-muted);
}
.import-drop-icon {
  font-size: 32px;
  color: var(--ui-text-muted);
}
.import-drop-title {
  margin: 0;
  font-weight: 650;
}
.import-pick {
  justify-content: center;
  margin-top: 4px;
}
.import-hidden-input {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}
.import-docs {
  list-style: none;
  margin: 12px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.import-doc {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  border: 1px solid var(--ui-border);
  border-radius: var(--app-radius, 12px);
  padding: 10px 12px;
}
.import-doc-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.import-doc-name {
  overflow-wrap: anywhere;
  font-weight: 600;
}
.import-doc-meta {
  font-size: 13px;
}
.import-doc-target {
  max-width: 420px;
}
.import-footer {
  margin-top: 12px;
}
</style>

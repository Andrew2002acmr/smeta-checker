<script setup lang="ts">
import type { FormatDto } from '~/generated/types.gen'

const backend = useBackend()
const open = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const fileName = ref('')
const sheet = ref('')
const checking = ref(false)
const error = ref('')
const result = ref<FormatDto | null>(null)

let epoch = 0
const pendingFile = ref<File | null>(null)

const sheetItems = useFormatSheets(result)

function pickFile() {
  fileInput.value?.click()
}

function onFilePicked(event: Event) {
  const input = event.target as HTMLInputElement | null
  const file = input?.files?.[0] ?? null
  if (input) input.value = ''
  if (!file) return
  pendingFile.value = file
  fileName.value = file.name
  sheet.value = ''
  reset()
  open.value = true
  void check()
}

function reset() {
  epoch++
  checking.value = false
  result.value = null
  error.value = ''
}

async function check() {
  if (!pendingFile.value) return
  const current = ++epoch
  checking.value = true
  error.value = ''
  try {
    const form = new FormData()
    form.append('file', pendingFile.value)
    const value = await backend.formattingUpload(form, sheet.value.trim())
    if (current !== epoch) return
    result.value = value
    if (!sheet.value && value.sheet) sheet.value = value.sheet
  } catch (e) {
    if (current !== epoch) return
    error.value = readableError(e)
  } finally {
    if (current === epoch) checking.value = false
  }
}

watch(sheet, (value, old) => {
  if (value === old || !open.value || !pendingFile.value) return
  reset()
  void check()
})

onBeforeUnmount(() => {
  epoch++
})
</script>

<template>
  <input
    ref="fileInput"
    type="file"
    accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    class="hidden-input"
    aria-label="Файл ведомости для проверки оформления"
    @change="onFilePicked"
  />
  <UButton
    icon="i-lucide-spell-check"
    color="neutral"
    variant="outline"
    aria-label="Проверить оформление файла ВОР без сохранения"
    @click="pickFile"
    >Оформление ВОР</UButton
  >

  <UModal
    v-model:open="open"
    :title="`Оформление · ${fileName || 'файл'}`"
    description="Файл не сохраняется в проект — только проверка оформления"
    :ui="{ content: 'max-w-3xl' }"
  >
    <template #body>
      <div class="toolbar format-bar">
        <USelect
          v-if="sheetItems.length"
          v-model="sheet"
          :items="sheetItems"
          aria-label="Лист ведомости для проверки оформления"
          class="format-sheet"
        />
        <UButton
          variant="outline"
          :loading="checking"
          :disabled="checking || !pendingFile"
          aria-label="Проверить оформление повторно"
          @click="check()"
        >
          Проверить снова
        </UButton>
      </div>

      <p v-if="checking && !result" class="empty" role="status">Проверяем оформление…</p>
      <p v-else-if="error" class="error-box" role="alert">
        {{ error }}
        <UButton variant="link" aria-label="Повторить проверку оформления" @click="check()">
          Повторить
        </UButton>
      </p>
      <FormatCheckResult
        v-else-if="result"
        :result="result"
        :filename="fileName"
        :upload-file="pendingFile"
      />
    </template>
  </UModal>
</template>

<style scoped>
.hidden-input {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
}
.format-bar {
  margin-bottom: 12px;
}
.format-sheet {
  min-width: 220px;
  flex: 1;
}
</style>

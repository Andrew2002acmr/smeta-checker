<script setup lang="ts">
import type { FormatDto, VersionDto } from '~/generated/types.gen'

const props = defineProps<{ version: VersionDto }>()

const backend = useBackend()
const open = ref(false)
const sheet = ref('')
const checking = ref(false)
const error = ref('')
const result = ref<FormatDto | null>(null)

let epoch = 0

const sheetItems = useFormatSheets(result)

const formatBadge = computed(() => {
  const status = (props.version.format_status ?? {}) as Record<string, unknown>
  if (status.state === 'passed')
    return { color: 'success' as const, label: 'Оформление: соответствует' }
  if (status.state === 'failed') {
    const errors = Number(status.error_count ?? 0)
    const warnings = Number(status.warning_count ?? 0)
    const parts = []
    if (errors) parts.push(`ошибок: ${errors}`)
    if (warnings) parts.push(`замечаний: ${warnings}`)
    return {
      color: 'warning' as const,
      label: `Оформление: ${parts.join(', ') || 'есть замечания'}`,
    }
  }
  return null
})

async function check() {
  const current = ++epoch
  checking.value = true
  error.value = ''
  try {
    const value = await backend.formatting(props.version.id, sheet.value.trim())
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

function reset() {
  epoch++
  checking.value = false
  result.value = null
  error.value = ''
}

watch(
  () => props.version.id,
  () => {
    open.value = false
    sheet.value = ''
    reset()
  },
)

watch(sheet, (value, old) => {
  if (value === old) return
  reset()
  if (open.value) void check()
})

watch(open, (value) => {
  if (value && !result.value && !checking.value && !error.value) void check()
})

onBeforeUnmount(() => {
  epoch++
})
</script>

<template>
  <UBadge
    v-if="formatBadge"
    :color="formatBadge.color"
    variant="soft"
    size="sm"
    :label="formatBadge.label"
  />
  <UButton
    v-if="props.version.filename.toLowerCase().endsWith('.xlsx')"
    icon="i-lucide-spell-check"
    variant="outline"
    aria-label="Проверить оформление ведомости"
    @click="open = true"
  >
    Проверить оформление
  </UButton>

  <UModal
    v-model:open="open"
    :title="`Оформление · ${props.version.filename}`"
    description="Детерминированная проверка шапки, ячеек и формул — без модели"
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
        <UInput
          v-else
          v-model="sheet"
          placeholder="Лист (по умолчанию — первый)"
          aria-label="Лист ведомости для проверки оформления"
          class="format-sheet"
          @keyup.enter="check()"
        />
        <UButton
          variant="outline"
          :loading="checking"
          :disabled="checking"
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
        :version-id="props.version.id"
        :filename="props.version.filename"
      />
    </template>
  </UModal>
</template>

<style scoped>
.format-bar {
  margin-bottom: 12px;
}
.format-sheet {
  min-width: 220px;
  flex: 1;
}
</style>

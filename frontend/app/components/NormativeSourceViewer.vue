<script setup lang="ts">
const props = defineProps<{
  source: {
    url: string
    label: string
    page: number
    section: string
    focus: string
    quote: string
    rects?: number[][]
  } | null
}>()
const emit = defineEmits<{ close: [] }>()
const page = ref(1)
const anchored = ref<boolean | null>(null)
const sectionLabel = computed(() =>
  props.source?.section
    ? `${props.source.section.includes('-') ? 'Норма' : 'Пункт'} ${props.source.section}`
    : 'Основание',
)
watch(page, () => {
  anchored.value = null
})
watch(
  () => props.source,
  (source) => {
    page.value = source?.page ?? 1
    anchored.value = null
  },
  { immediate: true },
)
</script>

<template>
  <UModal
    :open="!!source"
    title="Источник документа"
    :description="source?.label"
    :ui="{ content: 'w-[96vw] max-w-6xl' }"
    @update:open="
      (value) => {
        if (!value) emit('close')
      }
    "
  >
    <template #body>
      <div v-if="source" class="source-reader">
        <div class="source-reader-location">
          <strong>{{ page === source.page ? sectionLabel : 'Документ' }} · стр. {{ page }}</strong>
          <span v-if="anchored === true" role="status">Нужный фрагмент подсвечен.</span>
          <span v-else-if="anchored === false && source.focus" role="status"
            >Открыта нужная страница. Текстовый якорь не найден — проверьте лист.</span
          >
          <a :href="`${source.url}#page=${source.page}`" target="_blank" rel="noopener"
            >Открыть PDF отдельно</a
          >
          <UButton v-if="page !== source.page" variant="link" @click="page = source.page"
            >К основанию</UButton
          >
        </div>
        <PdfPanel
          :source-url="source.url"
          :page="page"
          :focus-text="page === source.page ? source.focus : ''"
          :focus-rects="page === source.page ? source.rects : undefined"
          @page="page = $event"
          @focused="anchored = $event"
        />
      </div>
    </template>
    <template #footer
      ><UButton color="neutral" variant="outline" @click="emit('close')"
        >Вернуться к проверке</UButton
      ></template
    >
  </UModal>
</template>

<style scoped>
.source-reader {
  display: flex;
  flex-direction: column;
  height: min(75vh, 1100px);
  min-height: 260px;
}
.source-reader-location {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem 1rem;
  padding-bottom: 0.75rem;
  overflow-wrap: anywhere;
}
.source-reader-location span {
  color: var(--ui-text-muted);
  font-size: 0.875rem;
}
.source-reader-location a {
  margin-left: auto;
}
</style>

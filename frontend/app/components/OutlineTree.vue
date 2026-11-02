<script setup lang="ts">
import type { StructureDto, StructureNodeDto } from '~/generated/types.gen'
const props = defineProps<{
  data: StructureDto | null
  page: number
  selectedId?: string
  error?: string
}>()
const emit = defineEmits<{ select: [node: StructureNodeDto] }>(),
  search = ref(''),
  host = ref<HTMLElement>()
const selected = computed(
  () =>
    props.data?.nodes.find((n) => n.id === props.selectedId) ||
    props.data?.nodes
      .filter((n) => n.physical_start <= props.page && n.physical_end >= props.page)
      .toSorted(
        (a, b) =>
          b.physical_start - a.physical_start ||
          (b.kind === 'table' ? 1 : 0) - (a.kind === 'table' ? 1 : 0) ||
          b.level - a.level,
      )[0],
)
const ancestors = computed(() => {
  const result: string[] = []
  let n = selected.value
  while (n) {
    result.push(n.id)
    n = props.data?.nodes.find((p) => p.id === n?.parent_id)
  }
  return result
})
watch(
  () => selected.value?.id,
  async () => {
    await nextTick()
    host.value?.querySelector('.outline-label.active')?.scrollIntoView({ block: 'nearest' })
  },
  { flush: 'post' },
)
</script>
<template>
  <section ref="host" class="ot-tree" aria-label="Дерево разделов PDF">
    <b class="ot-title">Разделы PDF</b
    ><UInput v-model="search" placeholder="Найти раздел" aria-label="Найти раздел" class="w-full" />
    <p v-if="error" class="muted" role="alert">{{ error }}</p>
    <p v-else-if="!data" class="muted ot-status" role="status">Открываем разделы…</p>
    <p v-else-if="!data.nodes.length" class="muted ot-status">
      Разделы не выделены. Используйте страницы или поиск по PDF.
    </p>
    <ul v-else class="ot-list">
      <OutlineBranch
        v-for="id in data.roots"
        :key="id"
        :node="data.nodes.find((n) => n.id === id)!"
        :nodes="data.nodes"
        :selected="selected?.id || ''"
        :ancestors="ancestors"
        :search="search.toLocaleLowerCase('ru-RU')"
        @select="emit('select', $event)"
      />
    </ul>
  </section>
</template>
<style scoped>
.ot-title {
  display: block;
  font-size: 14px;
  font-weight: 700;
  color: var(--ui-text-highlighted);
  margin: 0 4px 12px;
}
.ot-status {
  padding: 16px 0;
}
.ot-list {
  margin: 10px 0 0;
  padding: 0;
  list-style: none;
}
</style>

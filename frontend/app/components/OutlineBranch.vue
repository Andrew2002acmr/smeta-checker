<script setup lang="ts">
import type { StructureNodeDto } from '~/generated/types.gen'
const props = defineProps<{
  node: StructureNodeDto
  nodes: StructureNodeDto[]
  selected: string
  ancestors: string[]
  search: string
}>()
const emit = defineEmits<{ select: [node: StructureNodeDto] }>()
const children = computed(() =>
  props.node.children
    .map((id) => props.nodes.find((n) => n.id === id))
    .filter((n): n is StructureNodeDto => !!n),
)
const open = ref(props.ancestors.includes(props.node.id))
watch(
  () => props.ancestors,
  () => {
    if (props.ancestors.includes(props.node.id)) open.value = true
  },
)
function matches(node: StructureNodeDto, seen = new Set<string>()): boolean {
  if (seen.has(node.id)) return false
  seen.add(node.id)
  return (
    node.title.toLocaleLowerCase('ru-RU').includes(props.search) ||
    node.children.some((id) => {
      const child = props.nodes.find((n) => n.id === id)
      return child ? matches(child, seen) : false
    })
  )
}
const visible = computed(() => !props.search || matches(props.node))
</script>
<template>
  <li v-show="visible" class="outline-node">
    <div class="outline-row">
      <button
        v-if="children.length"
        class="outline-toggle"
        :aria-label="(open ? 'Свернуть ' : 'Раскрыть ') + node.title"
        :aria-expanded="open"
        @click="open = !open"
      >
        {{ open ? '▾' : '▸' }}</button
      ><span v-else class="outline-toggle"></span
      ><button
        class="outline-label"
        :class="{ active: node.id === selected }"
        :aria-current="node.id === selected ? 'location' : undefined"
        @click="emit('select', node)"
      >
        {{ node.title
        }}<small
          >PDF {{ node.physical_start
          }}{{ node.physical_end !== node.physical_start ? '–' + node.physical_end : '' }}</small
        >
      </button>
    </div>
    <ul v-if="children.length" v-show="open || search">
      <OutlineBranch
        v-for="child in children"
        :key="child.id"
        :node="child"
        :nodes="nodes"
        :selected="selected"
        :ancestors="ancestors"
        :search="search"
        @select="emit('select', $event)"
      />
    </ul>
  </li>
</template>
<style scoped>
.outline-node ul {
  padding-left: 12px;
}
.outline-row {
  display: flex;
  align-items: start;
}
.outline-toggle {
  flex: 0 0 15px;
  padding-top: 8px;
  background: none;
  border: 0;
  font-size: 13px;
  color: var(--text-color-muted);
}
.outline-label {
  font-size: 11px;
  line-height: 1.5;
  text-align: left;
  padding: 7px 6px;
  width: 100%;
  border-left: 2px solid var(--border-color-muted);
  color: var(--text-color-default);
}
.outline-label small {
  display: block;
  font-size: 10px;
  color: var(--text-color-muted);
  margin-top: 2px;
}
.outline-label.active {
  background: var(--color-primary-50);
  border-left-color: var(--color-primary);
}
.outline-label:hover {
  background: var(--background-color-elevated);
}
</style>

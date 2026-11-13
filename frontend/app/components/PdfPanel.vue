<script setup lang="ts">
import type { PDFDocumentProxy, PDFDocumentLoadingTask, RenderTask } from 'pdfjs-dist'
import { clampPage } from '~/utils/presentation'

const props = defineProps<{ sourceUrl: string; page: number; highlight?: number[] | null }>()
const emit = defineEmits<{ page: [page: number]; loaded: [count: number] }>()
const canvas = ref<HTMLCanvasElement>()
const textHost = ref<HTMLElement>()
const scroll = ref<HTMLElement>()
const marker = ref<HTMLElement>()
const markerStyle = computed(() => {
  const rect = props.highlight
  if (!rect || rect.length !== 4 || !rect.every(Number.isFinite)) return undefined
  const [x0 = 0, y0 = 0, x1 = 0, y1 = 0] = rect
  return {
    left: `${x0 * 100}%`,
    top: `${y0 * 100}%`,
    width: `${(x1 - x0) * 100}%`,
    height: `${(y1 - y0) * 100}%`,
  }
})

function revealHighlight() {
  if (!scroll.value || !marker.value) return
  const region = marker.value.getBoundingClientRect()
  const container = scroll.value.getBoundingClientRect()
  scroll.value.scrollTop += region.top - container.top - container.height / 3
  scroll.value.scrollLeft += region.left - container.left - container.width / 3
}
const zoom = ref(1)
const count = ref(0)
const busy = ref(false)
const ready = ref(false)
const error = ref('')
const width = ref(0)
const height = ref(0)
const scale = ref(1)
let documentPdf: PDFDocumentProxy | null = null
let loading: PDFDocumentLoadingTask | null = null
let renderTask: RenderTask | null = null
let epoch = 0
let resizeObserver: ResizeObserver | undefined

async function render() {
  if (!documentPdf || !canvas.value || !textHost.value || !scroll.value) return
  const current = ++epoch
  renderTask?.cancel()
  busy.value = true
  error.value = ''
  try {
    const page = await documentPdf.getPage(clampPage(props.page, documentPdf.numPages))
    if (current !== epoch) return
    const pdfjs = await import('pdfjs-dist')
    const fit = Math.max(200, scroll.value.clientWidth - 24) / page.getViewport({ scale: 1 }).width
    const viewport = page.getViewport({ scale: Math.min(fit * zoom.value, 4) })
    const ratio = Math.min(window.devicePixelRatio || 1, 2)
    width.value = viewport.width
    height.value = viewport.height
    scale.value = viewport.scale
    await nextTick()
    if (current !== epoch || !canvas.value || !textHost.value) return
    canvas.value.width = Math.ceil(viewport.width * ratio)
    canvas.value.height = Math.ceil(viewport.height * ratio)
    renderTask = page.render({
      canvas: canvas.value,
      canvasContext: canvas.value.getContext('2d')!,
      viewport,
      transform: [ratio, 0, 0, ratio, 0, 0],
    })
    await renderTask.promise
    if (current !== epoch) return
    textHost.value.innerHTML = ''
    const text = await page.getTextContent()
    await new pdfjs.TextLayer({
      textContentSource: text,
      container: textHost.value,
      viewport,
    }).render()
    if (current !== epoch) return
    ready.value = true
    if (marker.value) revealHighlight()
    else scroll.value.scrollTop = 0
  } catch (e) {
    if (current === epoch && !(e instanceof Error && e.name === 'RenderingCancelledException'))
      error.value = 'Не удалось открыть страницу PDF.'
  } finally {
    if (current === epoch) busy.value = false
  }
}

async function open() {
  epoch++
  renderTask?.cancel()
  if (loading) await loading.destroy()
  documentPdf = null
  ready.value = false
  busy.value = true
  error.value = ''
  try {
    const pdfjs = await import('pdfjs-dist')
    pdfjs.GlobalWorkerOptions.workerSrc = '/pdfjs/build/pdf.worker.mjs'
    const task = pdfjs.getDocument({
      url: props.sourceUrl,
      cMapUrl: '/pdfjs/cmaps/',
      cMapPacked: true,
      standardFontDataUrl: '/pdfjs/standard_fonts/',
      wasmUrl: '/pdfjs/wasm/',
    })
    loading = task
    const pdf = await task.promise
    if (task !== loading) {
      await task.destroy()
      return
    }
    documentPdf = pdf
    count.value = pdf.numPages
    emit('loaded', pdf.numPages)
    await render()
  } catch {
    error.value = 'Не удалось загрузить PDF. Проверьте доступность документа.'
    busy.value = false
  }
}

function changePage(value: number) {
  emit('page', clampPage(value, count.value))
}

watch(() => props.sourceUrl, open)
watch(() => props.page, render)
watch(() => props.highlight, revealHighlight, { flush: 'post' })
watch(zoom, render)
onMounted(() => {
  open()
  let timer: ReturnType<typeof setTimeout>
  resizeObserver = new ResizeObserver(() => {
    clearTimeout(timer)
    timer = setTimeout(render, 150)
  })
  if (scroll.value) resizeObserver.observe(scroll.value)
})
onBeforeUnmount(() => {
  epoch++
  resizeObserver?.disconnect()
  renderTask?.cancel()
  loading?.destroy()
  loading = null
  documentPdf = null
})

async function find(query: string) {
  const value = query.trim().toLocaleLowerCase('ru-RU')
  const hits: number[] = []
  const doc = documentPdf
  if (!doc || !value) return hits
  for (let n = 1; n <= doc.numPages; n++) {
    if (doc !== documentPdf) return []
    const content = await (await doc.getPage(n)).getTextContent()
    if (
      content.items
        .map((item) => ('str' in item ? item.str : ''))
        .join(' ')
        .toLocaleLowerCase('ru-RU')
        .includes(value)
    )
      hits.push(n)
  }
  if (hits[0]) emit('page', hits[0])
  return hits
}

defineExpose({ find })
</script>

<template>
  <div class="pdf-container">
    <div class="toolbar pdf-toolbar">
      <UButton
        variant="outline"
        color="neutral"
        icon="i-lucide-chevron-left"
        aria-label="Предыдущая страница PDF"
        :disabled="page <= 1"
        @click="changePage(page - 1)"
      /><UInput
        type="number"
        :model-value="page"
        :min="1"
        :max="count"
        aria-label="Физическая страница PDF"
        class="page-input"
        @change="changePage(Number(($event.target as HTMLInputElement).value))"
      /><span class="muted pdf-count">из {{ count || '…' }}</span
      ><UButton
        variant="outline"
        color="neutral"
        icon="i-lucide-chevron-right"
        aria-label="Следующая страница PDF"
        :disabled="page >= count"
        @click="changePage(page + 1)"
      /><UButton
        variant="outline"
        color="neutral"
        icon="i-lucide-zoom-out"
        aria-label="Уменьшить PDF"
        @click="zoom = Math.max(0.5, zoom - 0.5)"
      /><UButton
        variant="outline"
        color="neutral"
        icon="i-lucide-zoom-in"
        aria-label="Увеличить PDF"
        @click="zoom = Math.min(4, zoom + 0.5)"
      /><UButton variant="ghost" color="neutral" @click="zoom = 1">По ширине</UButton
      ><span v-if="busy && !ready" class="muted" role="status">Открываем…</span>
    </div>
    <div v-if="error" class="error-box" role="alert">
      {{ error }} <UButton @click="open">Повторить</UButton>
    </div>
    <div ref="scroll" class="pdf-scroll">
      <div
        class="pdf-stage"
        :style="{ width: width + 'px', height: height + 'px', '--scale-factor': scale }"
      >
        <canvas ref="canvas" :style="{ width: width + 'px', height: height + 'px' }"></canvas>
        <div ref="textHost" class="textLayer"></div>
        <div
          v-if="markerStyle"
          ref="marker"
          class="pdf-source-highlight"
          :style="markerStyle"
          aria-hidden="true"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.pdf-source-highlight {
  position: absolute;
  z-index: 2;
  pointer-events: none;
  border: 2px solid #a05000;
  background: rgb(255 183 49 / 23%);
}
.pdf-container {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}
.pdf-toolbar {
  min-height: 44px;
  font-size: 14px;
}
.pdf-count {
  font-size: 14px;
  font-variant-numeric: tabular-nums;
}
.pdf-scroll {
  overflow-y: auto;
  overflow-x: auto;
  flex: 1;
  min-height: 100px;
  background: var(--ui-bg-muted);
  padding: 12px;
}
.pdf-stage {
  position: relative;
  box-sizing: content-box;
  margin: 0 auto;
  overflow: hidden;
  background: var(--ui-bg);
  border: 1px solid var(--ui-border);
  --user-unit: 1;
  --total-scale-factor: var(--scale-factor);
  --min-font-size: 1;
  --scale-round-x: 1px;
  --scale-round-y: 1px;
}
.pdf-stage canvas {
  display: block;
}
.textLayer {
  position: absolute;
  inset: 0;
  overflow: hidden;
  contain: layout paint;
  line-height: 1;
  text-size-adjust: none;
  z-index: 2;
}
.textLayer :deep(:is(span, br)) {
  color: transparent;
  position: absolute;
  white-space: pre;
  cursor: text;
  transform-origin: 0 0;
}
.textLayer :deep(::selection) {
  background: color-mix(in srgb, var(--ui-info) 35%, transparent);
}
.page-input {
  width: 72px;
}
</style>

<script setup lang="ts">
import type { PDFDocumentProxy, PDFDocumentLoadingTask, RenderTask } from 'pdfjs-dist'
import type { PdfRegionDto } from '~/generated/types.gen'
const props = defineProps<{
  sourceUrl: string
  page: number
  region?: PdfRegionDto | null
  notes?: {
    id: string
    page: number
    rect: number[]
    text: string
    deleted: boolean
    position_id: string
  }[]
  annotate?: boolean
}>()
const emit = defineEmits<{
  page: [page: number]
  selection: [selection: { rect: number[]; text: string }]
  loaded: [count: number]
}>()
const canvas = ref<HTMLCanvasElement>(),
  stage = ref<HTMLElement>(),
  textHost = ref<HTMLElement>(),
  scroll = ref<HTMLElement>(),
  zoom = ref(1),
  count = ref(0),
  busy = ref(false),
  error = ref(''),
  width = ref(0),
  height = ref(0),
  scale = ref(1),
  pending = ref<number[] | null>(null)
let documentPdf: PDFDocumentProxy | null = null,
  loading: PDFDocumentLoadingTask | null = null,
  renderTask: RenderTask | null = null,
  epoch = 0,
  resizeObserver: ResizeObserver | undefined,
  first: { x: number; y: number } | null = null
function percent(rect: number[]) {
  return {
    left: rect[0]! * 100 + '%',
    top: rect[1]! * 100 + '%',
    width: (rect[2]! - rect[0]!) * 100 + '%',
    height: (rect[3]! - rect[1]!) * 100 + '%',
  }
}
const marks = computed(
  () =>
    props.region?.highlights.map((h) => ({
      ...h,
      style: percent([
        h.bbox[0]! / props.region!.width,
        h.bbox[1]! / props.region!.height,
        h.bbox[2]! / props.region!.width,
        h.bbox[3]! / props.region!.height,
      ]),
    })) || [],
)
async function render() {
  if (!documentPdf || !canvas.value || !textHost.value || !scroll.value) return
  const current = ++epoch
  renderTask?.cancel()
  busy.value = true
  error.value = ''
  pending.value = null
  try {
    const page = await documentPdf.getPage(Math.max(1, Math.min(documentPdf.numPages, props.page)))
    if (current !== epoch) return
    const pdfjs = await import('pdfjs-dist'),
      fit = Math.max(200, scroll.value.clientWidth - 24) / page.getViewport({ scale: 1 }).width,
      viewport = page.getViewport({ scale: Math.min(fit * zoom.value, 4) }),
      ratio = Math.min(window.devicePixelRatio || 1, 2)
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
    scroll.value.scrollTop = props.region?.highlights[0]
      ? Math.max(
          0,
          (props.region.highlights[0].bbox[1]! / props.region.height) * height.value -
            scroll.value.clientHeight / 3,
        )
      : 0
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
function point(e: PointerEvent) {
  const box = stage.value!.getBoundingClientRect()
  return {
    x: Math.max(0, Math.min(1, (e.clientX - box.left) / box.width)),
    y: Math.max(0, Math.min(1, (e.clientY - box.top) / box.height)),
  }
}
function down(e: PointerEvent) {
  if (!props.annotate) return
  first = point(e)
  ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  e.preventDefault()
}
function move(e: PointerEvent) {
  if (!first) return
  const last = point(e)
  pending.value = [
    Math.min(first.x, last.x),
    Math.min(first.y, last.y),
    Math.max(first.x, last.x),
    Math.max(first.y, last.y),
  ]
}
function up(e: PointerEvent) {
  move(e)
  first = null
  if (
    pending.value &&
    pending.value[2]! - pending.value[0]! > 0.002 &&
    pending.value[3]! - pending.value[1]! > 0.002
  )
    emit('selection', { rect: pending.value, text: '' })
}
function selectedText() {
  const selection = window.getSelection()
  if (
    !selection?.rangeCount ||
    selection.isCollapsed ||
    !stage.value?.contains(selection.anchorNode) ||
    !stage.value.contains(selection.focusNode)
  ) {
    error.value = 'Выделите текст в PDF, затем нажмите «Отметить текст».'
    return
  }
  const boxes = [...selection.getRangeAt(0).getClientRects()],
    box = stage.value.getBoundingClientRect()
  if (!boxes.length) return
  const rect = [
    Math.max(0, (Math.min(...boxes.map((r) => r.left)) - box.left) / box.width),
    Math.max(0, (Math.min(...boxes.map((r) => r.top)) - box.top) / box.height),
    Math.min(1, (Math.max(...boxes.map((r) => r.right)) - box.left) / box.width),
    Math.min(1, (Math.max(...boxes.map((r) => r.bottom)) - box.top) / box.height),
  ]
  emit('selection', { rect, text: selection.toString().slice(0, 4000) })
}
function changePage(value: number) {
  emit('page', Math.max(1, Math.min(count.value, value || 1)))
}
watch(() => props.sourceUrl, open)
watch(() => props.page, render)
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
  const value = query.trim().toLocaleLowerCase('ru-RU'),
    hits: number[] = [],
    doc = documentPdf
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
defineExpose({
  selectedText,
  find,
  clearSelection: () => {
    pending.value = null
  },
})
</script>
<template>
  <div class="pdf-container">
    <div class="toolbar pane-toolbar">
      <UButton
        size="xs"
        variant="outline"
        color="neutral"
        aria-label="Предыдущая страница PDF"
        :disabled="page <= 1"
        @click="changePage(page - 1)"
        >←</UButton
      ><UInput
        type="number"
        :model-value="page"
        :min="1"
        :max="count"
        aria-label="Физическая страница PDF"
        class="page-input"
        @change="changePage(Number(($event.target as HTMLInputElement).value))"
      /><span class="muted">из {{ count || '…' }}</span
      ><UButton
        size="xs"
        variant="outline"
        color="neutral"
        aria-label="Следующая страница PDF"
        :disabled="page >= count"
        @click="changePage(page + 1)"
        >→</UButton
      ><UButton
        size="xs"
        variant="outline"
        color="neutral"
        aria-label="Уменьшить PDF"
        @click="zoom = Math.max(0.5, zoom - 0.5)"
        >⌕ −</UButton
      ><UButton
        size="xs"
        variant="outline"
        color="neutral"
        aria-label="Увеличить PDF"
        @click="zoom = Math.min(4, zoom + 0.5)"
        >⌕ +</UButton
      ><UButton size="xs" variant="ghost" color="neutral" @click="zoom = 1">По ширине</UButton
      ><span v-if="busy" class="muted" role="status">Открываем…</span>
    </div>
    <div v-if="error" class="error-box" role="alert">
      {{ error }} <UButton size="xs" @click="open">Повторить</UButton>
    </div>
    <div ref="scroll" class="pdf-scroll">
      <div
        ref="stage"
        class="pdf-stage"
        :style="{ width: width + 'px', height: height + 'px', '--scale-factor': scale }"
      >
        <canvas ref="canvas" :style="{ width: width + 'px', height: height + 'px' }"></canvas>
        <div ref="textHost" class="textLayer"></div>
        <span
          v-for="(mark, i) in marks"
          :key="i"
          class="pdf-mark"
          :class="mark.kind"
          :style="mark.style"
          :title="mark.text"
        ></span
        ><span
          v-for="note in (notes || []).filter((n) => n.page === page && !n.deleted)"
          :key="note.id"
          class="pdf-note"
          :style="percent(note.rect)"
          :title="note.text"
        ></span
        ><span v-if="pending" class="pdf-note pending" :style="percent(pending)"></span>
        <div
          v-if="annotate"
          class="pdf-draw"
          @pointerdown="down"
          @pointermove="move"
          @pointerup="up"
          @pointercancel="first = null"
        ></div>
      </div>
    </div>
  </div>
</template>
<style scoped>
.pdf-container {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}
.pdf-scroll {
  overflow: auto;
  flex: 1;
  min-height: 100px;
  background: var(--background-color-elevated);
  padding: 12px;
}
.pdf-stage {
  position: relative;
  margin: 0 auto;
  background: var(--background-color-default);
  box-shadow: var(--shadow-panel);
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
  overflow: clip;
  line-height: 1;
  text-align: initial;
  text-size-adjust: none;
  transform-origin: 0 0;
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
  background: var(--color-info-200);
}
.pdf-mark,
.pdf-note {
  position: absolute;
  background: color-mix(in srgb, var(--color-warning-300) 40%, transparent);
  outline: 1px solid var(--color-warning-500);
  pointer-events: none;
  z-index: 3;
}
.pdf-mark.quantity {
  outline: 2px solid var(--color-warning-600);
}
.pdf-note {
  background: color-mix(in srgb, var(--color-warning-300) 30%, transparent);
  outline: 2px solid var(--color-warning-500);
}
.pending {
  outline-style: dashed;
}
.pdf-draw {
  position: absolute;
  inset: 0;
  z-index: 4;
  cursor: crosshair;
  touch-action: none;
}
.page-input {
  width: 66px;
}
</style>

<script setup lang="ts">
import type { PDFDocumentProxy, PDFDocumentLoadingTask, RenderTask } from 'pdfjs-dist'
import type { PdfRegionDto } from '~/generated/types.gen'
import { clampPage } from '~/utils/presentation'
import { selectionRectangles, selectionEnvelope } from '~/utils/pdfSelection'
import { evidenceRects } from '~/utils/normativePresentation'
const props = defineProps<{
  sourceUrl: string
  page: number
  focusText?: string
  focusRects?: number[][]
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
  selectionMode?: 'row' | 'area' | 'text'
}>()
const emit = defineEmits<{
  page: [page: number]
  selection: [
    selection: { rect: number[]; text: string; rects?: number[][]; mode?: 'row' | 'area' | 'text' },
  ]
  loaded: [count: number]
  focused: [found: boolean]
}>()
const canvas = ref<HTMLCanvasElement>(),
  stage = ref<HTMLElement>(),
  textHost = ref<HTMLElement>(),
  scroll = ref<HTMLElement>(),
  zoom = ref(1),
  count = ref(0),
  busy = ref(false),
  ready = ref(false),
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
const focusMarks = ref<number[][]>([])
function focusAnchor() {
  focusMarks.value = []
  const rects = evidenceRects(props.focusRects)
  if (rects.length && scroll.value) {
    focusMarks.value = rects
    scroll.value.scrollTop = Math.max(
      0,
      Math.min(...rects.map((r) => r[1]!)) * height.value - scroll.value.clientHeight / 3,
    )
    emit('focused', true)
    return
  }
  const target = (props.focusText ?? '')
    .normalize('NFKC')
    .toLocaleLowerCase('ru-RU')
    .replace(/[\s\u00ad]/g, '')
  if (!target || !textHost.value || !canvas.value || !scroll.value) return
  const owners: { node: Text; offset: number }[] = []
  let content = ''
  const walker = document.createTreeWalker(textHost.value, NodeFilter.SHOW_TEXT)
  while (walker.nextNode()) {
    if (content.length > 150000) {
      emit('focused', false)
      return
    }
    const node = walker.currentNode as Text
    for (let offset = 0; offset < node.data.length; offset++) {
      const folded = node.data[offset]!.normalize('NFKC')
        .toLocaleLowerCase('ru-RU')
        .replace(/[\s\u00ad]/g, '')
      content += folded
      for (let i = 0; i < folded.length; i++) owners.push({ node, offset })
    }
  }
  let start = content.indexOf(target)
  while (start >= 0 && /^[\d.-]+$/.test(target) && /\d/.test(content[start + target.length] ?? ''))
    start = content.indexOf(target, start + 1)
  const first = owners[start],
    last = owners[start + target.length - 1]
  if (start < 0 || !first || !last) {
    emit('focused', false)
    return
  }
  const range = document.createRange()
  range.setStart(first.node, first.offset)
  range.setEnd(last.node, last.offset + 1)
  const box = canvas.value.getBoundingClientRect()
  focusMarks.value = [...range.getClientRects()]
    .filter((r) => r.width > 0 && r.height > 0)
    .map((r) => [
      Math.max(0, (r.left - box.left) / box.width),
      Math.max(0, (r.top - box.top) / box.height),
      Math.min(1, (r.right - box.left) / box.width),
      Math.min(1, (r.bottom - box.top) / box.height),
    ])
  const top = focusMarks.value[0]?.[1]
  if (top !== undefined)
    scroll.value.scrollTop = Math.max(0, top * height.value - scroll.value.clientHeight / 3)
  emit('focused', focusMarks.value.length > 0)
}
async function render() {
  if (!documentPdf || !canvas.value || !textHost.value || !scroll.value) return
  const current = ++epoch
  renderTask?.cancel()
  busy.value = true
  error.value = ''
  pending.value = null
  focusMarks.value = []
  try {
    const page = await documentPdf.getPage(clampPage(props.page, documentPdf.numPages))
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
    ready.value = true
    scroll.value.scrollTop = props.region?.highlights[0]
      ? Math.max(
          0,
          (props.region.highlights[0].bbox[1]! / props.region.height) * height.value -
            scroll.value.clientHeight / 3,
        )
      : 0
    focusAnchor()
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
function point(e: PointerEvent) {
  const box = canvas.value!.getBoundingClientRect()
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
  if (!first) return
  const anchor = first
  move(e)
  first = null
  if (props.selectionMode === 'row') {
    const epsilon = 0.0005
    const rect = [
      Math.max(0, anchor.x - epsilon),
      Math.max(0, anchor.y - epsilon),
      Math.min(1, anchor.x + epsilon),
      Math.min(1, anchor.y + epsilon),
    ]
    pending.value = null
    emit('selection', { rect, text: '', mode: 'row' })
    return
  }
  if (
    pending.value &&
    pending.value[2]! - pending.value[0]! > 0.002 &&
    pending.value[3]! - pending.value[1]! > 0.002
  )
    emit('selection', { rect: pending.value, text: '', mode: 'area' })
}
function selectedText(silent = false) {
  const selection = window.getSelection()
  if (
    !selection?.rangeCount ||
    selection.isCollapsed ||
    !stage.value?.contains(selection.anchorNode) ||
    !stage.value.contains(selection.focusNode) ||
    !canvas.value
  ) {
    if (!silent) error.value = 'Выделите текст мышью внутри страницы PDF.'
    return
  }
  const rects = selectionRectangles(
    [...selection.getRangeAt(0).getClientRects()],
    canvas.value.getBoundingClientRect(),
  )
  if (!rects.length) return
  error.value = ''
  pending.value = null
  emit('selection', {
    rect: selectionEnvelope(rects),
    rects,
    text: selection.toString(),
    mode: 'text',
  })
}
let selectionTimer: ReturnType<typeof setTimeout> | undefined
let textPointerDown = false
function textPointerStart() {
  if (props.annotate && props.selectionMode === 'text') textPointerDown = true
}
function textPointerEnd() {
  if (textPointerDown) {
    textPointerDown = false
    textSelectionChanged()
  }
}
function textSelectionChanged() {
  if (!props.annotate || props.selectionMode !== 'text') return
  clearTimeout(selectionTimer)
  selectionTimer = setTimeout(() => {
    if (!textPointerDown) selectedText(true)
  }, 180)
}
function clearSelection() {
  pending.value = null
  const selection = window.getSelection()
  if (selection?.anchorNode && stage.value?.contains(selection.anchorNode))
    selection.removeAllRanges()
}
function changePage(value: number) {
  emit('page', clampPage(value, count.value))
}
watch(() => props.sourceUrl, open)
watch(() => props.page, render)
watch(() => props.focusText, render)
watch(() => props.focusRects, render, { deep: true })
watch(zoom, render)
watch(
  () => props.region,
  async (region) => {
    await nextTick()
    if (region?.highlights[0] && scroll.value && ready.value)
      scroll.value.scrollTop = Math.max(
        0,
        (region.highlights[0].bbox[1]! / region.height) * height.value -
          scroll.value.clientHeight / 3,
      )
  },
)
watch(() => props.selectionMode, clearSelection)

onMounted(() => {
  open()
  document.addEventListener('selectionchange', textSelectionChanged)
  document.addEventListener('pointerup', textPointerEnd)
  let timer: ReturnType<typeof setTimeout>
  resizeObserver = new ResizeObserver(() => {
    clearTimeout(timer)
    timer = setTimeout(render, 150)
  })
  if (scroll.value) resizeObserver.observe(scroll.value)
})
onBeforeUnmount(() => {
  document.removeEventListener('selectionchange', textSelectionChanged)
  document.removeEventListener('pointerup', textPointerEnd)
  clearTimeout(selectionTimer)
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
  clearSelection,
})
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
        ref="stage"
        class="pdf-stage"
        :style="{ width: width + 'px', height: height + 'px', '--scale-factor': scale }"
      >
        <canvas ref="canvas" :style="{ width: width + 'px', height: height + 'px' }"></canvas>
        <div ref="textHost" class="textLayer" @pointerdown="textPointerStart"></div>
        <div
          v-for="(rect, index) in focusMarks"
          :key="`anchor-${index}`"
          class="pdf-anchor-mark"
          :style="percent(rect)"
          aria-hidden="true"
        ></div>
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
          v-if="annotate && selectionMode !== 'text'"
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
.pdf-anchor-mark {
  position: absolute;
  z-index: 3;
  pointer-events: none;
  background: color-mix(in srgb, var(--color-amber-400) 30%, transparent);
  outline: 2px solid var(--ui-primary);
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
  overflow: auto;
  flex: 1;
  min-height: 100px;
  background: var(--ui-bg-muted);
  padding: 12px;
}
.pdf-stage {
  position: relative;
  box-sizing: content-box;
  margin: 0 auto;
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
  background: color-mix(in srgb, var(--ui-info) 35%, transparent);
}
.pdf-mark,
.pdf-note {
  position: absolute;
  background: color-mix(in srgb, var(--color-amber-400) 22%, transparent);
  outline: 2px solid var(--color-amber-700);
  outline-offset: -2px;
  pointer-events: none;
  z-index: 3;
}
.pdf-mark.quantity {
  outline-width: 3px;
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
  width: 72px;
}
</style>

<script setup lang="ts">
import type { ReviewHistoryDto, PreparedPageDto, SpecifyBasisRequest } from '~/generated/types.gen'
const props = defineProps<{ runId: string; positionIndex: number; page: number }>()
const emit = defineEmits<{ restore: [selection: SpecifyBasisRequest] }>()
const backend = useBackend()
const history = ref<ReviewHistoryDto | null>(null),
  extraction = ref<PreparedPageDto | null>(null),
  busy = ref(false),
  error = ref('')
async function load() {
  busy.value = true
  error.value = ''
  try {
    history.value = await backend.reviewHistory(props.runId, props.positionIndex)
  } catch (e) {
    error.value = readableError(e)
  } finally {
    busy.value = false
  }
}
async function loadExtraction() {
  busy.value = true
  error.value = ''
  try {
    extraction.value = await backend.preparedPage(props.runId, props.positionIndex, props.page)
  } catch (e) {
    error.value = readableError(e)
  } finally {
    busy.value = false
  }
}
function date(value: string) {
  return new Date(value).toLocaleString('ru-RU')
}
defineExpose({ load })
</script>
<template>
  <section aria-label="История и OCR">
    <UCollapsible>
      <UButton label="История, черновики и OCR" variant="soft" block @click="load" />
      <template #content>
        <p class="muted">
          Машинные результаты, черновики и подтверждения хранятся отдельно. Распознавание
          сохраняется сразу, до подтверждения основания.
        </p>
        <UButton label="Обновить историю" variant="ghost" :loading="busy" @click="load" />
        <UButton
          :label="`Сохранённый текст страницы ${page}`"
          variant="ghost"
          :loading="busy"
          @click="loadExtraction"
        />
        <p v-if="error" class="error-box" role="alert">{{ error }}</p>
        <div v-if="extraction" class="history-card">
          <strong
            >Снимок подготовки · стр. {{ extraction.page }} ·
            {{
              extraction.origin === 'ocr'
                ? 'OCR'
                : extraction.origin === 'native'
                  ? 'Текст PDF'
                  : 'Извлечённый текст'
            }}</strong
          >
          <p v-if="extraction.pending">OCR этой страницы не был завершён при подготовке позиции.</p>
          <p v-if="extraction.error">{{ extraction.error }}</p>
          <p class="muted">Хранилище: {{ extraction.blob_key }} · SHA256 {{ extraction.sha256 }}</p>
          <pre>{{ extraction.text || 'В снимке нет текста этой страницы.' }}</pre>
          <p v-if="extraction.truncated">Показаны первые 20 000 символов.</p>
        </div>
        <template v-if="history">
          <h3>Машинные версии · {{ history.results.length }}</h3>
          <details v-for="item in history.results" :key="item.id" class="history-card">
            <summary>
              {{ date(item.created_at) }} · {{ item.status }} · {{ item.found_volume }}
              {{ item.found_unit }}
            </summary>
            <p>{{ item.details }}</p>
            <p class="muted">Версия {{ item.id }}. Сохранён исходный результат алгоритма.</p>
          </details>
          <h3>Черновики и распознавания · {{ history.artifacts.length }}</h3>
          <details v-for="item in history.artifacts" :key="item.id" class="history-card">
            <summary>
              {{ item.kind === 'ocr' ? 'OCR' : 'Черновик' }} · {{ date(item.created_at) }} ·
              {{ item.actor }}
            </summary>
            <p v-if="item.preview.ocr_key" class="muted">
              OCR-хранилище: {{ item.preview.ocr_key }}
            </p>
            <UButton
              label="Продолжить с этого варианта"
              size="xs"
              @click="emit('restore', item.selection)"
            />
            <p class="muted">
              {{ item.preview.filename }} · {{ item.preview.fragments?.length || 1 }} областей
            </p>
            <div v-for="(fragment, index) in item.preview.fragments" :key="index">
              <p>Область {{ index + 1 }} · стр. {{ fragment.page }}</p>
              <img
                v-if="fragment.image_key"
                :src="backend.reviewImageUrl(runId, positionIndex, 'artifact', item.id, index)"
                :alt="`Сохранённая область ${index + 1}`"
                loading="lazy"
              />
              <p>Исходный OCR: {{ fragment.ocr_text ?? 'не запускался' }}</p>
              <p>Текст: {{ fragment.corrected_text ?? fragment.text }}</p>
              <p>{{ fragment.text_checked ? 'Текст проверен' : 'Текст не проверен' }}</p>
            </div>
            <pre v-if="!item.preview.fragments?.length">{{ item.preview.quote }}</pre>
          </details>
          <h3>Основания и отмены · {{ history.bases.length }}</h3>
          <details v-for="item in history.bases" :key="item.id" class="history-card">
            <summary>
              {{ item.revoked_at ? 'Отменено' : 'Подтверждено' }} · {{ date(item.created_at) }} ·
              {{ item.actor }}
            </summary>
            <p>{{ item.quote }}</p>
            <p>{{ item.comment }}</p>
            <p v-if="item.revoked_at">
              Отменил {{ item.revoked_by }} · {{ date(item.revoked_at) }}
            </p>
            <p v-if="item.result_version_id" class="muted">
              Машинная версия: {{ item.result_version_id }}
            </p>
            <div v-for="(fragment, index) in item.fragments" :key="index">
              <img
                v-if="fragment.image_key"
                :src="backend.reviewImageUrl(runId, positionIndex, 'basis', item.id, index)"
                :alt="`Основание: область ${index + 1}`"
                loading="lazy"
              />
              <p>Исходный OCR: {{ fragment.ocr_text ?? 'не запускался' }}</p>
              <p>Исправленный текст: {{ fragment.corrected_text ?? 'без правок' }}</p>
              <p>{{ fragment.text_checked ? 'Текст проверен' : 'Текст не проверен' }}</p>
            </div>
          </details>
        </template>
      </template>
    </UCollapsible>
  </section>
</template>
<style scoped>
.history-card {
  padding: 10px;
  margin: 8px 0;
  border: 1px solid var(--ui-border);
  border-radius: 6px;
  overflow-wrap: anywhere;
}
pre {
  white-space: pre-wrap;
  max-height: 300px;
  overflow: auto;
  font-size: 12px;
}
img {
  max-width: 100%;
  max-height: 220px;
  object-fit: contain;
  border: 1px solid var(--ui-border);
}
h3 {
  margin-top: 12px;
}
</style>

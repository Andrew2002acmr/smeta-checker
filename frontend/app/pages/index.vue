<script setup lang="ts">
const backend = useBackend(),
  error = ref('')
onMounted(async () => {
  try {
    await backend.connect()
    await navigateTo('/history', { replace: true })
  } catch (e) {
    error.value = readableError(e)
  }
})
</script>
<template>
  <main class="shell">
    <h1 class="page-title">КДФП · Проверка ВОР</h1>
    <p v-if="error" class="error-box" role="alert">{{ error }}</p>
    <p v-else>Открываем сохранённые проверки…</p>
    <UButton to="/upload">Новая проверка</UButton>
  </main>
</template>

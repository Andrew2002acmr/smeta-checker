<script setup lang="ts">
const props = defineProps<{
  active?: 'check' | 'format' | 'knowledge' | 'projects' | 'create'
  wide?: boolean
}>()
const route = useRoute()
const activeTab = computed(() => {
  if (props.active === 'format' || route.path === '/formatting') return 'format'
  if (props.active === 'knowledge' || route.path === '/knowledge-base') return 'knowledge'
  return 'check'
})
const backend = useBackend()
const connected = useState('workspace-connected', () => false)
const signingOut = ref(false)
const logoutError = ref('')
async function logout() {
  if (signingOut.value) return
  signingOut.value = true
  logoutError.value = ''
  try {
    await backend.disconnect()
    await navigateTo('/login', { replace: true })
  } catch {
    logoutError.value = 'Не удалось выйти. Повторите попытку.'
  } finally {
    signingOut.value = false
  }
}
</script>

<template>
  <a class="skip-link" href="#main-content">К содержимому</a>
  <header class="app-header" :class="{ 'app-header-wide': wide }">
    <NuxtLink to="/" class="brand" aria-label="КДФП — все проекты">
      <img class="brand-logo dark:hidden" src="/kdfp-logo-light.svg" alt="" />
      <img class="brand-logo hidden dark:block" src="/kdfp-logo-dark.svg" alt="" />
    </NuxtLink>
    <nav class="header-navigation" aria-label="Основная навигация">
      <UButton
        to="/"
        color="neutral"
        :variant="activeTab === 'check' ? 'soft' : 'ghost'"
        :aria-current="activeTab === 'check' ? 'page' : undefined"
        icon="i-lucide-layout-dashboard"
        >Проверка ВОР</UButton
      >
      <UButton
        to="/formatting"
        color="neutral"
        :variant="activeTab === 'format' ? 'soft' : 'ghost'"
        :aria-current="activeTab === 'format' ? 'page' : undefined"
        icon="i-lucide-spell-check"
        >Оформление ВОР</UButton
      >
      <UButton
        to="/knowledge-base"
        color="neutral"
        :variant="activeTab === 'knowledge' ? 'soft' : 'ghost'"
        :aria-current="activeTab === 'knowledge' ? 'page' : undefined"
        icon="i-lucide-book-open"
        >Проверка по нормативам</UButton
      >
      <UColorModeButton
        class="theme-button"
        aria-label="Переключить светлую и тёмную тему"
        title="Переключить тему"
      />
      <UButton
        v-if="connected && route.path !== '/login'"
        color="neutral"
        variant="ghost"
        :loading="signingOut"
        @click="logout"
        >Выйти</UButton
      >
    </nav>
  </header>
  <p v-if="logoutError" class="error-box shell" role="alert">{{ logoutError }}</p>
</template>

<style scoped>
.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  min-height: 72px;
  padding-block: 14px;
  margin: 0 auto;
  width: calc(100% - 64px);
  max-width: 1376px;
  border-bottom: 1px solid var(--ui-border-muted);
}
.app-header-wide {
  max-width: none;
}
.brand {
  display: inline-flex;
  align-items: center;
  gap: 16px;
  min-height: 44px;
  color: var(--ui-text-highlighted);
  white-space: nowrap;
}
.brand-logo {
  width: 128px;
  height: auto;
}
.header-navigation {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: flex-end;
  min-width: 0;
}
.theme-button {
  min-width: 44px;
}
@media (max-width: 640px) {
  .app-header {
    flex-wrap: wrap;
    gap: 8px;
    min-height: 64px;
    padding-block: 10px;
    width: calc(100% - 32px);
  }
  .brand {
    gap: 12px;
  }
  .header-navigation {
    width: 100%;
    justify-content: flex-start;
    overflow-x: visible;
    flex-wrap: wrap;
    padding-bottom: 2px;
  }
  .header-navigation :deep(a) {
    flex: 0 0 auto;
    white-space: nowrap;
  }
  .theme-button {
    margin-inline-start: auto;
  }
}
</style>

<script setup lang="ts">
const props = defineProps<{ active?: 'check' | 'format' | 'knowledge' | 'projects' | 'create' }>()
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
  <header class="app-header">
    <NuxtLink to="/" class="brand">
      <span class="brand-mark">КД<span>ФП</span></span>
      <span class="brand-divider" aria-hidden="true"></span>
      <span class="brand-product">Проверка ВОР</span>
      <span class="visually-hidden"> — все проекты</span>
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
  padding-block: 28px 24px;
  margin: 0 auto 8px;
  width: calc(100% - 64px);
  max-width: 1376px;
  border-bottom: 1px solid var(--ui-border);
}
.brand {
  display: inline-flex;
  align-items: center;
  gap: 16px;
  min-height: 44px;
  color: var(--ui-text-highlighted);
  white-space: nowrap;
}
.brand-mark {
  font-size: 24px;
  font-weight: 800;
  letter-spacing: -0.04em;
}
.brand-mark span {
  color: var(--ui-primary);
}
.brand-divider {
  height: 24px;
  width: 1px;
  background: var(--ui-border);
}
.brand-product {
  font-size: 16px;
  font-weight: 650;
}
.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
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
    gap: 12px;
    padding-block: 16px;
    margin-bottom: 8px;
    width: calc(100% - 32px);
  }
  .brand {
    gap: 12px;
  }
  .header-navigation {
    width: 100%;
    justify-content: flex-start;
    overflow-x: auto;
    flex-wrap: nowrap;
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

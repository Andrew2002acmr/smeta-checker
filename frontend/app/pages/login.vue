<script setup lang="ts">
const backend = useBackend(),
  route = useRoute(),
  password = ref(''),
  showPassword = ref(false),
  loginName = ref(''),
  mode = ref<'local' | 'ldap' | null>(null),
  error = ref(''),
  busy = ref(false),
  checking = ref(true)
const corporate = computed(() => mode.value === 'ldap')
const back = computed(() => safeInternalRedirect(route.query.back))
async function login() {
  if (busy.value || !mode.value) return
  if (!loginName.value.trim() || !password.value) return
  busy.value = true
  error.value = ''
  try {
    await backend.connect(password.value, loginName.value.trim())
    await navigateTo(back.value, { replace: true })
  } catch (e) {
    error.value = readableError(e)
  } finally {
    busy.value = false
    password.value = ''
  }
}
onMounted(async () => {
  try {
    const gate = await backend.gate()
    mode.value = gate.mode
  } catch (e) {
    error.value = readableError(e)
  } finally {
    checking.value = false
  }
})
</script>

<template>
  <div class="login-page">
    <main id="main-content" class="login-shell" tabindex="-1">
      <div class="login-center">
        <UCard class="login-card">
          <div class="login-brand">
            <img
              src="/kdfp-logo-light.svg"
              alt="КДФП"
              class="login-logo"
              width="120"
              height="120"
            />
          </div>
          <h1 class="login-heading">Вход в систему</h1>
          <p v-if="checking" class="empty" role="status">Проверяем доступ…</p>
          <template v-else>
            <p v-if="error" class="error-box" role="alert">{{ error }}</p>
            <form v-if="mode" class="login-form" @submit.prevent="login">
              <UFormField
                :label="corporate ? 'Корпоративная почта' : 'Логин'"
                name="login"
                required
              >
                <UInput
                  v-model="loginName"
                  name="username"
                  :aria-label="corporate ? 'Корпоративная почта' : 'Логин'"
                  :autocomplete="corporate ? 'email' : 'username'"
                  :leading-icon="corporate ? 'i-lucide-mail' : 'i-lucide-user'"
                  autocapitalize="none"
                  :spellcheck="false"
                  :disabled="busy"
                  autofocus
                  required
                  class="w-full"
                />
              </UFormField>
              <UFormField label="Пароль" name="password" required>
                <UInput
                  id="password"
                  v-model="password"
                  :type="showPassword ? 'text' : 'password'"
                  aria-label="Пароль"
                  autocomplete="current-password"
                  leading-icon="i-lucide-lock"
                  :disabled="busy"
                  required
                  class="w-full"
                  :ui="{ trailing: 'pe-1' }"
                >
                  <template #trailing>
                    <UButton
                      color="neutral"
                      variant="link"
                      size="sm"
                      :icon="showPassword ? 'i-lucide-eye-off' : 'i-lucide-eye'"
                      :aria-label="showPassword ? 'Скрыть пароль' : 'Показать пароль'"
                      :aria-pressed="showPassword"
                      aria-controls="password"
                      type="button"
                      @click="showPassword = !showPassword"
                    />
                  </template>
                </UInput>
              </UFormField>
              <UButton
                type="submit"
                :loading="busy"
                :disabled="!loginName.trim() || !password || busy"
                block
              >
                Войти
              </UButton>
            </form>
          </template>
        </UCard>
      </div>
    </main>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100dvh;
  background: var(--ui-bg-muted);
}
.login-shell {
  min-height: 100dvh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.login-center {
  width: 100%;
  max-width: 420px;
}
.login-card {
  padding: 32px;
}
.login-brand {
  display: flex;
  justify-content: center;
  margin-bottom: 24px;
}
.login-logo {
  width: 120px;
  height: 120px;
  border-radius: 12px;
  flex-shrink: 0;
}
.login-heading {
  font-size: 20px;
  font-weight: 600;
  margin: 0 0 20px;
  color: var(--ui-text);
  text-align: center;
}
.login-form {
  display: grid;
  gap: 16px;
}
/* Hide the native password reveal button in Edge/IE. */
::-ms-reveal {
  display: none;
}
@media (max-width: 640px) {
  .login-card {
    padding: 24px;
  }
  .login-logo {
    width: 96px;
    height: 96px;
  }
  .login-center {
    max-width: 100%;
  }
}
</style>

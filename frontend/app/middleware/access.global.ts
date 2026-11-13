export default defineNuxtRouteMiddleware(async (to) => {
  if (to.path === '/login') return
  try {
    await useBackend().connect()
  } catch {
    return navigateTo({ path: '/login', query: { back: to.fullPath } }, { replace: true })
  }
})

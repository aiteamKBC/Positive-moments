<script setup lang="ts">
/**
 * The last step of "Sign in with Microsoft".
 *
 * The backend hands over a one-time code in the URL fragment, which browsers
 * never send to a server. It is wiped from the address bar at once and traded
 * for the API token; the code is useless a second time or after two minutes.
 */
import { onMounted } from 'vue'
import { useRouter } from 'vue-router'

import BrandIdentity from '../components/BrandIdentity.vue'
import { completeMicrosoftSignIn } from '../services/api'

const router = useRouter()

onMounted(async () => {
  const code = new window.URLSearchParams(window.location.hash.slice(1)).get('code')
  window.history.replaceState(null, '', window.location.pathname)
  try {
    if (!code) throw new Error('missing code')
    await router.replace(await completeMicrosoftSignIn(code))
  } catch {
    await router.replace({ name: 'login', query: { sso_error: 'handoff_failed' } })
  }
})
</script>

<template>
  <main class="grid min-h-screen place-items-center bg-white px-5">
    <div class="text-center">
      <BrandIdentity size="md" />
      <p class="mt-6 text-sm text-muted">Signing you in…</p>
    </div>
  </main>
</template>

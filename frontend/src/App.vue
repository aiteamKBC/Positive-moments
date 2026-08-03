<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { clearToken, hasToken, logout } from './services/api'

const route = useRoute()
const router = useRouter()
const showShell = computed(() => route.name !== 'login')

async function signOut() {
  try {
    await logout()
  } finally {
    clearToken()
    await router.push({ name: 'login' })
  }
}
</script>

<template>
  <div v-if="showShell" class="min-h-screen bg-cream">
    <header class="border-b border-slate-200/80 bg-white">
      <div class="flex h-16 w-full items-center justify-between px-4 sm:px-6 lg:px-8">
        <RouterLink to="/lectures" class="flex items-center gap-3" aria-label="Positive Mentions home">
          <span class="grid h-9 w-9 place-items-center rounded-xl bg-brand-600 text-white shadow-sm">
            <svg viewBox="0 0 24 24" class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M7 8h10M7 12h6m-7 8 3.5-3H18a3 3 0 0 0 3-3V6a3 3 0 0 0-3-3H6a3 3 0 0 0-3 3v8a3 3 0 0 0 3 3v3Z"/>
            </svg>
          </span>
          <span>
            <span class="block text-sm font-bold tracking-tight text-ink">Positive Mentions</span>
            <span class="hidden text-xs text-muted sm:block">Learner experience intelligence</span>
          </span>
        </RouterLink>
        <button v-if="hasToken()" class="btn-secondary !px-3 !py-2" type="button" @click="signOut">
          Sign out
        </button>
      </div>
    </header>
    <main>
      <RouterView />
    </main>
  </div>
  <RouterView v-else />
</template>

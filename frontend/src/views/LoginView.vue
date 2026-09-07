<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { login } from '../services/api'
import { errorMessage } from '../utils/format'

const username = ref('')
const password = ref('')
const loading = ref(false)
const error = ref('')
const router = useRouter()
const route = useRoute()

async function submit() {
  loading.value = true
  error.value = ''
  try {
    await login(username.value, password.value)
    const destination = typeof route.query.next === 'string' ? route.query.next : '/lectures'
    await router.replace(destination)
  } catch (caught) {
    error.value = errorMessage(caught, 'Sign in failed. Check your credentials and try again.')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <main class="relative grid min-h-screen place-items-center overflow-hidden bg-ink px-4 py-12">
    <div class="absolute -left-32 top-1/3 h-80 w-80 rounded-full bg-brand-500/20 blur-3xl" />
    <div class="absolute -right-20 top-0 h-96 w-96 rounded-full bg-cyan-400/10 blur-3xl" />
    <section class="relative w-full max-w-md rounded-3xl bg-white p-7 shadow-2xl sm:p-9">
      <div class="grid h-12 w-12 place-items-center rounded-2xl bg-brand-600 text-white">
        <svg viewBox="0 0 24 24" class="h-6 w-6" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M7 8h10M7 12h6m-7 8 3.5-3H18a3 3 0 0 0 3-3V6a3 3 0 0 0-3-3H6a3 3 0 0 0-3 3v8a3 3 0 0 0 3 3v3Z"/>
        </svg>
      </div>
      <p class="mt-6 text-xs font-bold uppercase tracking-[0.2em] text-brand-700">Internal workspace</p>
      <h1 class="mt-2 text-3xl font-bold tracking-tight text-ink">Welcome to Positive Mentions</h1>
      <p class="mt-2 text-sm leading-6 text-muted">Sign in with the shared dashboard account.</p>

      <form class="mt-8 space-y-5" @submit.prevent="submit">
        <div>
          <label for="username" class="label">Username</label>
          <input id="username" v-model="username" class="field" autocomplete="username" required autofocus />
        </div>
        <div>
          <label for="password" class="label">Password</label>
          <input id="password" v-model="password" type="password" class="field" autocomplete="current-password" required />
        </div>
        <p v-if="error" class="rounded-xl bg-rose-50 px-4 py-3 text-sm text-rose-700" role="alert">{{ error }}</p>
        <button type="submit" class="btn-primary w-full" :disabled="loading">
          {{ loading ? 'Signing in…' : 'Sign in' }}
        </button>
      </form>
      <p class="mt-6 text-center text-xs text-slate-400">Authorized access only</p>
    </section>
  </main>
</template>

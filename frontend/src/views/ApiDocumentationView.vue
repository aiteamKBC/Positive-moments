<script setup lang="ts">
/**
 * "API docs": takes a signed-in Dashboard user straight to the LMS API documentation (Swagger UI at
 * /api/docs/), with no second sign-in. Someone not signed in reaches this page only after the router
 * has sent them through Microsoft sign-in.
 *
 * The backend sends anyone whose documentation access has lapsed back here for a fresh grant. If the
 * browser drops the session cookie, that would bounce for ever, so a second arrival within a few
 * seconds stops and says so instead.
 */
import axios from 'axios'
import { onMounted, ref } from 'vue'

import { openApiDocumentation } from '../services/api'

const RECENT_MS = 10_000
const LAST_OPENED = 'api_docs_opened_at'
const problem = ref('')

async function open() {
  problem.value = ''
  window.sessionStorage.setItem(LAST_OPENED, String(Date.now()))
  try {
    await openApiDocumentation()
  } catch (error) {
    window.sessionStorage.removeItem(LAST_OPENED)
    const status = axios.isAxiosError(error) ? error.response?.status : undefined
    if (status === 401) return                            // the API client is already signing in again
    problem.value = status === 404
      ? 'The API documentation is not switched on.'
      : 'The API documentation could not be opened. Please try again.'
  }
}

onMounted(() => {
  const last = Number(window.sessionStorage.getItem(LAST_OPENED) || 0)
  if (Date.now() - last < RECENT_MS) {
    window.sessionStorage.removeItem(LAST_OPENED)
    problem.value = 'The API documentation could not be opened. Check that this browser accepts cookies from this site, then try again.'
    return
  }
  void open()
})
</script>

<template>
  <div class="mx-auto max-w-xl px-5 py-16 text-center">
    <template v-if="problem">
      <p class="text-sm text-muted" role="alert">{{ problem }}</p>
      <button
        type="button"
        class="mt-5 rounded-lg bg-brand-900 px-4 py-2 text-sm font-semibold text-white"
        @click="open"
      >
        Try again
      </button>
    </template>
    <p v-else class="text-sm text-muted">Opening the API documentation…</p>
  </div>
</template>

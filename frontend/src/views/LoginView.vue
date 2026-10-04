<script setup lang="ts">
/**
 * Sign in.
 *
 * The identity on this screen is Kent Business College and the product is
 * Lecture Intelligence; the old "Positive Mentions" branding is gone, because
 * positive moments are one feature inside this platform rather than the
 * platform itself.
 *
 * Microsoft 365 is the way in - the same account people use for the
 * Communication Centre. The username/password form stays as a fallback for
 * local accounts. No credential of any kind appears in this file or anywhere
 * else in the bundle.
 */
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AppIcon from '../components/AppIcon.vue'
import BrandIdentity from '../components/BrandIdentity.vue'
import { login, startMicrosoftSignIn } from '../services/api'
import { errorMessage } from '../utils/format'

const SSO_ERRORS: Record<string, string> = {
  not_configured: 'Microsoft sign-in is not set up on this server yet. Use a local account.',
  wrong_tenant: 'That Microsoft account is not a Kent Business College account.',
  account_disabled: 'This account has been switched off in Lecture Intelligence.',
  not_a_college_account: 'Sign in with your @kentbusinesscollege.com account.',
  account_conflict: 'Your Microsoft account does not match your Lecture Intelligence account. Ask an administrator.',
  microsoft_refused: 'Microsoft sign-in was cancelled or refused.',
}

const username = ref('')
const password = ref('')
const loading = ref(false)
const showLocal = ref(false)
const router = useRouter()
const route = useRoute()
const ssoError = typeof route.query.sso_error === 'string' ? route.query.sso_error : ''
const error = ref(ssoError ? (SSO_ERRORS[ssoError] ?? 'Microsoft sign-in did not complete. Please try again.') : '')
const nextPath = typeof route.query.next === 'string' ? route.query.next : '/operations'

function signInWithMicrosoft() {
  loading.value = true
  startMicrosoftSignIn(nextPath)
}

async function submit() {
  loading.value = true
  error.value = ''
  try {
    await login(username.value, password.value)
    await router.replace(nextPath)
  } catch (caught) {
    error.value = errorMessage(caught, 'Sign in failed. Check your credentials and try again.')
  } finally {
    loading.value = false
  }
}

const HIGHLIGHTS = [
  { icon: 'operations', text: 'Daily pipeline health across every teaching day' },
  { icon: 'lectures', text: 'One record per lecture, from calendar to quality report' },
  { icon: 'moments', text: 'Positive learner moments, found and evidenced' },
]
</script>

<template>
  <main class="grid min-h-screen lg:grid-cols-[minmax(0,1fr)_minmax(26rem,30rem)]">
    <!-- brand panel -->
    <section class="on-plum relative hidden overflow-hidden bg-brand-900 p-12 text-white lg:flex lg:flex-col lg:justify-between">
      <div
        class="pointer-events-none absolute inset-0 opacity-70"
        style="background:radial-gradient(60rem 40rem at 12% 8%, rgba(119,81,198,.38) 0, transparent 60%),radial-gradient(40rem 30rem at 88% 92%, rgba(213,171,78,.22) 0, transparent 55%)"
      />
      <div class="relative"><BrandIdentity size="lg" inverse /></div>

      <div class="relative max-w-xl">
        <p class="text-2xs font-bold uppercase tracking-[0.2em] text-white/50">Internal operations platform</p>
        <!-- Explicitly white: the base layer gives every heading the ink
             colour, which disappears against the plum panel. -->
        <h1 class="mt-5 font-display text-[3.25rem] leading-[1.05] text-white">
          Every lecture, accounted for.
        </h1>
        <p class="mt-5 text-base leading-7 text-white/65">
          Lecture Intelligence tracks each Kent Business College lecture from the teaching calendar through
          transcription, attendance, quality assurance and the moments learners remember.
        </p>
        <ul class="mt-8 space-y-3">
          <li v-for="item in HIGHLIGHTS" :key="item.text" class="flex items-center gap-3 text-sm text-white/75">
            <span class="grid h-8 w-8 place-items-center rounded-lg bg-white/10"><AppIcon :name="item.icon" :size="16" /></span>
            {{ item.text }}
          </li>
        </ul>
      </div>

      <p class="relative text-2xs text-white/40">Kent Business College · Authorised access only</p>
    </section>

    <!-- form -->
    <section class="flex items-center justify-center bg-white px-5 py-12 sm:px-12">
      <div class="w-full max-w-sm">
        <div class="mb-10 lg:hidden"><BrandIdentity size="md" /></div>
        <p class="kicker">Welcome back</p>
        <h2 class="mt-1.5 font-display text-3xl leading-tight text-ink">Sign in</h2>
        <p class="mt-2 text-sm leading-6 text-muted">
          Use your Kent Business College Microsoft account - the same one as the Communication Centre.
        </p>

        <button type="button" class="btn-primary mt-8 !h-11 w-full" :disabled="loading" @click="signInWithMicrosoft">
          {{ loading && !showLocal ? 'Redirecting to Microsoft…' : 'Sign in with Microsoft' }}
        </button>
        <p v-if="error && !showLocal" class="notice-error mt-4" role="alert">
          <AppIcon name="alert" :size="16" class="mt-0.5" />{{ error }}
        </p>

        <button
          v-if="!showLocal" type="button"
          class="mt-6 w-full text-center text-xs font-semibold text-muted underline-offset-4 hover:underline"
          @click="showLocal = true; error = ''"
        >
          Sign in with a local account instead
        </button>

        <form v-else class="mt-8 space-y-4 border-t border-line pt-6" @submit.prevent="submit">
          <div>
            <label for="username" class="label">Username</label>
            <input id="username" v-model="username" class="field !h-11" autocomplete="username" required />
          </div>
          <div>
            <label for="password" class="label">Password</label>
            <input id="password" v-model="password" type="password" class="field !h-11" autocomplete="current-password" required />
          </div>
          <p v-if="error" class="notice-error" role="alert">
            <AppIcon name="alert" :size="16" class="mt-0.5" />{{ error }}
          </p>
          <button type="submit" class="btn-secondary !h-11 w-full" :disabled="loading">
            {{ loading ? 'Signing in…' : 'Sign in' }}
          </button>
        </form>

        <p class="mt-8 text-2xs leading-5 text-faint">
          This is an internal Kent Business College system. Activity is recorded against your account.
        </p>
      </div>
    </section>
  </main>
</template>

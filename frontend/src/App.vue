<script setup lang="ts">
/**
 * One product, one shell.
 *
 * Operations, Lectures and Positive Moments are three jobs inside a single
 * Kent Business College application, so they share one header, one navigation
 * and one type scale. Anything that made them look like three tools bolted
 * together belongs in here, once, rather than in each view.
 *
 * The bar is the college's deep plum. That is a deliberate inversion: content
 * is where the operator works and should stay light and data-dense, so the
 * brand lives in the chrome and does not compete with the tables.
 */
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AppIcon from './components/AppIcon.vue'
import BrandIdentity from './components/BrandIdentity.vue'
import { clearToken, hasToken, logout } from './services/api'

const route = useRoute()
const router = useRouter()
const menuOpen = ref(false)
const showShell = computed(() => route.meta.public !== true)

const NAV = [
  { label: 'Operations', to: '/operations', icon: 'operations', match: /^\/operations/ },
  { label: 'Lectures', to: '/lectures', icon: 'lectures', match: /^\/lectures/ },
  { label: 'Positive Moments', to: '/positive-moments', icon: 'moments', match: /^\/positive-moments/ },
]

const active = computed(() => NAV.find((item) => item.match.test(route.path))?.to ?? '')

watch(() => route.path, () => { menuOpen.value = false })

async function signOut() {
  try { await logout() } finally {
    clearToken()
    await router.push({ name: 'login' })
  }
}
</script>

<template>
  <div v-if="showShell" class="min-h-screen bg-canvas">
    <header class="on-plum sticky top-0 z-40 bg-brand-900 text-white">
      <div class="mx-auto flex h-14 w-full max-w-[1520px] items-center gap-6 px-4 sm:px-6 lg:px-8">
        <RouterLink to="/operations" class="shrink-0" aria-label="Kent Business College Lecture Intelligence — Operations">
          <BrandIdentity size="sm" inverse />
        </RouterLink>

        <nav class="hidden h-full items-stretch gap-0.5 md:flex" aria-label="Main">
          <RouterLink
            v-for="item in NAV"
            :key="item.to"
            :to="item.to"
            class="relative flex items-center gap-2 px-3.5 text-[0.8125rem] font-semibold transition"
            :class="active === item.to
              ? 'text-white after:absolute after:inset-x-3 after:bottom-0 after:h-[2px] after:rounded-full after:bg-gold-400'
              : 'text-white/60 hover:text-white'"
            :aria-current="active === item.to ? 'page' : undefined"
          >
            <AppIcon :name="item.icon" :size="16" />
            {{ item.label }}
          </RouterLink>
        </nav>

        <div class="ml-auto flex items-center gap-2">
          <span class="hidden text-2xs font-medium uppercase tracking-kicker text-white/45 lg:inline">
            Internal workspace
          </span>
          <button
            v-if="hasToken()"
            type="button"
            class="hidden items-center gap-1.5 rounded-lg border border-white/15 px-3 py-1.5 text-[0.8125rem] font-semibold text-white/80 transition hover:border-white/35 hover:text-white sm:inline-flex"
            @click="signOut"
          >
            <AppIcon name="signOut" :size="15" /> Sign out
          </button>
          <button
            type="button"
            class="grid h-9 w-9 place-items-center rounded-lg text-white/75 transition hover:bg-white/10 hover:text-white md:hidden"
            :aria-expanded="menuOpen"
            aria-label="Toggle navigation"
            @click="menuOpen = !menuOpen"
          >
            <AppIcon :name="menuOpen ? 'close' : 'menu'" :size="18" />
          </button>
        </div>
      </div>

      <nav v-if="menuOpen" class="border-t border-white/10 px-3 py-2 md:hidden" aria-label="Mobile">
        <RouterLink
          v-for="item in NAV"
          :key="item.to"
          :to="item.to"
          class="flex items-center gap-2.5 rounded-lg px-3 py-2.5 text-sm font-semibold transition"
          :class="active === item.to ? 'bg-white/10 text-white' : 'text-white/65'"
        >
          <AppIcon :name="item.icon" :size="16" /> {{ item.label }}
        </RouterLink>
        <button
          type="button"
          class="flex w-full items-center gap-2.5 rounded-lg px-3 py-2.5 text-left text-sm font-semibold text-white/65"
          @click="signOut"
        >
          <AppIcon name="signOut" :size="16" /> Sign out
        </button>
      </nav>
    </header>

    <main><RouterView /></main>
  </div>
  <RouterView v-else />
</template>

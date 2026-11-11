<script setup lang="ts">
/**
 * One AI System Message: the saved text, editable, with its version history.
 *
 * The text shown is exactly what PostgreSQL holds and what n8n will read.
 * Saving names the version this editor was opened on (`expected_version`),
 * so if someone saved in the meantime the server refuses with 409 and the
 * edits stay in the box until the person chooses to load the newer text.
 * Restoring an earlier version saves it again as a new version; nothing is
 * ever overwritten.
 */
import { computed, onMounted, ref } from 'vue'

import AppIcon from './AppIcon.vue'
import StatusBadge from './StatusBadge.vue'
import { getPrompt, getPromptVersion, restorePrompt, savePrompt } from '../services/social'
import type { PromptDetail, PromptKey, PromptVersion } from '../types/social'
import { cairoDateTime } from '../utils/datetime'
import { apiError } from '../utils/social'

const props = defineProps<{ promptKey: PromptKey; title: string; description: string }>()
const emit = defineEmits<{ dirty: [value: boolean] }>()

const detail = ref<PromptDetail | null>(null)
const draft = ref('')
const loading = ref(true)
const saving = ref(false)
const loadError = ref('')
const feedback = ref<{ tone: 'ok' | 'error' | 'warn'; text: string } | null>(null)
const conflict = ref(false)
const preview = ref<(PromptVersion & { content: string }) | null>(null)
const confirmRestore = ref<number | null>(null)

/** Browsers hand textarea text back with LF line breaks; compare like with like. */
const normalise = (text: string) => text.replace(/\r\n?/g, '\n')
const saved = computed(() => normalise(detail.value?.active?.content ?? ''))
const activeVersion = computed(() => detail.value?.active?.version ?? 0)
const dirty = computed(() => !loading.value && normalise(draft.value) !== saved.value)
const canSave = computed(() => dirty.value && draft.value.trim().length > 0 && !saving.value)
const earlier = computed(() => (detail.value?.versions ?? []).filter((item) => item.version !== activeVersion.value))
const id = computed(() => `prompt-${props.promptKey}`)

function setDirty() {
  emit('dirty', dirty.value)
}

async function load() {
  loading.value = true
  loadError.value = ''
  conflict.value = false
  try {
    detail.value = await getPrompt(props.promptKey)
    draft.value = detail.value.active?.content ?? ''
  } catch (caught) {
    loadError.value = apiError(caught, 'This prompt could not be loaded.').detail
  } finally {
    loading.value = false
    setDirty()
  }
}
onMounted(load)

function describeVersion(item: PromptVersion) {
  const restored = item.restored_from_version ? ` · restored from v${item.restored_from_version}` : ''
  return `${item.created_by} · ${cairoDateTime(item.created_at)}${restored}`
}

async function save() {
  if (!canSave.value) return
  saving.value = true
  feedback.value = null
  try {
    const active = await savePrompt(props.promptKey, draft.value, activeVersion.value)
    await load()
    feedback.value = { tone: 'ok', text: `Saved as version ${active.version}. n8n uses it from its next analysis.` }
  } catch (caught) {
    const error = apiError(caught, 'The prompt could not be saved. Try again.')
    if (error.code === 'version_conflict') {
      conflict.value = true
      feedback.value = {
        tone: 'warn',
        text: `Someone saved version ${String(error.data.active_version ?? 'a newer')} after you opened this prompt. `
          + 'Your edits are still in the box: copy anything you need, then load the latest version.',
      }
    } else {
      feedback.value = { tone: 'error', text: error.detail }
    }
  } finally {
    saving.value = false
    setDirty()
  }
}

function discard() {
  draft.value = detail.value?.active?.content ?? ''
  feedback.value = null
  setDirty()
}

async function view(version: number) {
  feedback.value = null
  try {
    preview.value = await getPromptVersion(props.promptKey, version)
  } catch (caught) {
    feedback.value = { tone: 'error', text: apiError(caught, 'That version could not be loaded.').detail }
  }
}

async function restore(version: number) {
  confirmRestore.value = null
  saving.value = true
  feedback.value = null
  try {
    const active = await restorePrompt(props.promptKey, version, activeVersion.value)
    preview.value = null
    await load()
    feedback.value = { tone: 'ok', text: `Version ${version} restored as version ${active.version}.` }
  } catch (caught) {
    const error = apiError(caught, 'That version could not be restored. Try again.')
    if (error.code === 'version_conflict') conflict.value = true
    feedback.value = { tone: error.code === 'version_conflict' ? 'warn' : 'error', text: error.detail }
  } finally {
    saving.value = false
    setDirty()
  }
}

</script>

<template>
  <section class="surface flex min-w-0 flex-col" :data-test="`prompt-${promptKey}`" :aria-labelledby="`${id}-title`">
    <div class="panel-head">
      <div class="min-w-0">
        <h3 :id="`${id}-title`" class="text-base font-bold text-ink">{{ title }}</h3>
        <p class="mt-0.5 text-xs text-muted">{{ description }}</p>
      </div>
      <StatusBadge
        v-if="!loading && !loadError"
        :label="activeVersion ? `Active: version ${activeVersion}` : 'Not saved yet'"
        :tone="activeVersion ? 'brand' : 'waiting'" data-test="active-version"
      />
    </div>

    <div class="panel-body flex flex-1 flex-col gap-3">
      <p v-if="loading" class="text-sm text-muted">Loading the saved prompt…</p>

      <div v-else-if="loadError" class="notice-error" role="alert">
        <AppIcon name="alert" :size="16" class="mt-0.5" />
        <span class="flex-1">{{ loadError }}</span>
        <button type="button" class="btn-quiet" @click="load">Retry</button>
      </div>

      <template v-else>
        <p v-if="detail?.active" class="text-xs text-muted" data-test="active-meta">
          Version {{ detail.active.version }} saved by {{ describeVersion(detail.active) }}
        </p>
        <p v-else class="text-xs text-muted">No version has been saved yet. The text you save becomes version 1.</p>

        <label class="sr-only" :for="`${id}-text`">{{ title }} System Message</label>
        <textarea
          :id="`${id}-text`" v-model="draft" spellcheck="false" wrap="soft"
          class="min-h-[22rem] w-full flex-1 resize-y rounded-lg border border-line bg-white p-3 font-mono text-[0.8125rem] leading-6 text-ink transition hover:border-brand-300 focus:border-brand-500"
          :readonly="saving" data-test="prompt-text" @input="setDirty"
        />
        <div class="flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
          <span data-test="prompt-length">{{ draft.length.toLocaleString() }} characters<span v-if="dirty" class="ml-2 font-semibold text-amber-700">· Unsaved changes</span></span>
          <div class="flex flex-wrap gap-2">
            <button v-if="dirty" type="button" class="btn-secondary btn-sm" :disabled="saving" data-test="discard" @click="discard">
              Discard changes
            </button>
            <button type="button" class="btn-primary btn-sm" :disabled="!canSave" data-test="save" @click="save">
              <AppIcon name="check" :size="14" />{{ saving ? 'Saving…' : 'Save new version' }}
            </button>
          </div>
        </div>

        <div
          v-if="feedback" role="status" data-test="feedback"
          :class="feedback.tone === 'ok' ? 'notice' : feedback.tone === 'warn' ? 'notice-warn' : 'notice-error'"
        >
          <AppIcon :name="feedback.tone === 'ok' ? 'checkCircle' : 'alert'" :size="16" class="mt-0.5" />
          <span class="flex-1">{{ feedback.text }}</span>
          <button v-if="conflict" type="button" class="btn-quiet" data-test="load-latest" @click="load(); feedback = null">
            Load latest version
          </button>
        </div>

        <details v-if="earlier.length" class="rounded-lg border border-line" data-test="history">
          <summary class="cursor-pointer px-3 py-2 text-sm font-semibold text-ink">
            Earlier versions <span class="text-muted">({{ earlier.length }})</span>
          </summary>
          <ul class="divide-y divide-line border-t border-line">
            <li v-for="item in earlier" :key="item.version" class="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
              <span class="min-w-0 text-xs text-body">
                <span class="font-semibold text-ink">Version {{ item.version }}</span> · {{ describeVersion(item) }}
              </span>
              <span class="flex gap-3">
                <button type="button" class="btn-quiet" :data-test="`view-${item.version}`" @click="view(item.version)">View</button>
                <button
                  v-if="confirmRestore !== item.version" type="button" class="btn-quiet" :disabled="saving"
                  :data-test="`restore-${item.version}`" @click="confirmRestore = item.version"
                >Restore</button>
                <span v-else class="flex items-center gap-2 text-xs">
                  Restore as the active prompt?
                  <button type="button" class="btn-quiet" :data-test="`confirm-restore-${item.version}`" @click="restore(item.version)">Yes</button>
                  <button type="button" class="btn-quiet" @click="confirmRestore = null">No</button>
                </span>
              </span>
            </li>
          </ul>
        </details>

        <div v-if="preview" class="rounded-lg border border-brand-200 bg-brand-50/50" data-test="preview">
          <div class="flex items-center justify-between gap-2 px-3 py-2">
            <span class="text-xs font-semibold text-ink">Version {{ preview.version }} (read only)</span>
            <button type="button" class="icon-btn !h-7 !w-7" aria-label="Close preview" @click="preview = null">
              <AppIcon name="close" :size="14" />
            </button>
          </div>
          <pre class="max-h-72 overflow-auto whitespace-pre-wrap break-words border-t border-brand-200 p-3 font-mono text-xs leading-5 text-body">{{ preview.content }}</pre>
        </div>
      </template>
    </div>
  </section>
</template>

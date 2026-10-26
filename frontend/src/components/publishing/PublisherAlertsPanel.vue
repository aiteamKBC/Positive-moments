<script setup lang="ts">
/**
 * The publisher's OPEN alerts, exactly as the reconciler stored them (bunny_publisher_alerts).
 *
 * Read-only: no acknowledge / resolve buttons - the reconciler resolves an alert itself when its
 * condition is no longer observed. Alerts are grouped by their STORED severity only: everything
 * stored as DIGEST is the informational daily digest (its counters folded away until asked for);
 * every other severity - Critical, Warning or one this page does not know - is listed in full.
 * A lecture or job is shown only when the alert names one; every other stored detail is listed
 * as it is. Nothing is inferred.
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'

import AppIcon from '../AppIcon.vue'
import EmptyState from '../EmptyState.vue'
import SectionPanel from '../SectionPanel.vue'
import StatusBadge from '../StatusBadge.vue'
import { getPublisherAlerts } from '../../services/publishing'
import type { PublisherAlert, PublisherAlerts } from '../../types/publishing'
import { businessDate, cairoDateTime, sinceNow } from '../../utils/datetime'
import { errorMessage } from '../../utils/format'
import { humanise } from '../../utils/labels'
import { alertCountText, DIGEST, label } from '../../utils/publishing'

const props = defineProps<{ refreshKey: string }>()
const emit = defineEmits<{ close: []; openLecture: [lectureId: string] }>()

const data = ref<PublisherAlerts | null>(null)
const error = ref('')
/** Digest rows the reader opened; kept by alert id so a refresh does not fold them again. */
const expanded = reactive(new Set<string>())

async function load() {
  try {
    data.value = await getPublisherAlerts()
    error.value = ''
  } catch (caught) {
    error.value = errorMessage(caught, 'The publisher alerts could not be read.')
  }
}

onMounted(load)
watch(() => props.refreshKey, load)

const alerts = computed(() => data.value?.alerts.filter(alert => alert.severity !== DIGEST) ?? [])
const digests = computed(() => data.value?.alerts.filter(alert => alert.severity === DIGEST) ?? [])
const countText = computed(() => (data.value ? alertCountText(data.value.counts) : null))

function toggle(alert: PublisherAlert) {
  if (expanded.has(alert.alert_id)) expanded.delete(alert.alert_id)
  else expanded.add(alert.alert_id)
}

function detailValue(value: unknown) {
  if (value === null || value === undefined || value === '') return '—'
  if (Array.isArray(value)) return value.join(', ')
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}
</script>

<template>
  <SectionPanel
    title="Open publisher alerts"
    :count="countText"
    note="As recorded by the publisher's reconciliation. An alert closes by itself when its condition is no longer observed."
    flush
  >
    <template #actions>
      <button type="button" class="btn-quiet btn-sm" @click="emit('close')">
        <AppIcon name="close" :size="14" /> Hide
      </button>
    </template>
    <p v-if="error" class="notice-error m-4">{{ error }}</p>
    <div v-else-if="!data" class="p-4"><div class="skeleton h-16 w-full" /></div>
    <EmptyState v-else-if="!data.alerts.length" compact title="No open alerts" message="The publisher has nothing open right now." />
    <template v-else>
      <!-- Warnings, critical and any other stored severity: in full -->
      <p v-if="!alerts.length" class="px-4 py-3 text-sm text-muted" data-test="no-alerts">No warning or critical alerts are open.</p>
      <div v-else class="overflow-x-auto">
        <table class="data-table" data-test="alerts-table">
          <thead>
            <tr>
              <th scope="col">Severity</th>
              <th scope="col">Alert</th>
              <th scope="col">Lecture / job</th>
              <th scope="col">Details</th>
              <th scope="col" class="text-right">Seen</th>
              <th scope="col">First seen</th>
              <th scope="col">Last seen</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="alert in alerts" :key="alert.alert_id" data-test="alert-row">
              <td><StatusBadge v-bind="label.alert(alert.severity)" /></td>
              <td class="min-w-[14rem]">
                <p class="font-semibold text-ink">{{ humanise(alert.kind) }}</p>
                <p class="text-xs text-muted">{{ alert.summary }}</p>
                <p v-if="alert.notification_status" class="text-2xs text-faint">Notification {{ humanise(alert.notification_status).toLowerCase() }}</p>
              </td>
              <td class="min-w-[10rem]">
                <button
                  v-if="alert.lecture" type="button" class="row-link text-left text-sm font-semibold"
                  @click="emit('openLecture', alert.lecture.lecture_id)"
                >
                  {{ alert.lecture.subject ?? 'Lecture' }}
                  <span v-if="alert.lecture.session_date" class="block text-2xs font-normal text-muted">{{ businessDate(alert.lecture.session_date) }}</span>
                </button>
                <p v-if="alert.job_id" class="font-mono text-2xs text-muted">job {{ alert.job_id }}</p>
                <span v-if="!alert.lecture && !alert.job_id" class="text-xs text-faint">Not linked to a lecture</span>
              </td>
              <td class="min-w-[12rem]">
                <dl v-if="Object.keys(alert.detail).length" class="space-y-0.5 text-2xs">
                  <div v-for="(value, key) in alert.detail" :key="key" class="flex gap-1.5">
                    <dt class="shrink-0 text-faint">{{ humanise(String(key)) }}</dt>
                    <dd class="break-all font-mono text-muted">{{ detailValue(value) }}</dd>
                  </div>
                </dl>
                <span v-else class="text-xs text-faint">—</span>
              </td>
              <td class="text-right tabular-nums">{{ alert.occurrences }}×</td>
              <td class="whitespace-nowrap text-xs text-muted">{{ cairoDateTime(alert.first_seen_at) }}</td>
              <td class="whitespace-nowrap text-xs text-muted" :title="cairoDateTime(alert.last_seen_at)">{{ sinceNow(alert.last_seen_at, '—') }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- The daily digest: informational, one line each, counters on request -->
      <section v-if="digests.length" class="border-t border-line" aria-labelledby="publisher-digest-title" data-test="digest-section">
        <h3 id="publisher-digest-title" class="px-4 pt-3 text-xs font-semibold uppercase tracking-wide text-muted">
          Daily digest <span class="font-normal normal-case tracking-normal text-faint">· informational, stored with severity Digest</span>
        </h3>
        <ul class="divide-y divide-line">
          <li v-for="digest in digests" :key="digest.alert_id" class="px-4 py-2.5" data-test="digest-row">
            <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
              <StatusBadge v-bind="label.alert(digest.severity)" />
              <p class="min-w-0 flex-1 text-sm">
                <span class="font-semibold text-ink">{{ humanise(digest.kind) }}</span>
                <span class="ml-1.5 text-xs text-muted">{{ digest.summary }}</span>
              </p>
              <span class="whitespace-nowrap text-xs text-muted" :title="cairoDateTime(digest.last_seen_at)">{{ sinceNow(digest.last_seen_at, '—') }}</span>
              <button
                type="button" class="btn-quiet btn-sm"
                :aria-expanded="expanded.has(digest.alert_id)" :aria-controls="`digest-${digest.alert_id}`"
                data-test="digest-toggle" @click="toggle(digest)"
              >
                {{ expanded.has(digest.alert_id) ? 'Hide details' : 'View details' }}
              </button>
            </div>
            <div v-show="expanded.has(digest.alert_id)" :id="`digest-${digest.alert_id}`" class="mt-2 rounded-lg bg-canvas p-3" data-test="digest-details">
              <dl class="grid grid-cols-2 gap-x-4 gap-y-1 text-2xs sm:grid-cols-3 lg:grid-cols-4">
                <div v-for="(value, key) in digest.detail" :key="key" class="flex justify-between gap-2">
                  <dt class="text-faint">{{ humanise(String(key)) }}</dt>
                  <dd class="font-mono tabular-nums text-ink">{{ detailValue(value) }}</dd>
                </div>
              </dl>
              <p class="mt-2 text-2xs text-muted">
                Seen {{ digest.occurrences }}× · first {{ cairoDateTime(digest.first_seen_at) }} · last {{ cairoDateTime(digest.last_seen_at) }}
                <template v-if="digest.notification_status"> · notification {{ humanise(digest.notification_status).toLowerCase() }}</template>
                <template v-if="digest.lecture">
                  ·
                  <button type="button" class="row-link font-semibold" @click="emit('openLecture', digest.lecture.lecture_id)">
                    {{ digest.lecture.subject ?? 'Lecture' }}<template v-if="digest.lecture.session_date">, {{ businessDate(digest.lecture.session_date) }}</template>
                  </button>
                </template>
                <span v-if="digest.job_id" class="font-mono"> · job {{ digest.job_id }}</span>
              </p>
            </div>
          </li>
        </ul>
      </section>
    </template>
  </SectionPanel>
</template>

<script setup lang="ts">
/**
 * The one semantic status badge.
 *
 * Callers pass a backend token and, optionally, the tone it deserves. Nothing
 * here guesses a tone by pattern-matching on substrings - that is how "not
 * eligible" once came out green because it contained "eligible".
 */
import { computed } from 'vue'
import { TONE_CLASS, humanise, type Tone } from '../utils/labels'

const props = withDefaults(defineProps<{
  label?: string | null
  value?: string | null
  tone?: Tone
  title?: string | null
}>(), { label: undefined, value: undefined, title: undefined, tone: 'neutral' })

const text = computed(() => props.label ?? humanise(props.value))
</script>

<template>
  <span :class="TONE_CLASS[tone]" :title="title ?? value ?? undefined">
    <slot name="icon" />
    {{ text }}
  </span>
</template>

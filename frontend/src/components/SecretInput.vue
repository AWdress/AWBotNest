<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  modelValue: { type: String, default: '' },
  placeholder: { type: String, default: '' },
  autocomplete: { type: String, default: 'off' },
  maskedValue: { type: String, default: '********' },
  disabled: { type: Boolean, default: false },
  readonly: { type: Boolean, default: false },
  mono: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue', 'reveal'])
const visible = ref(false)
const isMasked = computed(() => props.modelValue === props.maskedValue)

function toggleVisibility() {
  visible.value = !visible.value
  if (visible.value && isMasked.value) emit('reveal')
}
</script>

<template>
  <div class="secret-input">
    <input
      class="input"
      :class="{ mono }"
      :type="visible ? 'text' : 'password'"
      :value="modelValue"
      :placeholder="placeholder"
      :autocomplete="autocomplete"
      :disabled="disabled"
      :readonly="readonly"
      @input="emit('update:modelValue', $event.target.value)"
    />
    <button
      type="button"
      class="secret-toggle"
      :disabled="disabled || !modelValue"
      :aria-label="visible ? '隐藏内容' : '显示内容'"
      :title="visible ? '隐藏内容' : '显示内容'"
      @click="toggleVisibility"
    >
      <svg v-if="visible" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M3 3l18 18"/><path d="M10.6 10.6a2 2 0 0 0 2.8 2.8"/><path d="M9.9 4.2A10.8 10.8 0 0 1 12 4c5.5 0 9 5 9 8a12.7 12.7 0 0 1-2.1 3.5M6.6 6.6C4.3 8 3 10.2 3 12c0 3 3.5 8 9 8 1.2 0 2.3-.2 3.3-.6"/>
      </svg>
      <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>
      </svg>
    </button>
  </div>
</template>

<style scoped>
.secret-input { position: relative; flex: 1; width: 100%; min-width: 0; }
.secret-input .input { width: 100%; padding-right: 44px; }
.secret-toggle {
  position: absolute; right: 6px; top: 50%; transform: translateY(-50%);
  width: 34px; height: 34px; display: grid; place-items: center;
  border: 0; border-radius: 8px; background: transparent;
  color: var(--text-muted); cursor: pointer;
}
.secret-toggle:hover:not(:disabled) { color: var(--accent); background: var(--accent-dim); }
.secret-toggle:disabled { opacity: .4; cursor: default; }
.secret-toggle svg { width: 18px; height: 18px; }
</style>

<template>
  <label class="ts" :class="{ 'ts--focus': focused, 'ts--error': error, 'ts--ok': ok }">
    <span class="ts-tag">{{ label }}</span>
    <select
      :value="modelValue"
      @change="$emit('update:modelValue', ($event.target as HTMLSelectElement).value)"
      @focus="focused = true"
      @blur="focused = false; $emit('blur')"
    >
      <option v-for="opt in options" :key="String(opt.value)" :value="opt.value" :disabled="opt.disabled">
        {{ opt.label }}
      </option>
    </select>
    <span class="ts-caret">⌄</span>
    <span v-if="selectedDesc" class="ts-desc">{{ selectedDesc }}</span>
    <span v-if="ok" class="ts-status ts-status--ok">✓</span>
    <span v-else-if="error" class="ts-status ts-status--err">✗</span>
  </label>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";

const props = defineProps<{
  modelValue: string | number;
  label: string;
  options: { value: string | number; label: string; desc?: string; disabled?: boolean }[];
  error?: boolean;
  ok?: boolean;
}>();
defineEmits<{ "update:modelValue": [v: string]; blur: [] }>();

const focused = ref(false);
const selectedDesc = computed(() => props.options.find((opt) => String(opt.value) === String(props.modelValue))?.desc ?? "");
</script>

<style scoped>
.ts {
  display: grid;
  grid-template-columns: minmax(140px, max-content) minmax(0, 1fr) auto auto;
  align-items: stretch;
  border: 1px solid var(--border);
  background: var(--bg-base);
  position: relative;
  transition: border-color 80ms ease-out;
}
.ts:hover { border-color: var(--border-strong); }
.ts--focus { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
.ts--error { border-color: var(--err); }
.ts--ok { border-color: var(--ok); }
.ts-tag {
  background: var(--bg-panel);
  color: var(--fg-tertiary);
  padding: 10px 12px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.04em;
  border-right: 1px solid var(--border);
  display: flex;
  align-items: center;
  white-space: nowrap;
}
.ts--focus .ts-tag { color: var(--accent); }
.ts select {
  appearance: none;
  -webkit-appearance: none;
  background: transparent;
  border: 0;
  padding: 10px 34px 10px 12px;
  color: var(--fg-primary);
  font: inherit;
  font-size: 13px;
  outline: none;
  min-width: 0;
  cursor: pointer;
  grid-column: 2 / 4;
  grid-row: 1;
}
.ts select option {
  background: var(--bg-base);
  color: var(--fg-primary);
}
.ts-caret {
  grid-column: 3;
  grid-row: 1;
  pointer-events: none;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 12px;
  color: var(--accent);
  font-size: 16px;
  line-height: 1;
}
.ts-desc {
  grid-column: 2 / -1;
  border-top: 1px solid var(--border);
  padding: 7px 12px;
  color: var(--fg-tertiary);
  background: var(--bg-panel);
  font-size: 11px;
}
.ts-status {
  grid-column: 4;
  grid-row: 1;
  display: flex;
  align-items: center;
  padding: 0 12px;
  font-weight: 700;
  font-size: 14px;
}
.ts-status--ok { color: var(--ok); }
.ts-status--err { color: var(--err); }
</style>

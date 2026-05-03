<template>
  <label class="tt" :class="{ 'tt--disabled': disabled }">
    <input type="checkbox" :checked="modelValue" :disabled="disabled" @change="onChange" />
    <span class="tt-box">{{ modelValue ? '[✓]' : '[ ]' }}</span>
    <span class="tt-label"><slot /></span>
  </label>
</template>

<script setup lang="ts">
const props = defineProps<{ modelValue: boolean; disabled?: boolean }>();
const emit = defineEmits<{ "update:modelValue": [v: boolean] }>();
function onChange(e: Event) {
  if (props.disabled) return;
  emit("update:modelValue", (e.target as HTMLInputElement).checked);
}
</script>

<style scoped>
.tt { display: inline-flex; align-items: center; gap: 8px; cursor: pointer; user-select: none; transition: color 60ms; }
.tt input { display: none; }
.tt-box { color: var(--accent); font-weight: 700; }
.tt:hover .tt-box { text-shadow: 0 0 6px var(--accent); }
.tt-label { font-size: 13px; color: var(--fg-secondary); }
.tt:hover .tt-label { color: var(--fg-primary); }
.tt--disabled { cursor: not-allowed; opacity: 0.45; }
.tt--disabled:hover .tt-box { text-shadow: none; }
.tt--disabled:hover .tt-label { color: var(--fg-secondary); }
</style>

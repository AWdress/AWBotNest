<script setup>
defineProps({
  compact: { type: Boolean, default: false },
})
</script>

<template>
  <span class="premium-badge" :class="{ compact }" title="Telegram Premium 会员">
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="m12 2.8 2.74 5.55 6.13.89-4.44 4.32 1.05 6.11L12 16.79l-5.48 2.88 1.05-6.11-4.44-4.32 6.13-.89L12 2.8Z" />
    </svg>
    <span>会员</span>
  </span>
</template>

<style scoped>
.premium-badge {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 3px;
  padding: 2px 7px;
  border: 1px solid rgba(245, 184, 65, .24);
  border-radius: 999px;
  background: rgba(245, 184, 65, .1);
  color: #f5c45d;
  font-size: 10px;
  font-weight: 650;
  line-height: 1.2;
  white-space: nowrap;
}
.premium-badge svg { width: 9px; height: 9px; }
.premium-badge.compact { padding: 1px 5px; font-size: 8px; }
.premium-badge.compact svg { width: 7px; height: 7px; }
</style>

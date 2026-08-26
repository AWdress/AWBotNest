<script setup>
import { computed, ref, watch } from 'vue'
import { getAccountAvatarUrl, loadAccountAvatar } from '../composables/accountAvatars'

const props = defineProps({
  account: { type: Object, required: true },
})

const imageUrl = ref('')
const failed = ref(false)
let requestId = 0

const initials = computed(() => {
  const value = String(props.account?.name || props.account?.session || '账号').trim()
  return value.slice(0, 2)
})

async function loadAvatar() {
  const currentRequest = ++requestId
  imageUrl.value = getAccountAvatarUrl(props.account)
  failed.value = false
  if (imageUrl.value || !props.account?.avatar_id || !props.account?.session) return
  try {
    const url = await loadAccountAvatar(props.account)
    if (currentRequest !== requestId) return
    imageUrl.value = url
    failed.value = !url
  } catch {
    if (currentRequest === requestId) failed.value = true
  }
}

watch(
  () => `${props.account?.session || ''}|${props.account?.avatar_id || ''}`,
  loadAvatar,
  { immediate: true },
)
</script>

<template>
  <span class="account-avatar" :class="{ fallback: !imageUrl || failed, offline: !account.online }" aria-hidden="true">
    <img v-if="imageUrl && !failed" :src="imageUrl" alt="" @error="failed = true">
    <span v-else>{{ initials }}</span>
  </span>
</template>

<style scoped>
.account-avatar { display: grid; place-items: center; overflow: hidden; flex: 0 0 auto; background: var(--accent-dim); color: #dceaff; }
.account-avatar img { width: 100%; height: 100%; object-fit: cover; }
.account-avatar span { line-height: 1; }
.account-avatar.offline { filter: grayscale(1); opacity: .58; }
</style>

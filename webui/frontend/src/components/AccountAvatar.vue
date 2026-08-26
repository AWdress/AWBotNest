<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api'

const props = defineProps({
  account: { type: Object, required: true },
})

const imageUrl = ref('')
const failed = ref(false)
let objectUrl = ''
let requestId = 0

const initials = computed(() => {
  const value = String(props.account?.name || props.account?.session || '账号').trim()
  return value.slice(0, 2)
})

function releaseImage() {
  if (objectUrl) URL.revokeObjectURL(objectUrl)
  objectUrl = ''
  imageUrl.value = ''
}

async function loadAvatar() {
  const currentRequest = ++requestId
  releaseImage()
  failed.value = false
  if (!props.account?.avatar_id || !props.account?.session) return
  try {
    const blob = await api.accountAvatar(props.account.session, props.account.avatar_id)
    if (currentRequest !== requestId) return
    objectUrl = URL.createObjectURL(blob)
    imageUrl.value = objectUrl
  } catch {
    if (currentRequest === requestId) failed.value = true
  }
}

watch(
  () => `${props.account?.session || ''}|${props.account?.avatar_id || ''}`,
  loadAvatar,
  { immediate: true },
)
onBeforeUnmount(() => { requestId += 1; releaseImage() })
</script>

<template>
  <span class="account-avatar" :class="{ fallback: !imageUrl || failed }" aria-hidden="true">
    <img v-if="imageUrl && !failed" :src="imageUrl" alt="" @error="failed = true">
    <span v-else>{{ initials }}</span>
  </span>
</template>

<style scoped>
.account-avatar { display: grid; place-items: center; overflow: hidden; flex: 0 0 auto; background: var(--accent-dim); color: #dceaff; }
.account-avatar img { width: 100%; height: 100%; object-fit: cover; }
.account-avatar span { line-height: 1; }
</style>

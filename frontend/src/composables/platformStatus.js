import { ref, shallowRef } from 'vue'
import { api } from '../api'

export const platformStatus = shallowRef(null)
export const platformStatusError = ref('')
export const platformStatusLoading = ref(false)

let pollingTimer = null
let statusRequest = null
let visibilityBound = false
let generation = 0

export async function refreshPlatformStatus(force = false) {
  if (document.hidden && !force) return platformStatus.value
  if (statusRequest) return statusRequest

  platformStatusLoading.value = !platformStatus.value
  const startedGeneration = generation
  statusRequest = api.status(force)
    .then((data) => {
      if (startedGeneration === generation) {
        platformStatus.value = data
        platformStatusError.value = ''
      }
      return data
    })
    .catch((error) => {
      if (startedGeneration === generation) platformStatusError.value = error.message || '读取平台状态失败'
      throw error
    })
    .finally(() => {
      if (startedGeneration === generation) {
        statusRequest = null
        platformStatusLoading.value = false
      }
    })

  return statusRequest
}

function handleVisibilityChange() {
  if (!document.hidden && pollingTimer) refreshPlatformStatus(true).catch(() => {})
}

export function startPlatformStatusPolling(interval = 10000) {
  if (!visibilityBound) {
    document.addEventListener('visibilitychange', handleVisibilityChange)
    visibilityBound = true
  }
  if (!pollingTimer) {
    pollingTimer = window.setInterval(() => {
      refreshPlatformStatus().catch(() => {})
    }, interval)
  }
  return refreshPlatformStatus()
}

export function stopPlatformStatusPolling() {
  generation += 1
  statusRequest = null
  platformStatus.value = null
  platformStatusError.value = ''
  platformStatusLoading.value = false
  api.clearCache()
  if (pollingTimer) window.clearInterval(pollingTimer)
  pollingTimer = null
  if (visibilityBound) document.removeEventListener('visibilitychange', handleVisibilityChange)
  visibilityBound = false
}

import { readonly, ref } from 'vue'
import { api } from '../api'

const profileState = ref(null)
let profileRequest = null
let profileGeneration = 0

function normalizeProfile(value = {}) {
  return {
    username: String(value.username || '').trim(),
    avatar_url: String(value.avatar_url || '').trim(),
  }
}

function preloadAvatar(url) {
  if (!url) return Promise.resolve(true)
  return new Promise((resolve) => {
    const image = new Image()
    const timer = setTimeout(() => finish(false), 6000)
    function finish(ok) {
      clearTimeout(timer)
      image.onload = image.onerror = null
      resolve(ok)
    }
    image.onload = () => finish(true)
    image.onerror = () => finish(false)
    image.src = url
  })
}

async function prepareProfile(value) {
  const next = normalizeProfile(value)
  if (next.avatar_url && next.avatar_url !== profileState.value?.avatar_url) {
    const loaded = await preloadAvatar(next.avatar_url)
    if (!loaded) next.avatar_url = ''
  }
  return next
}

export async function applyUiProfile(value) {
  const next = await prepareProfile(value)
  profileState.value = next
  return next
}

export async function loadUiProfile(force = false) {
  if (!force && profileState.value) return profileState.value
  if (profileRequest) return profileRequest
  const generation = profileGeneration
  profileRequest = api.getUiProfile()
    .then(prepareProfile)
    .then((next) => {
      if (generation === profileGeneration) profileState.value = next
      return next
    })
    .finally(() => { if (generation === profileGeneration) profileRequest = null })
  return profileRequest
}

export function clearUiProfile() {
  profileGeneration += 1
  profileState.value = null
  profileRequest = null
}

export const uiProfile = readonly(profileState)

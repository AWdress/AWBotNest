export function formatLogTime(item = {}) {
  const date = item.timestamp ? new Date(item.timestamp) : null
  if (date && !Number.isNaN(date.getTime())) {
    const pad = value => String(value).padStart(2, '0')
    return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  }
  return item.date ? `${String(item.date).slice(5)} ${item.time || ''}`.trim() : (item.time || '')
}

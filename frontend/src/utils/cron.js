const CRON_HINT = /(^|[_-])(cron|cronexpr|cron_expression|cron_schedule|schedule_cron|schedule)([_-]|$)/i
const CRON_LABEL_HINT = /(cron|执行周期|执行时间|定时周期|定时规则|调度周期)/i
const NON_CRON_HINT = /(^|[_-])(enabled|enable|mode|type|interval|timezone|time_zone)([_-]|$)/i
const TOKEN = /^[0-9A-Za-z*?/,#LW-]+$/
const CRON_NAMES = /JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC|MON|TUE|WED|THU|FRI|SAT|SUN/gi

function validToken(part) {
  if (!TOKEN.test(part)) return false
  return /^[0-9*?/,#LW-]+$/i.test(part.replace(CRON_NAMES, ''))
}

export function cronParts(value) {
  const text = String(value ?? '').trim().replace(/\s+/g, ' ')
  if (!text) return []
  const parts = text.split(' ')
  if ((parts.length !== 5 && parts.length !== 6) || !parts.every(validToken)) return []
  const bounds = parts.length === 6
    ? [[0, 59], [0, 59], [0, 23], [1, 31], [1, 12], [0, 7]]
    : [[0, 59], [0, 23], [1, 31], [1, 12], [0, 7]]
  const validRanges = parts.every((part, index) => {
    const numbers = part.match(/\d+/g) || []
    const [minimum, maximum] = bounds[index]
    return numbers.every((item) => Number(item) >= minimum && Number(item) <= maximum)
  })
  return validRanges ? parts : []
}

export function isValidCron(value) {
  return cronParts(value).length > 0
}

export function isCronField(spec = {}, name = '') {
  if (spec.type === 'cron' || spec.format === 'cron') return true
  if (!['', 'string', 'text'].includes(String(spec.type || ''))) return false
  const key = String(name || '')
  const label = String(spec.label || spec.title || '')
  if (NON_CRON_HINT.test(key)) return false
  return CRON_HINT.test(key) || CRON_LABEL_HINT.test(label)
}

export function simpleCron(value) {
  const parts = cronParts(value)
  if (!parts.length) return null
  const six = parts.length === 6
  const [second, minute, hour, day, month, weekday] = six
    ? parts
    : ['0', ...parts]
  const number = (raw, min, max) => /^\d+$/.test(raw) && Number(raw) >= min && Number(raw) <= max
  if (month !== '*') return null
  const base = { six, second, minute, hour, day, weekday }
  if (day === '*' && weekday === '*' && number(hour, 0, 23)
      && number(minute, 0, 59) && number(second, 0, 59)) return { ...base, mode: 'daily' }
  if (day === '*' && number(weekday, 0, 7) && number(hour, 0, 23)
      && number(minute, 0, 59) && number(second, 0, 59)) return { ...base, mode: 'weekly' }
  if (weekday === '*' && number(day, 1, 31) && number(hour, 0, 23)
      && number(minute, 0, 59) && number(second, 0, 59)) return { ...base, mode: 'monthly' }
  if (day === '*' && weekday === '*' && hour === '*' && number(minute, 0, 59)
      && number(second, 0, 59)) return { ...base, mode: 'hourly' }
  return null
}

const WEEKDAYS = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六']
const pad = (value) => String(value).padStart(2, '0')

export function describeCron(value) {
  const simple = simpleCron(value)
  if (!simple) return isValidCron(value) ? '复杂 Cron 规则' : 'Cron 表达式格式不正确'
  const time = `${pad(simple.hour === '*' ? 0 : simple.hour)}:${pad(simple.minute)}`
    + (simple.six ? `:${pad(simple.second)}` : '')
  if (simple.mode === 'daily') return `每天 ${time} 执行`
  if (simple.mode === 'weekly') return `每周${WEEKDAYS[Number(simple.weekday) % 7].slice(2)} ${time} 执行`
  if (simple.mode === 'monthly') return `每月 ${Number(simple.day)} 日 ${time} 执行`
  return `每小时第 ${Number(simple.minute)} 分${simple.six ? ` ${Number(simple.second)} 秒` : ''}执行`
}

function shanghaiStamp(year, month, day, hour, minute, second) {
  return Date.UTC(year, month, day, hour - 8, minute, second)
}

export function nextCronRuns(value, count = 3, now = new Date()) {
  const simple = simpleCron(value)
  if (!simple) return []
  const shifted = new Date(now.getTime() + 8 * 60 * 60 * 1000)
  const year = shifted.getUTCFullYear()
  const month = shifted.getUTCMonth()
  const day = shifted.getUTCDate()
  const hour = shifted.getUTCHours()
  const minute = Number(simple.minute)
  const second = Number(simple.second)
  const result = []
  if (simple.mode === 'hourly') {
    let stamp = shanghaiStamp(year, month, day, hour, minute, second)
    if (stamp <= now.getTime()) stamp += 60 * 60 * 1000
    for (let i = 0; i < count; i++) result.push(new Date(stamp + i * 60 * 60 * 1000))
    return result
  }
  if (simple.mode === 'daily') {
    let stamp = shanghaiStamp(year, month, day, Number(simple.hour), minute, second)
    if (stamp <= now.getTime()) stamp += 24 * 60 * 60 * 1000
    for (let i = 0; i < count; i++) result.push(new Date(stamp + i * 24 * 60 * 60 * 1000))
    return result
  }
  if (simple.mode === 'weekly') {
    const wanted = Number(simple.weekday) % 7
    let delta = (wanted - shifted.getUTCDay() + 7) % 7
    let stamp = shanghaiStamp(year, month, day + delta, Number(simple.hour), minute, second)
    if (stamp <= now.getTime()) stamp += 7 * 24 * 60 * 60 * 1000
    for (let i = 0; i < count; i++) result.push(new Date(stamp + i * 7 * 24 * 60 * 60 * 1000))
    return result
  }
  let cursorYear = year
  let cursorMonth = month
  while (result.length < count && cursorYear < year + 5) {
    const stamp = shanghaiStamp(
      cursorYear, cursorMonth, Number(simple.day), Number(simple.hour), minute, second,
    )
    const check = new Date(stamp + 8 * 60 * 60 * 1000)
    if (check.getUTCMonth() === cursorMonth && stamp > now.getTime()) result.push(new Date(stamp))
    cursorMonth += 1
    if (cursorMonth > 11) { cursorMonth = 0; cursorYear += 1 }
  }
  return result
}

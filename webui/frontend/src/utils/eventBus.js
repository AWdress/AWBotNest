// 简单的事件总线，用于跨组件通信
class EventBus {
  constructor() { this.events = new Map() }
  on(event, cb) {
    if (!this.events.has(event)) this.events.set(event, [])
    this.events.get(event).push(cb)
  }
  off(event, cb) {
    if (!this.events.has(event)) return
    const cbs = this.events.get(event)
    const i = cbs.indexOf(cb)
    if (i >= 0) cbs.splice(i, 1)
  }
  emit(event, data) {
    if (!this.events.has(event)) return
    this.events.get(event).forEach(cb => cb(data))
  }
}

export const eventBus = new EventBus()

export const EVENTS = {
  BOT_ROUTING_CHANGED: 'bot_routing_changed',
}

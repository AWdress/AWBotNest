const JOB_NAMES = {
  'log-cleaner': '日志自动清理', log_cleaner: '日志自动清理',
  repo_poll: '插件仓库轮询', 'repo-poll': '插件仓库轮询', 插件仓库轮询: '插件仓库轮询',
}

export function jobDisplayName(job = {}) {
  const raw = String(job.name || String(job.id || '').split('::').at(-1) || '未命名任务')
  return JOB_NAMES[raw] || raw.replaceAll('_', ' ')
}

export function jobOwnerLabel(job = {}, pluginNames = {}) {
  if (!job.plugin_id || job.plugin_id === '__platform__') return job.plugin || '平台服务'
  return job.plugin || pluginNames[job.plugin_id] || '插件任务'
}

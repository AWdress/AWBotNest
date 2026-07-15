import { importShared } from './__federation_fn_import-JrT3xvdd.js';
import Config from './__federation_expose_Config-DIoSRx0n.js';

true              &&(function polyfill() {
  const relList = document.createElement("link").relList;
  if (relList && relList.supports && relList.supports("modulepreload")) {
    return;
  }
  for (const link of document.querySelectorAll('link[rel="modulepreload"]')) {
    processPreload(link);
  }
  new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      if (mutation.type !== "childList") {
        continue;
      }
      for (const node of mutation.addedNodes) {
        if (node.tagName === "LINK" && node.rel === "modulepreload")
          processPreload(node);
      }
    }
  }).observe(document, { childList: true, subtree: true });
  function getFetchOpts(link) {
    const fetchOpts = {};
    if (link.integrity) fetchOpts.integrity = link.integrity;
    if (link.referrerPolicy) fetchOpts.referrerPolicy = link.referrerPolicy;
    if (link.crossOrigin === "use-credentials")
      fetchOpts.credentials = "include";
    else if (link.crossOrigin === "anonymous") fetchOpts.credentials = "omit";
    else fetchOpts.credentials = "same-origin";
    return fetchOpts;
  }
  function processPreload(link) {
    if (link.ep)
      return;
    link.ep = true;
    const fetchOpts = getFetchOpts(link);
    fetch(link.href, fetchOpts);
  }
}());

// 本地预览入口（npm run dev）：用一个「模拟 host」把 Config.vue 跑起来，
// 方便不启动平台也能调界面。真正运行时由平台注入真实 host（见 Config.vue 注释）。
const {createApp,h} = await importShared('vue');

const mockHost = {
  pluginId: '_TEMPLATE_VUE',
  token: 'dev',
  async getConfig() {
    console.log('[mock] getConfig');
    return { greeting: '你好（本地预览）', enabled: true }
  },
  async saveConfig(values) {
    console.log('[mock] saveConfig', values);
  },
  async callApi(path, opts = {}) {
    console.log('[mock] callApi', path, opts);
    if (path === '/ping') return { ok: true, message: 'pong', server_time: Math.floor(Date.now() / 1000) }
    if (path === '/echo') return { ok: true, received: opts.body, echo_count: 1 }
    return { ok: true }
  },
  toast: {
    success: (m) => console.log('[toast.success]', m),
    error: (m) => console.warn('[toast.error]', m),
  },
};

createApp({
  render: () => h(Config, { pluginId: mockHost.pluginId, host: mockHost }),
}).mount('#app');

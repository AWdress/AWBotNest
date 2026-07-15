import { importShared } from './__federation_fn_import-JrT3xvdd.js';

const _export_sfc = (sfc, props) => {
  const target = sfc.__vccOpts || sfc;
  for (const [key, val] of props) {
    target[key] = val;
  }
  return target;
};

const {openBlock:_openBlock,createElementBlock:_createElementBlock,createCommentVNode:_createCommentVNode,createElementVNode:_createElementVNode,vModelText:_vModelText,withDirectives:_withDirectives,vModelCheckbox:_vModelCheckbox,toDisplayString:_toDisplayString,Fragment:_Fragment} = await importShared('vue');


const _hoisted_1 = { class: "vcfg" };
const _hoisted_2 = {
  key: 0,
  class: "muted"
};
const _hoisted_3 = { class: "card" };
const _hoisted_4 = { class: "row" };
const _hoisted_5 = { class: "row switch" };
const _hoisted_6 = ["disabled"];
const _hoisted_7 = { class: "card" };
const _hoisted_8 = { class: "row" };
const _hoisted_9 = { class: "muted" };
const _hoisted_10 = { class: "row" };

const {ref,onMounted} = await importShared('vue');



const _sfc_main = {
  __name: 'Config',
  props: {
  pluginId: { type: String, required: true },
  host: { type: Object, required: true },
},
  setup(__props) {

// 插件自带的配置界面，通过模块联邦暴露给平台（见 vite.config 的 exposes './Config'）。
// 平台运行时加载本组件并注入两个 prop：
//   pluginId: 本插件 id
//   host: 平台能力对象
//     host.getConfig()            读取本插件已保存配置（Promise<对象>）
//     host.saveConfig(values)     保存配置（Promise）——存平台统一存储，插件里 ctx.config 可读到
//     host.callApi(path, {method, body})  调用插件 ctx.on_api 注册的后端接口（Promise<JSON>）
//     host.toast.success/error(msg)       弹平台提示
//     host.token                  管理员令牌（一般用不到，host.callApi 已带）
// 组件用的是平台那一份 Vue（模块联邦 shared），无需自带。
const props = __props;

const cfg = ref({ greeting: '你好', enabled: true });
const loading = ref(true);
const saving = ref(false);
const pingResult = ref('');
const echoText = ref('');

onMounted(async () => {
  try {
    const saved = await props.host.getConfig();
    cfg.value = { greeting: '你好', enabled: true, ...(saved || {}) };
  } catch (e) {
    props.host.toast.error('读取配置失败：' + (e.message || e));
  } finally {
    loading.value = false;
  }
});

async function save() {
  saving.value = true;
  try {
    await props.host.saveConfig(cfg.value);
    props.host.toast.success('配置已保存');
  } catch (e) {
    props.host.toast.error('保存失败：' + (e.message || e));
  } finally {
    saving.value = false;
  }
}

async function doPing() {
  try {
    const r = await props.host.callApi('/ping');
    pingResult.value = `pong · 服务器时间 ${r.server_time}`;
  } catch (e) {
    props.host.toast.error('ping 失败：' + (e.message || e));
  }
}

async function doEcho() {
  try {
    const r = await props.host.callApi('/echo', { method: 'POST', body: { text: echoText.value } });
    props.host.toast.success(`已回显，累计 ${r.echo_count} 次`);
  } catch (e) {
    props.host.toast.error('echo 失败：' + (e.message || e));
  }
}

return (_ctx, _cache) => {
  return (_openBlock(), _createElementBlock("div", _hoisted_1, [
    (loading.value)
      ? (_openBlock(), _createElementBlock("div", _hoisted_2, "加载配置…"))
      : (_openBlock(), _createElementBlock(_Fragment, { key: 1 }, [
          _createElementVNode("section", _hoisted_3, [
            _cache[5] || (_cache[5] = _createElementVNode("h3", null, "基础配置", -1)),
            _createElementVNode("label", _hoisted_4, [
              _cache[3] || (_cache[3] = _createElementVNode("span", null, "问候语", -1)),
              _withDirectives(_createElementVNode("input", {
                "onUpdate:modelValue": _cache[0] || (_cache[0] = $event => ((cfg.value.greeting) = $event)),
                class: "inp",
                type: "text"
              }, null, 512), [
                [_vModelText, cfg.value.greeting]
              ])
            ]),
            _createElementVNode("label", _hoisted_5, [
              _cache[4] || (_cache[4] = _createElementVNode("span", null, "启用功能", -1)),
              _withDirectives(_createElementVNode("input", {
                "onUpdate:modelValue": _cache[1] || (_cache[1] = $event => ((cfg.value.enabled) = $event)),
                type: "checkbox"
              }, null, 512), [
                [_vModelCheckbox, cfg.value.enabled]
              ])
            ]),
            _createElementVNode("button", {
              class: "btn primary",
              disabled: saving.value,
              onClick: save
            }, _toDisplayString(saving.value ? '保存中…' : '保存配置'), 9, _hoisted_6)
          ]),
          _createElementVNode("section", _hoisted_7, [
            _cache[6] || (_cache[6] = _createElementVNode("h3", null, "调用插件后端接口（ctx.on_api）", -1)),
            _createElementVNode("div", _hoisted_8, [
              _createElementVNode("button", {
                class: "btn",
                onClick: doPing
              }, "GET /ping"),
              _createElementVNode("span", _hoisted_9, _toDisplayString(pingResult.value), 1)
            ]),
            _createElementVNode("div", _hoisted_10, [
              _withDirectives(_createElementVNode("input", {
                "onUpdate:modelValue": _cache[2] || (_cache[2] = $event => ((echoText).value = $event)),
                class: "inp",
                type: "text",
                placeholder: "发点文字给 /echo"
              }, null, 512), [
                [_vModelText, echoText.value]
              ]),
              _createElementVNode("button", {
                class: "btn",
                onClick: doEcho
              }, "POST /echo")
            ])
          ])
        ], 64))
  ]))
}
}

};
const Config = /*#__PURE__*/_export_sfc(_sfc_main, [['__scopeId',"data-v-003f7034"]]);

export { Config as default };

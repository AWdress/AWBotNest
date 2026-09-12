import{i as u,_}from"./index-YpN6vmej.js";/**
 * @license @lucide/vue v1.29.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const C=e=>e==="";/**
 * @license @lucide/vue v1.29.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const A=(...e)=>e.filter((t,a,o)=>!!t&&t.trim()!==""&&o.indexOf(t)===a).join(" ").trim();/**
 * @license @lucide/vue v1.29.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const b=e=>e.replace(/([a-z0-9])([A-Z])/g,"$1-$2").toLowerCase();/**
 * @license @lucide/vue v1.29.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const M=e=>e.replace(/^([A-Z])|[\s-_]+(\w)/g,(t,a,o)=>o?o.toUpperCase():a.toLowerCase());/**
 * @license @lucide/vue v1.29.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const I=e=>{const t=M(e);return t.charAt(0).toUpperCase()+t.slice(1)};/**
 * @license @lucide/vue v1.29.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */var c={xmlns:"http://www.w3.org/2000/svg",width:24,height:24,viewBox:"0 0 24 24",fill:"none",stroke:"currentColor","stroke-width":2,"stroke-linecap":"round","stroke-linejoin":"round"};/**
 * @license @lucide/vue v1.29.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const{provide:F,inject:$}=await u("vue"),W=Symbol("lucide-icons");function j(){return $(W,{})}/**
 * @license @lucide/vue v1.29.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const{computed:O,h:S}=await u("vue"),T=({name:e,iconNode:t,"icon-node":a,absoluteStrokeWidth:o,"absolute-stroke-width":n,strokeWidth:l,"stroke-width":r,size:m,color:s,...f},{slots:g})=>{const{size:h,color:V,strokeWidth:x=2,absoluteStrokeWidth:B=!1,class:L=""}=j(),N=O(()=>{const w=C(o)||C(n)||o===!0||n===!0||B===!0,y=l||r||x||c["stroke-width"];return w?Number(y)*24/Number(m??h??c.width):y});return S("svg",{...c,...f,width:m??h??c.width,height:m??h??c.height,stroke:s??V??c.stroke,"stroke-width":N.value,class:A("lucide",L,...e?[`lucide-${b(I(e))}-icon`,`lucide-${b(e)}`]:["lucide-icon"])},[...(t??a??[]).map(w=>S(...w)),...g.default?[g.default()]:[]])};/**
 * @license @lucide/vue v1.29.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const{h:z}=await u("vue"),H=(e,t)=>(a,{slots:o,attrs:n})=>z(T,{...n,...a,iconNode:t,name:e},o.default?{default:o.default}:void 0),{normalizeClass:D,createElementVNode:i,openBlock:v,createElementBlock:k,createCommentVNode:Q}=await u("vue"),U={class:"secret-input"},Z=["type","value","placeholder","autocomplete","disabled","readonly"],J=["disabled","aria-label","title"],P={key:0,viewBox:"0 0 24 24",fill:"none",stroke:"currentColor","stroke-width":"2","stroke-linecap":"round","stroke-linejoin":"round"},G={key:1,viewBox:"0 0 24 24",fill:"none",stroke:"currentColor","stroke-width":"2","stroke-linecap":"round","stroke-linejoin":"round"},{computed:K,ref:R}=await u("vue"),X={__name:"SecretInput",props:{modelValue:{type:String,default:""},placeholder:{type:String,default:""},autocomplete:{type:String,default:"off"},maskedValue:{type:String,default:"********"},disabled:{type:Boolean,default:!1},readonly:{type:Boolean,default:!1},mono:{type:Boolean,default:!1}},emits:["update:modelValue","reveal"],setup(e,{emit:t}){const a=e,o=t,n=R(!1),l=K(()=>a.modelValue===a.maskedValue);function r(){n.value=!n.value,n.value&&l.value&&o("reveal")}return(m,s)=>(v(),k("div",U,[i("input",{class:D(["input",{mono:e.mono}]),type:n.value?"text":"password",value:e.modelValue,placeholder:e.placeholder,autocomplete:e.autocomplete,disabled:e.disabled,readonly:e.readonly,onInput:s[0]||(s[0]=f=>o("update:modelValue",f.target.value))},null,42,Z),i("button",{type:"button",class:"secret-toggle",disabled:e.disabled||!e.modelValue,"aria-label":n.value?"隐藏内容":"显示内容",title:n.value?"隐藏内容":"显示内容",onClick:r},[n.value?(v(),k("svg",P,[...s[1]||(s[1]=[i("path",{d:"M3 3l18 18"},null,-1),i("path",{d:"M10.6 10.6a2 2 0 0 0 2.8 2.8"},null,-1),i("path",{d:"M9.9 4.2A10.8 10.8 0 0 1 12 4c5.5 0 9 5 9 8a12.7 12.7 0 0 1-2.1 3.5M6.6 6.6C4.3 8 3 10.2 3 12c0 3 3.5 8 9 8 1.2 0 2.3-.2 3.3-.6"},null,-1)])])):(v(),k("svg",G,[...s[2]||(s[2]=[i("path",{d:"M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"},null,-1),i("circle",{cx:"12",cy:"12",r:"3"},null,-1)])]))],8,J)]))}},ee=_(X,[["__scopeId","data-v-52950ebc"]]),p="awbotnest:notification-sync",E="awbotnest_notification_sync",d=typeof BroadcastChannel<"u"?new BroadcastChannel(p):null;function Y(e){return{id:`${Date.now()}_${Math.random().toString(36).slice(2)}`,at:Date.now(),...e}}function te(e={}){const t=Y(e);window.dispatchEvent(new CustomEvent(p,{detail:t})),d?d.postMessage(t):localStorage.setItem(E,JSON.stringify(t))}function oe(e){const t=l=>{try{const r=e(l);r?.catch&&r.catch(()=>{})}catch{}},a=l=>t(l.detail||{}),o=l=>t(l.data||{}),n=l=>{if(!(l.key!==E||!l.newValue))try{t(JSON.parse(l.newValue))}catch{}};return window.addEventListener(p,a),d?d.addEventListener("message",o):window.addEventListener("storage",n),()=>{window.removeEventListener(p,a),d?d.removeEventListener("message",o):window.removeEventListener("storage",n)}}export{ee as S,H as c,te as p,oe as s};

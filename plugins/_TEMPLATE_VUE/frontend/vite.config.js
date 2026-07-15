import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import federation from '@originjs/vite-plugin-federation'

// 插件前端 = 模块联邦「远程」。构建产物 dist/ 由平台在运行时加载：
//   平台从 /api/plugins/<id>/fe/assets/remoteEntry.js 动态 import 本远程，
//   取出下面 exposes 的 './Config' 组件挂进配置弹窗。
// vue 声明为 shared 且 generate:false —— 复用平台那份 Vue，不重复打包、不冲突。
export default defineConfig({
  base: './',                 // 资源用相对路径，联邦按 remoteEntry 位置解析子 chunk
  plugins: [
    vue(),
    federation({
      // name 建议改成与插件 id 相关的唯一值（如 'awbotnest_myplugin'），
      // 避免多个 vue 插件同名在联邦运行时里相互干扰。
      name: 'awbotnest_remote',
      filename: 'remoteEntry.js',
      exposes: {
        './Config': './src/Config.vue',
      },
      shared: {
        vue: { singleton: true, requiredVersion: false, generate: false },
      },
      format: 'esm',
    }),
  ],
  build: {
    target: 'esnext',         // 联邦运行时用到顶层 await
    minify: false,            // 方便排查，可按需改 true
    cssCodeSplit: true,       // 分离组件样式，联邦加载时自动注入
  },
  server: { port: 5002, cors: true },
})

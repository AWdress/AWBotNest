import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import federation from '@originjs/vite-plugin-federation'
import { fileURLToPath, URL } from 'node:url'

// 构建产物输出到 ../static，由 FastAPI 托管。
// 本平台作为「模块联邦宿主」：不暴露组件，只声明共享依赖(vue)，
// 让 vue 模式插件的联邦组件复用宿主这一份 Vue（避免两份 Vue 实例冲突）。
// 运行时通过 __federation__ 的动态 API 加载插件的 remoteEntry.js（插件 id 构建期未知）。
export default defineConfig({
  plugins: [
    vue(),
    federation({
      name: 'awbotnest_host',
      remotes: {},                      // 空：插件在运行时用 __federation_method_setRemote 动态注册
      shared: {
        vue: { singleton: true, requiredVersion: false },
      },
    }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    outDir: '../static',
    emptyOutDir: true,
    assetsDir: 'assets',
    target: 'esnext',   // 模块联邦运行时用到顶层 await，需 esnext
  },
  server: {
    port: 5173,
    // 开发时把 API 请求代理到后端（默认 18001）
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:18001',
        changeOrigin: true,
      },
    },
  },
})

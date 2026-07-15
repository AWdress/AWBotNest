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
      // 插件在运行时用 __federation_method_setRemote 动态注册，构建期未知，故这里本应为空。
      // 但 @originjs/vite-plugin-federation 在 remotes 为空时有个已知 bug：不会把共享作用域
      // 占位符 __rf_placeholder__shareScope 替换成真实共享表，占位符原样漏进产物，
      // 导致加载任意 vue 模式插件、初始化共享作用域时抛 "__rf_placeholder__shareScope is not defined"。
      // 放一个永不加载的哑 remote 触发替换逻辑（运行时真正的 remote 仍走动态注册）。
      remotes: {
        __rf_stub__: '__rf_stub__@http://127.0.0.1:9/__rf_stub__.js',
      },
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

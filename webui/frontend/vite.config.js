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
    // 性能优化配置
    rollupOptions: {
      output: {
        // 手动分包：将大型库单独打包
        manualChunks(id) {
          // 将插件视图单独打包（Plugins.vue 文件较大）
          if (id.includes('views/Plugins.vue')) {
            return 'plugins-view'
          }
          // 其他视图保持默认行为
        },
        // 使用内容哈希的文件名（更好的缓存控制）
        chunkFileNames: 'assets/js/[name]-[hash].js',
        entryFileNames: 'assets/js/[name]-[hash].js',
        assetFileNames: (assetInfo) => {
          // 图片资源放到 assets/png 目录
          if (/\.(png|jpe?g|svg|gif|webp)$/i.test(assetInfo.name)) {
            return 'assets/png/[name]-[hash].[ext]'
          }
          // CSS 文件放到 assets/css 目录
          if (/\.css$/i.test(assetInfo.name)) {
            return 'assets/css/[name]-[hash].[ext]'
          }
          // 其他资源
          return 'assets/[name]-[hash].[ext]'
        },
      },
    },
    // 启用 CSS 代码分割
    cssCodeSplit: true,
    // 压缩配置 — esbuild 内置无需额外依赖
    minify: 'esbuild',
    esbuild: {
      drop: ['console', 'debugger'],  // 生产环境移除 console / debugger
    },
    // 设置分包大小警告阈值（1MB）
    chunkSizeWarningLimit: 1000,
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

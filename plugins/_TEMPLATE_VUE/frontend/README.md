# 插件前端（Vue 模式）

这是「Vue 模式」插件的前端工程：用 Vite + 模块联邦把 `src/Config.vue` 打包成远程组件，
平台在运行时加载它当作插件的配置界面。

## 开发

```bash
npm install
npm run dev      # 本地预览 Config.vue（用 src/main.js 里的模拟 host，不需启动平台）
```

## 构建（发布前必做）

```bash
npm run build    # 产物在 dist/，其中 dist/assets/remoteEntry.js 是平台加载入口
```

平台加载的是**构建产物**而非源码，所以发布插件时必须把构建好的 `dist/` 一起提交/打包。
未构建时，平台配置弹窗会提示「未随附前端构建产物」。

## 约定

- 必须暴露 `./Config` 组件（见 `vite.config.js` 的 `exposes`）——平台配置弹窗加载它。
- `vue` 声明为 `shared` 且 `generate: false`，复用平台那一份 Vue，别自己再打包一份。
- 组件通过两个 prop 与平台交互：`pluginId` 和 `host`（读写配置 / 调用插件接口 / 弹提示），
  用法见 `src/Config.vue` 顶部注释。
- 后端接口在插件的 `__init__.py` 里用 `ctx.on_api` 注册，前端 `host.callApi(path, opts)` 调用。

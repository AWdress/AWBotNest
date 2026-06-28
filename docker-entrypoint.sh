#!/bin/sh
# =============================================================================
# AWBotNest 容器入口脚本
#   以 root 启动，仅做一件事：把 bind-mount 进来的运行时目录归属修正给 awbot，
#   然后用 gosu 降权到 awbot 运行 python。
#
#   为什么需要它：docker-compose 把宿主机的 ./logs ./sessions ./db_file ./data
#   ./plugins 挂载进容器，这些目录在宿主机由 root 创建，属主覆盖了镜像构建期的
#   chown，导致降权后的 awbot(uid 10001) 无权写入（PermissionError）。
#   挂载点的属主只能在运行时修正，构建期的 chown 对它无效。
#
#   降权目标不变：真正跑业务+插件(任意代码)的 python 进程仍以 awbot 运行，
#   只有这段极短的初始化以 root 执行。
# =============================================================================
set -e

APP_USER=awbot
RUNTIME_DIRS="logs sessions db_file data plugins"

# 仅当以 root 运行时才尝试修正权限并降权（直接以非 root 运行则跳过）
if [ "$(id -u)" = "0" ]; then
    for d in $RUNTIME_DIRS; do
        # 目录可能因新挂载而不存在，先确保存在再归属
        mkdir -p "/app/$d"
        # 只在属主不是 awbot 时 chown，避免大目录每次启动重复递归
        if [ "$(stat -c '%U' "/app/$d" 2>/dev/null)" != "$APP_USER" ]; then
            chown -R "$APP_USER:$APP_USER" "/app/$d" 2>/dev/null || \
                echo "[entrypoint] warn: chown /app/$d 失败，awbot 可能无写权限" >&2
        fi
    done
    exec gosu "$APP_USER" "$@"
fi

# 非 root（例如用户自定义了 USER），直接执行
exec "$@"

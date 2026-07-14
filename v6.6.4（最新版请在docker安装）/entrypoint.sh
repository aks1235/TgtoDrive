#!/bin/bash
# TgtoDrive 双 Bot 启动脚本
# 主 Bot (tgto123.pyc) + 字幕 Bot (subtitle_bot.pyc) 并行运行

set -e

echo "🚀 TgtoDrive 启动中..."

# 启动主 Bot（后台）
python -O tgto123.pyc &
MAIN_PID=$!
echo "✅ 主 Bot 已启动 (PID: $MAIN_PID)"

# 等待一秒确保主 Bot 初始化
sleep 1

# 如果配置了字幕 Bot Token，启动字幕 Bot（后台）
if [ -n "$ENV_SUBTITLE_BOT_TOKEN" ]; then
    python -O subtitle_bot.pyc &
    SUB_PID=$!
    echo "🎬 字幕 Bot 已启动 (PID: $SUB_PID)"
else
    echo "⚠️ 未配置 ENV_SUBTITLE_BOT_TOKEN，跳过字幕 Bot"
fi

# 等待任意子进程退出
wait -n
EXIT_CODE=$?

echo "⚠️ 进程退出 (code=$EXIT_CODE)，容器将重启..."
exit $EXIT_CODE
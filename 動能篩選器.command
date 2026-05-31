#!/bin/bash
# 動能篩選器啟動器
# 雙擊此檔案即可啟動，關閉視窗自動停止 server

cd "$(dirname "$0")"

PORT=5177
PYTHON=/opt/homebrew/bin/python3.13

# 清除同 port 的舊 process
lsof -ti:$PORT | xargs kill -9 2>/dev/null

# 啟動 server（背景執行）
$PYTHON server.py &
SERVER_PID=$!

# 關閉時自動停止 server
trap "kill $SERVER_PID 2>/dev/null; exit" EXIT INT TERM

# 等待 server 就緒（最多 10 秒）
echo "啟動中..."
for i in $(seq 1 20); do
    if curl -s http://localhost:$PORT/api/state > /dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

# 在 Chrome 以 App 模式開啟（無網址列、獨立視窗）
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if [ -f "$CHROME" ]; then
    "$CHROME" --app=http://localhost:$PORT --window-size=1600,900 2>/dev/null &
else
    open "http://localhost:$PORT"
fi

echo "動能篩選器已啟動：http://localhost:$PORT"
echo "按 Ctrl+C 或關閉此視窗以停止 server"

# 保持 script 執行（讓 trap 能作用）
wait $SERVER_PID

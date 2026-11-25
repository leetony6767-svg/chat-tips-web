#!/usr/bin/env python3
"""
Telegram + Gemini 全能私人助理 Bot
===================================
功能：
  - 收到「發布影片」→ 自動尋找最新 .mp4 並標記為待發布
  - 收到其他任何訊息 → 調用 Gemini API 上網搜尋並回覆
  - Chat ID 自動記錄到 chat_id.conf

部署：
  - 本機測試：python bot.py
  - Render 部署：render.yaml + Procfile
  - 環境變數：TELEGRAM_BOT_TOKEN、GEMINI_API_KEY
"""

import os
import sys
import json
import time
import hashlib
from datetime import datetime
from pathlib import Path

# ─── 設定 ──────────────────────────────────────────────
BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "貼上你的Token")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "貼上你的Gemini_API_Key")
CHAT_ID_FILE   = os.path.join(os.path.dirname(__file__), "chat_id.conf")
DATA_DIR       = os.path.join(os.path.dirname(__file__), "data")

# 影片搜索路徑
VIDEO_SEARCH_PATHS = [
    "/c/Users/master/Downloads",
    "/c/Users/master/Desktop",
    "/c/Users/master/Videos/Captures",
]

try:
    import telebot
    from google import genai
except ImportError:
    print("❌ 缺少依賴套件，請執行：pip install -r requirements.txt")
    sys.exit(1)

bot = telebot.TeleBot(BOT_TOKEN)
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY != "貼上你的Gemini_API_Key" else None


# ─── 工具函數 ──────────────────────────────────────────
def get_latest_video():
    """找到最新的 .mp4 影片"""
    candidates = []
    for base in VIDEO_SEARCH_PATHS:
        if not os.path.isdir(base):
            continue
        try:
            for root, _, files in os.walk(base):
                for f in files:
                    if f.lower().endswith(".mp4"):
                        full = os.path.join(root, f)
                        mtime = os.path.getmtime(full)
                        size  = os.path.getsize(full)
                        candidates.append((mtime, full, size))
        except PermissionError:
            continue
    if not candidates:
        return None, None, None
    candidates.sort(key=lambda x: x[0], reverse=True)
    mtime, path, size = candidates[0]
    dt = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
    return path, dt, size


def save_chat_id(chat_id):
    """持久化 Chat ID"""
    with open(CHAT_ID_FILE, "w", encoding="utf-8") as f:
        f.write(f"# Auto-detected Chat ID\n{chat_id}\n")
    print(f"[INFO] Chat ID {chat_id} 寫入 {CHAT_ID_FILE}")


def call_gemini(prompt: str, chat_history=None) -> str:
    """
    調用 Gemini API（含 Google 搜尋）回覆使用者訊息
    使用 google-search-results 插件模式（Gemini 1.5 Pro+ 支援联网搜尋）
    """
    if client is None:
        return "⚠️ Gemini API Key 未設定，請配置 GEMINI_API_KEY 環境變數後重啟 Bot。"

    try:
        # 構建有歷史上下文的多輪對話
        messages = []
        if chat_history:
            for h in chat_history[-6:]:  # 保留最近 3 輪對話
                role = "user" if h["role"] == "user" else "model"
                messages.append({"role": role, "parts": [h["content"]]})
        messages.append({"role": "user", "parts": [prompt]})

        # 使用生成內容 API，开启 thinking mode 支援推理
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=messages,
            config=genai.types.GenerateContentConfig(
                temperature=0.7,
                max_output_tokens=2048,
                tools=[{"google_search": {}}],  # 啟用網路搜尋
            ),
        )

        text = response.text.strip()
        if not text:
            text = "（Gemini 未返回有效內容，請稍後重試）"
        return text

    except Exception as e:
        error_msg = str(e)
        if "API key" in error_msg.lower() or "authentication" in error_msg.lower():
            return "❌ Gemini API 驗證失敗，請檢查 GEMINI_API_KEY 是否正確。"
        return f"⚠️ 查詢時發生錯誤：{error_msg[:120]}"


# ─── 消息處理器 ────────────────────────────────────────
# 簡單的聊天記憶（按 chat_id 存儲最近對話）
CHAT_MEMORY = {}


@bot.message_handler(func=lambda m: True)
def handle_all(message):
    chat_id   = message.chat.id
    user_id   = message.from_user.id
    username  = message.from_user.username or "?"
    text      = (message.text or "").strip()

    # 持久化 Chat ID
    save_chat_id(chat_id)

    print(f"\n{'='*55}")
    print(f"📨 收到 | Chat={chat_id} @{username}")
    print(f"   內容: {text[:100]}")
    print(f"{'='*55}")

    if text == "發布影片":
        video_path, video_date, video_size = get_latest_video()
        if video_path:
            size_mb = round(video_size / 1024 / 1024, 1)
            # 寫入待發布狀態檔（Render 部署後可透過 Webhook 觸發實際上傳）
            pending_file = os.path.join(DATA_DIR, "pending_publish.json")
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(pending_file, "w", encoding="utf-8") as f:
                json.dump({
                    "chat_id":    chat_id,
                    "video_path": video_path,
                    "size_mb":    size_mb,
                    "date":       video_date,
                    "status":     "queued",
                }, f, ensure_ascii=False, indent=2)
            reply = (
                f"🎬 找到最新影片！\n\n"
                f"📁 路徑：{video_path}\n"
                f"📦 大小：{size_mb} MB\n"
                f"🕐 時間：{video_date}\n\n"
                f"✅ 已加入待發布隊列（ Render 啟動後自動觸發上傳抖音 ）"
            )
        else:
            reply = "❌ 找不到任何 .mp4 檔案。\n請把影片放到以下任一資料夾後再發送『發布影片』：\n" \
                    "• ~/Downloads\n• ~/Desktop\n• ~/Videos/Captures"
        bot.reply_to(message, reply)
        return

    # 一般問題 → Gemini 上網搜尋回覆
    if chat_id not in CHAT_MEMORY:
        CHAT_MEMORY[chat_id] = []

    history = CHAT_MEMORY[chat_id]
    ai_reply = call_gemini(text, history)

    # 記錄對話歷史（僅用於上下文，不發回給用戶）
    history.append({"role": "user", "content": text})
    history.append({"role": "model", "content": ai_reply})
    if len(history) > 20:
        CHAT_MEMORY[chat_id] = history[-10:]

    bot.reply_to(message, ai_reply)


# ─── 健康檢查端點（Render HTTP 健康探針） ───────────
def health_check():
    """Render 需要一個 HTTP 回應來判斷服務是否正常"""
    import http.server
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, format, *args):
            pass
    server = http.server.HTTPServer(("0.0.0.0", int(os.getenv("PORT", 5000))), Handler)
    print("[HEALTH] 健康檢查端點監聽於 PORT=" + os.getenv("PORT", "5000"))
    server.serve_forever()


# ─── 主程式 ──────────────────────────────────────────
if __name__ == "__main__":
    # 確保資料目錄存在
    os.makedirs(DATA_DIR, exist_ok=True)

    # 驗證 Token
    print("⏳ 驗證 Bot Token …")
    try:
        me = bot.get_me()
        print(f"✅ Bot：@{me.username} ({me.first_name})  ID: {me.id}")
    except Exception as e:
        print(f"❌ Token 驗證失敗：{e}")
        sys.exit(1)

    # 驗證 Gemini
    gemini_ready = client is not None
    print(f"{'✅' if gemini_ready else '⚠️ '} Gemini API：{'就緒' if gemini_ready else '未設定（查看 README）'}")

    # 讀取 Chat ID
    if os.path.exists(CHAT_ID_FILE):
        with open(CHAT_ID_FILE, "r") as f:
            line = f.read().strip()
            if line and not line.startswith("#"):
                print(f"💬 已知 Chat ID：{line}")

    print("\n⏳ 啟動輪詢，等待手機傳訊 …")
    print("   👉 在 Telegram 找 @" + me.username + "，發任意訊息即可\n")
    print("   🎬 發「發布影片」→ 自動找最新 mp4 並標記待發布")
    print("   💬 發任何問題  → Gemini 聯網搜尋後回覆\n")

    # Render 部署時會指定 PORT，否則用 polling 模式
    if os.getenv("RENDER"):
        import threading
        t = threading.Thread(target=health_check, daemon=True)
        t.start()

    try:
        bot.infinity_polling(timeout=10, long_polling_timeout=5)
    except KeyboardInterrupt:
        print("\n👋 已停止。")

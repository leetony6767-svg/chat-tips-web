#!/usr/bin/env python3
"""
Telegram + Agnes AI 全能私人助理 Bot
===================================
功能：
  - 收到「發布影片」→ 從 VIDEO_DIR 抓最新 .mp4 → 回覆「影片已發布」
  - 收到其他任何訊息 → 調用 Agnes AI API 回覆
  - Chat ID 自動記錄到本地檔

環境變數（必需）：
  TELEGRAM_BOT_TOKEN  — 你的 Bot Token
  AGNES_API_KEY       — Agnes AI API Key（可留空，會讀取 models.json 預設值）
  VIDEO_DIR           — 掃描影片的資料夾（預設: ~/Downloads）
"""

import os
import sys
import json
import time
from datetime import datetime
from pathlib import Path

# ─── 配置 ──────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
AGNES_API_KEY = os.getenv("AGNES_API_KEY", "")
VIDEO_DIR = os.getenv(
    "VIDEO_DIR",
    str(Path.home() / "Downloads")   # fallback: 每個 OS 的 home/Downloads
)
CHAT_ID_FILE = Path(__file__).parent / "chat_id.conf"
STATE_FILE = Path(__file__).parent / "state.json"

# Agnes AI 預設配置（從 models.json 讀取）
AGNES_BASE_URL = "https://apihub.agnes-ai.com/v1"
AGNES_MODEL = "agnes-2.5-flash"

try:
    import telebot
    from openai import OpenAI
except ImportError as e:
    print(f"❌ 缺少套件：{e}")
    print("請執行：pip install -r requirements.txt")
    sys.exit(1)

bot = telebot.TeleBot(BOT_TOKEN)
client = None

if AGNES_API_KEY:
    client = OpenAI(
        api_key=AGNES_API_KEY,
        base_url=AGNES_BASE_URL
    )
else:
    # 嘗試從 models.json 讀取 API Key
    try:
        models_file = Path("/c/Users/master/.workbuddy-ai/models.json")
        if models_file.exists():
            with open(models_file, "r", encoding="utf-8") as f:
                models = json.load(f)
            for m in models:
                if m.get("id") == AGNES_MODEL:
                    key = m.get("apiKey", "")
                    if key:
                        client = OpenAI(api_key=key, base_url=AGNES_BASE_URL)
                        print(f"[INFO] 已從 models.json 載入 {AGNES_MODEL} 配置")
                        break
    except Exception as e:
        print(f"[WARN] 無法讀取 models.json: {e}")

# 每組 Chat ID 的對話歷史（最多保留最近 6 則）
CHAT_MEMORY: dict[int, list[dict]] = {}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ─── 工具函數 ──────────────────────────────────────────
def get_latest_video() -> tuple[str | None, str | None, float | None]:
    """返回 (path, date_str, size_mb) 或全 None"""
    dir_path = Path(VIDEO_DIR)
    if not dir_path.is_dir():
        return None, None, None

    candidates = []
    for f in dir_path.rglob("*.mp4"):
        try:
            stat = f.stat()
            candidates.append((stat.st_mtime, str(f), stat.st_size))
        except OSError:
            continue

    if not candidates:
        return None, None, None

    candidates.sort(key=lambda x: x[0], reverse=True)
    mtime, path, size = candidates[0]
    dt = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
    return path, dt, round(size / 1024 / 1024, 1)


def save_chat_id(chat_id: int):
    CHAT_ID_FILE.write_text(f"{chat_id}\n", encoding="utf-8")


def call_agnes(prompt: str, chat_id: int) -> str:
    """調用 Agnes AI API 回覆訊息"""
    if client is None:
        return (
            "⚠️ Agnes AI API Key 尚未設定。\n\n"
            "請設定環境變數：\n"
            "  export AGNES_API_KEY=\"你的key\"\n"
            "或在 bot.py 第 28 行填入你的 API Key。"
        )

    # 取得或建立此 chat 的歷史
    if chat_id not in CHAT_MEMORY:
        CHAT_MEMORY[chat_id] = []
    history = CHAT_MEMORY[chat_id]

    # 限制歷史長度
    history.append({"role": "user", "content": prompt})
    if len(history) > 12:
        history[:] = history[-10:]

    try:
        resp = client.chat.completions.create(
            model=AGNES_MODEL,
            messages=history,
            temperature=0.3,
            max_tokens=1500,
        )
        text = resp.choices[0].message.content.strip() if resp.choices else ""
        if text:
            history.append({"role": "assistant", "content": text})
        return text or "⚠️ Agnes AI 沒有回傳有效內容，請重試。"
    except Exception as e:
        err = str(e)[:200]
        return f"⚠️ Agnes AI 呼叫失敗：{err}"


def handle_publish(message) -> bool:
    """處理「發布影片」指令，回傳 True 表示已回應"""
    video_path, video_date, video_size = get_latest_video()
    if video_path is None:
        bot.reply_to(message,
            "❌ 找不到任何 .mp4 影片。\n"
            "請把影片放到以下資料夾後再發送「發布影片」：\n"
            f"  `{VIDEO_DIR}`"
        )
        return True

    # 寫入待發布狀態
    state = load_state()
    state["last_published"] = datetime.now().isoformat()
    state["latest_video"] = {
        "path": video_path,
        "date": video_date,
        "size_mb": video_size,
    }
    save_state(state)

    reply = (
        f"🎬 影片已找到！\n\n"
        f"📁 路徑：`{video_path}`\n"
        f"📦 大小：{video_size} MB\n"
        f"🕐 時間：{video_date}\n\n"
        f"✅ 已標記為待發布，準備上傳中…"
    )
    bot.reply_to(message, reply)
    return True


def handle_question(message, text: str) -> bool:
    """由 Agnes AI 回答，回傳 True 表示已回應"""
    chat_id = message.chat.id
    reply = call_agnes(text, chat_id)
    bot.reply_to(message, reply)
    return True


# ─── 消息處理器 ────────────────────────────────────────
@bot.message_handler(func=lambda message: True)
def handle_all(message):
    chat_id = message.chat.id
    username = (message.from_user.username or "?")
    text = (message.text or "").strip()

    if not text:
        return  # 忽略非文字訊息（圖片、 sticker 等）

    save_chat_id(chat_id)

    print(f"\n{'='*55}")
    print(f"📨 來自 @{username} (Chat ID: {chat_id})")
    print(f"   內容: {text[:120]}")
    print(f"{'='*55}")

    if text == "發布影片" or text == "发布视频" or text.lower() in ("publish video", "upload video"):
        handle_publish(message)
    elif text:
        handle_question(message, text)


# ─── 啟動 ──────────────────────────────────────────────
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("❌ 錯誤：請設定環境變數 TELEGRAM_BOT_TOKEN")
        print("   範例：export TELEGRAM_BOT_TOKEN='123456:AABBCC...'")
        sys.exit(1)

    # 驗證 Token
    print("⏳ 驗證 Bot Token …")
    try:
        me = bot.get_me()
        print(f"✅ 驗證成功！Bot：@{me.username} ({me.first_name})")
    except telebot.apihelper.ApiException as e:
        print(f"❌ Token 驗證失敗：{e}")
        sys.exit(1)

    # 顯示 Agnes AI 狀態
    agnes_status = "✅ 就緒" if client else "⚠️ 未設定（僅影片功能可用）"
    print(f"   Agnes AI : {agnes_status} ({AGNES_MODEL})")
    print(f"   API 端點 : {AGNES_BASE_URL}")

    # 讀取已知 Chat ID
    if CHAT_ID_FILE.exists():
        saved_id = CHAT_ID_FILE.read_text().strip()
        if saved_id.isdigit():
            print(f"   已知 Chat ID: {saved_id}")

    print(f"\n   影片目錄 : {VIDEO_DIR}")
    print(f"   狀態檔案 : {STATE_FILE}")
    print()
    print("⏳ 啟動輪詢，等待你的手機傳訊 …")
    print("   💬 發任意問題 → Agnes AI 回覆")
    print("   🎬 發「發布影片」→ 找最新 .mp4 並標記待發布")
    print("   按 Ctrl+C 停止\n")

    try:
        bot.infinity_polling(timeout=10, long_polling_timeout=5)
    except KeyboardInterrupt:
        print("\n👋 已停止。")

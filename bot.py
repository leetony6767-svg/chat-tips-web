#!/usr/bin/env python3
"""
Telegram + Agnes AI 本機電腦助理 Bot
====================================
直接在執行此程式的「這台電腦」上運作，可透過 Telegram 下指令控制本機。

環境變數（建議設定）：
  TELEGRAM_BOT_TOKEN  — 你的 Telegram Bot Token（必需）
  AGNES_API_KEY       — Agnes AI API Key（可留空，會嘗試讀 models.json）
  VIDEO_DIR           — 掃描影片的資料夾（預設: ~/Downloads）
  ALLOWED_CHAT_ID     — 允許控制電腦的 Chat ID（強烈建議填寫，例如 123456789；留空則不限制）
"""

import os
import sys
import json
import subprocess
from datetime import datetime
from pathlib import Path

# ─── 配置 ──────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
AGNES_API_KEY = os.getenv("AGNES_API_KEY", "").strip()
ALLOWED_CHAT_ID = os.getenv("ALLOWED_CHAT_ID", "").strip()
VIDEO_DIR = os.getenv(
    "VIDEO_DIR",
    str(Path.home() / "Downloads")
)

BASE_DIR = Path(__file__).resolve().parent
CHAT_ID_FILE = BASE_DIR / "chat_id.conf"
STATE_FILE = BASE_DIR / "state.json"

AGNES_BASE_URL = "https://apihub.agnes-ai.com/v1"
AGNES_MODEL = "agnes-2.5-flash"

# 阻擋明顯高風險的破壞性指令
BLOCKED_KEYWORDS = [
    "rm -rf /",
    "del /f /s /q",
    "format ",
    "shutdown",
    "reboot",
    "mkfs",
    ":(){:|:&};:",
]

try:
    import telebot
    from openai import OpenAI
except ImportError as e:
    print(f"❌ 缺少套件：{e}")
    print("請執行：pip install -r requirements.txt")
    sys.exit(1)

bot = telebot.TeleBot(BOT_TOKEN)
client = None


def init_agnes_client():
    global client
    if AGNES_API_KEY:
        client = OpenAI(api_key=AGNES_API_KEY, base_url=AGNES_BASE_URL)
        return

    candidate_paths = [
        Path.home() / ".workbuddy-ai" / "models.json",
        Path("/c/Users/master/.workbuddy-ai/models.json"),
    ]
    for models_file in candidate_paths:
        try:
            if models_file.exists():
                models = json.loads(models_file.read_text(encoding="utf-8"))
                if isinstance(models, list):
                    for m in models:
                        if m.get("id") == AGNES_MODEL and m.get("apiKey"):
                            client = OpenAI(
                                api_key=m["apiKey"],
                                base_url=AGNES_BASE_URL,
                            )
                            print(f"[INFO] 已從 {models_file} 載入 {AGNES_MODEL}")
                            return
        except Exception as e:
            print(f"[WARN] 讀取 {models_file} 失敗：{e}")


init_agnes_client()
CHAT_MEMORY: dict[int, list[dict]] = {}


def is_authorized(chat_id: int) -> bool:
    if not ALLOWED_CHAT_ID:
        return True
    return str(chat_id) == ALLOWED_CHAT_ID


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def save_chat_id(chat_id: int):
    CHAT_ID_FILE.write_text(f"{chat_id}\n", encoding="utf-8")


def get_latest_video() -> tuple[str | None, str | None, float | None]:
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


def open_on_pc(target: str) -> str:
    target = target.strip().strip('"').strip("'")
    if not target:
        return "⚠️ 用法：`/open 程式名稱或檔案路徑`"

    try:
        if os.name == "nt":
            os.startfile(target)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
        return f"✅ 已在電腦開啟：`{target}`"
    except Exception as e:
        return f"❌ 開啟失敗：{e}"


def run_on_pc(command: str) -> str:
    cmd = command.strip()
    if not cmd:
        return "⚠️ 用法：`/run 要執行的指令`"

    lower_cmd = cmd.lower()
    if any(bad in lower_cmd for bad in BLOCKED_KEYWORDS):
        return "🚫 為安全起見，已阻擋高風險系統指令。"

    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(BASE_DIR),
        )
        output = (result.stdout or "") + (
            f"\n[STDERR]\n{result.stderr}" if result.stderr else ""
        )
        output = output.strip() or "（執行完成，無輸出內容）"
        if len(output) > 3000:
            output = output[:3000] + "\n...（輸出過長已截斷）"
        return f"💻 指令：`{cmd}`\n退出碼：{result.returncode}\n\n```\n{output}\n```"
    except subprocess.TimeoutExpired:
        return f"⏱️ 指令逾時（超過 30 秒）：`{cmd}`"
    except Exception as e:
        return f"❌ 執行失敗：{e}"


def call_agnes(prompt: str, chat_id: int) -> str:
    if client is None:
        return (
            "⚠️ Agnes AI API Key 尚未設定。\n\n"
            "請設定環境變數 `AGNES_API_KEY`，或在 `models.json` 中配置金鑰。"
        )

    history = CHAT_MEMORY.setdefault(chat_id, [])
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
        return f"⚠️ Agnes AI 呼叫失敗：{str(e)[:200]}"


def handle_publish(message):
    video_path, video_date, video_size = get_latest_video()
    if video_path is None:
        bot.reply_to(
            message,
            f"❌ 找不到任何 .mp4 影片。\n請把影片放到以下資料夾後再發送「發布影片」：\n`{VIDEO_DIR}`",
        )
        return

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
        f"✅ 已標記為待發布。"
    )
    bot.reply_to(message, reply)


@bot.message_handler(func=lambda message: True)
def handle_all(message):
    chat_id = message.chat.id
    text = (message.text or "").strip()
    if not text:
        return

    save_chat_id(chat_id)

    if not is_authorized(chat_id):
        bot.reply_to(
            message,
            f"🔒 未授權的 Chat ID：`{chat_id}`\n若要允許此帳號控制電腦，請設定環境變數 `ALLOWED_CHAT_ID={chat_id}`。",
        )
        return

    lower_text = text.lower()

    if text in ("發布影片", "发布视频") or lower_text in ("publish video", "upload video"):
        handle_publish(message)
    elif lower_text == "/status":
        bot.reply_to(
            message,
            f"🖥️ 本機 Bot 運作中\n"
            f"• Chat ID：`{chat_id}`\n"
            f"• 影片目錄：`{VIDEO_DIR}`\n"
            f"• Agnes AI：{'已就緒' if client else '未設定'}\n"
            f"• 可用指令：`/status`、`/open <路徑或程式>`、`/run <指令>`、`發布影片`",
        )
    elif lower_text.startswith("/open "):
        bot.reply_to(message, open_on_pc(text[6:]))
    elif lower_text.startswith("/run "):
        bot.reply_to(message, run_on_pc(text[5:]))
    else:
        bot.reply_to(message, call_agnes(text, chat_id))


if __name__ == "__main__":
    if not BOT_TOKEN:
        print("❌ 錯誤：請設定環境變數 TELEGRAM_BOT_TOKEN")
        sys.exit(1)

    try:
        me = bot.get_me()
        print(f"✅ Bot 已連線：@{me.username} ({me.first_name})")
    except Exception as e:
        print(f"❌ Token 驗證失敗：{e}")
        sys.exit(1)

    print(f"📁 影片目錄：{VIDEO_DIR}")
    print(f"🔐 授權 Chat ID：{ALLOWED_CHAT_ID or '未限制（建議設定）'}")
    print("⏳ 開始監聽 Telegram 指令（直接在本機執行）...")

    try:
        bot.infinity_polling(timeout=10, long_polling_timeout=5)
    except KeyboardInterrupt:
        print("\n👋 已停止。")
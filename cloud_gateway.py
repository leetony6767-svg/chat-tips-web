#!/usr/bin/env python3
"""
cloud_gateway.py — 雲端 Telegram 入口 + Agnes AI + 本機控制中樞
==============================================================
部署在 Render（Linux）。這是「唯一」的 Telegram 入口，改用 Webhook 收 update，
不會跟本機端搶 getUpdates。

分流邏輯（單一事實來源）：
  - 文字以控制前綴開頭（/open /run /script /list /shot，或 @local 開頭）
        → 產生 job 存入「控制隊列」，回「⏳ 已送本機執行中」；
          本機 windows_worker 出向長輪詢取走、執行、回傳結果，雲端再發回 Telegram。
  - 其他訊息（一般問答）
        → 直連 Agnes AI，回完整答案。

HTTP 端點：
  POST /telegram-webhook   收 Telegram update（webhook）
  GET  /worker/poll        本機 worker 出向長輪詢取控制 job
  POST /worker/result      本機 worker 回傳執行結果（含截圖 base64）
  GET  /health             Render 健康檢查
  GET  /                   Render 健康檢查

本機 worker 與雲端之間用 AGENT_KEY 驗證（HTTP header X-Agent-Key）。
"""
import os
import sys
import io
import json
import base64
import queue
import threading
from datetime import datetime

# ─── 強制 UTF-8 ─────────────────────────────────────────────
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", write_through=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", write_through=True)

import requests
from flask import Flask, request, jsonify

# ─── 配置 ───────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
AGNES_API_KEY = os.environ.get("AGNES_API_KEY", "").strip()
AGNES_BASE_URL = os.environ.get("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1").strip()
AGNES_MODEL = os.environ.get("AGNES_MODEL", "agnes-2.5-flash").strip()
AGENT_KEY = os.environ.get("AGENT_KEY", "local-agent-key").strip()
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID", "").strip()
PORT = int(os.environ.get("PORT", "8080"))

# 雲端自己的 HTTPS 入口（webhook 目標），可被環境變數覆蓋
CLOUD_BASE_URL = os.environ.get("CLOUD_BASE_URL", "https://chat-tips-web.onrender.com").rstrip("/")

# 控制指令前綴：命中即視為「要控制本機」
CONTROL_PREFIXES = ("/open", "/run", "/script", "/list", "/ls", "/shot", "/screenshot", "@local")

# Agnes 客戶端與對話記憶
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

agnes_client = OpenAI(api_key=AGNES_API_KEY, base_url=AGNES_BASE_URL) if (OpenAI and AGNES_API_KEY) else None
CHAT_MEMORY: dict = {}
AGNES_SYSTEM = "你是全能私人助理，請以繁體中文簡潔、直接、實用回答使用者。"

# ─── Telegram 低層輔助（用 requests 直打，避免多入口） ──────
def tg(method, **params):
    """呼叫 Telegram Bot API（非 webhook 相關），回 JSON dict。"""
    if method == "getUpdates":
        raise RuntimeError("本程式禁止 getUpdates（會與本機端衝突）")
    r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                    params=params, timeout=30)
    return r.json()

def tg_send(chat_id, text, reply_to=None, parse="markdown"):
    p = {"chat_id": chat_id, "text": text[:4000]}
    if parse:
        p["parse_mode"] = parse
    if reply_to:
        p["reply_to_message_id"] = reply_to
    return tg("sendMessage", **p)

def tg_send_photo(chat_id, image_b64, caption, reply_to=None):
    # 用 multipart 上傳 base64 圖片
    data = {"chat_id": chat_id, "caption": caption[:1000]}
    if reply_to:
        data["reply_to_message_id"] = reply_to
    files = {"photo": ("shot.jpg", base64.b64decode(image_b64), "image/jpeg")}
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                     data=data, files=files, timeout=40)
    return r.json()

def is_authorized(chat_id):
    return (not ALLOWED_CHAT_ID) or (str(chat_id) == ALLOWED_CHAT_ID)

# ─── 控制隊列（本機 worker 出向取走） ─────────────────────
JOB_QUEUE: "queue.Queue" = queue.Queue()
JOB_COUNTER = {"n": 0}
JOB_LOCK = threading.Lock()

def enqueue_job(kind, arg, chat_id, message_id, source):
    with JOB_LOCK:
        JOB_COUNTER["n"] += 1
        job_id = f"job-{JOB_COUNTER['n']}"
    JOB_QUEUE.put({
        "job_id": job_id,
        "kind": kind,
        "arg": arg,
        "chat_id": chat_id,
        "message_id": message_id,
        "source": source,
        "created": datetime.now().isoformat(),
    })
    return job_id

def call_agnes(text, chat_id):
    if agnes_client is None:
        return "⚠️ Agnes AI 未就緒（AGNES_API_KEY 未設定）。"
    history = CHAT_MEMORY.setdefault(chat_id, [])
    history.append({"role": "user", "content": text})
    if len(history) > 10:
        history[:] = history[-10:]
    try:
        resp = agnes_client.chat.completions.create(
            model=AGNES_MODEL,
            messages=[{"role": "system", "content": AGNES_SYSTEM}] + history,
            temperature=0.3, max_tokens=1500,
        )
        ans = (resp.choices[0].message.content or "").strip()
        if ans:
            history.append({"role": "assistant", "content": ans})
        return ans or "⚠️ Agnes AI 沒有回傳有效內容，請再試一次。"
    except Exception as e:
        if history and history[-1].get("role") == "user":
            history.pop()
        return f"⚠️ Agnes AI 呼叫失敗：{str(e)[:200]}"

def _classify_kind(text):
    """回 (kind, arg)：open/run/script/list/shot；非控制指令回 None。"""
    t = text.strip()
    low = t.lower()
    # @local 後接指令
    if low.startswith("@local"):
        t = t[len("@local"):].strip()
        low = t.lower()
    if low.startswith("/open"):
        return "open", t.split(" ", 1)[1] if " " in t else ""
    if low.startswith("/run") or low.startswith("/cmd"):
        return "run", t.split(" ", 1)[1] if " " in t else ""
    if low.startswith("/script"):
        return "script", t.split(" ", 1)[1] if " " in t else ""
    if low.startswith("/list") or low.startswith("/ls"):
        return "list", t.split(" ", 1)[1] if " " in t else ""
    if low.startswith("/shot") or low.startswith("/screenshot"):
        return "shot", ""
    return None

# ─── Flask App ──────────────────────────────────────────────
app = Flask(__name__)
app.config.update(THREADED=True, JSON_SORT_KEYS=False)

@app.route("/", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    return "tg-agnes-gateway: ok\n"

@app.route("/telegram-webhook", methods=["POST"])
def webhook():
    """Telegram webhook 入口。"""
    update = request.get_json(force=True, silent=True) or {}
    msg = update.get("message") or {}
    text = (msg.get("text") or "").strip()
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    username = (msg.get("from_user") or {}).get("username", "?")
    print(f"[WEBHOOK] chat={chat_id} @{username} text={text[:80]!r}", flush=True)

    if not text:
        return jsonify({"ok": True, "ignored": "no-text"})
    if not is_authorized(chat_id):
        tg_send(chat_id, f"🔒 未授權 Chat（`{chat_id}`），僅允許 `{ALLOWED_CHAT_ID or '未限制'}` 控制。")
        return jsonify({"ok": True})

    matched = _classify_kind(text)
    if matched:
        # 控制本機 → 入隊列
        kind, arg = matched
        job_id = enqueue_job(kind, arg, chat_id, message_id, "telegram")
        tg_send(chat_id,
                f"⏳ 已把「{kind}」送給本機執行（{job_id}）。\n"
                f"本機在線時會稍候回覆結果；離線時會一直等待。",
                reply_to=message_id, parse=None)
        print(f"[QUEUE] 控制指令入隊列：{job_id} {kind} {arg!r}", flush=True)
        return jsonify({"ok": True, "queued": job_id})

    # 一般問答 → Agnes AI
    ans = call_agnes(text, chat_id)
    tg_send(chat_id, ans, reply_to=message_id)
    print(f"[AGNES] 回覆 {len(ans)} 字", flush=True)
    return jsonify({"ok": True})

@app.route("/worker/poll", methods=["GET"])
def worker_poll():
    """本機 worker 出向長輪詢。參數 timeout（秒）。"""
    if request.headers.get("X-Agent-Key") != AGENT_KEY:
        return jsonify({"error": "bad-key"}), 401
    try:
        timeout = int(request.args.get("timeout", "25"))
    except ValueError:
        timeout = 25
    try:
        job = JOB_QUEUE.get(block=True, timeout=min(timeout, 55))
    except queue.Empty:
        return jsonify({"jobs": []})
    JOB_QUEUE.task_done()
    return jsonify({"jobs": [job]})

@app.route("/worker/result", methods=["POST"])
def worker_result():
    """本機 worker 回傳結果。body: {job_id, text, image_b64?}"""
    if request.headers.get("X-Agent-Key") != AGENT_KEY:
        return jsonify({"error": "bad-key"}), 401
    data = request.get_json(force=True, silent=True) or {}
    job = data.get("job", {}) or {}
    chat_id = data.get("chat_id") or job.get("chat_id")
    message_id = data.get("message_id") or job.get("message_id")
    text = data.get("text", "")
    image_b64 = data.get("image_b64")

    if image_b64:
        tg_send_photo(chat_id, image_b64, text[:1000], reply_to=message_id)
    if text:
        tg_send(chat_id, text, reply_to=message_id, parse="markdown")
    print(f"[RESULT] job={data.get('job_id')} 回傳 {len(text)} 字" +
          (" + 圖片" if image_b64 else ""), flush=True)
    return jsonify({"ok": True})


def setup_webhook():
    """啟動時把 Telegram 指向本 service 的 /telegram-webhook。"""
    url = f"{CLOUD_BASE_URL}/telegram-webhook"
    r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                     params={"url": url, "drop_pending_updates": "true"},
                     timeout=30)
    d = r.json()
    print(f"[WEBHOOK] setWebhook -> {url}  ok={d.get('ok')} {d.get('description','')}", flush=True)


def main():
    global BOT_TOKEN
    if not BOT_TOKEN:
        print("❌ 缺少 TELEGRAM_BOT_TOKEN")
        sys.exit(1)

    me = tg("getMe")
    print(f"✅ Gateway 連線 @{me.get('result', {}).get('username')}", flush=True)
    print(f"🧠 Agnes: {'就緒' if agnes_client else '未設定'} ({AGNES_MODEL})", flush=True)
    print(f"🌐 Webhook 目標：{CLOUD_BASE_URL}/telegram-webhook", flush=True)
    print(f"🔑 AGENT_KEY 已設定（worker 驗證用）", flush=True)

    setup_webhook()

    # 啟動後 3 秒再校正一次 webhook（避免冷啟動時 URL 未就緒）
    threading.Timer(3.0, lambda: setup_webhook()).start()

    print(f"🌐 啟動 HTTP 伺服器 http://0.0.0.0:{PORT}", flush=True)
    app.run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    main()

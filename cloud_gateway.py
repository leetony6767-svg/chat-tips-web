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
import sqlite3
import threading
import time
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
    """呼叫 Telegram Bot API（非 webhook 相關），回 JSON dict。

    注意：getUpdates 只能在「webhook 已被暫時移除」的窗口內使用
    （見 reclaim_offline_updates）；webhook 生效時 getUpdates 會回 409。
    本機 worker 已改做出向 HTTP 輪詢，不使用 getUpdates，故無搶 update 衝突。
    """
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

# ─── 持久化控制隊列（SQLite；本機 worker 出向取走） ────────
# 目的：Render 休眠/重啟後，排中與已 claim 未完成的控制指令不會被清空，
#       開機/重啟後本機 worker 出向長輪詢可把「之前累積」的指令取走補執行。
DB_FILE = os.environ.get("JOB_DB_FILE", "jobs.db").strip()
DB_PATH = DB_FILE if os.path.isabs(DB_FILE) else os.path.join(os.getcwd(), DB_FILE)
DB_LOCK = threading.Lock()
# claim 後未回傳 result 的 job，逾時後自動排回 pending（防 worker 崩潰卡住）
CLAIM_TTL_SEC = int(os.environ.get("JOB_CLAIM_TTL_SEC", "180"))
# 已完成的 job 保留天數（純空間整理用；超過才刪）
DONE_KEEP_DAYS = int(os.environ.get("JOB_DONE_KEEP_DAYS", "7"))

def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

import contextlib
@contextlib.contextmanager
def db():
    """取得 DB 鎖 + 連線，保證關閉（sqlite3 的 with 只 commit 不 close，會洩漏）。"""
    conn = _connect()
    with DB_LOCK:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

def _init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT UNIQUE NOT NULL,
            kind TEXT NOT NULL,
            arg TEXT NOT NULL DEFAULT '',
            chat_id TEXT,
            message_id TEXT,
            source TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created TEXT,
            claimed_at TEXT,
            done_at TEXT,
            update_id TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
        # 既有 DB 補欄（CREATE IF NOT EXISTS 不會為舊表加新欄；加不成就忽略）
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN update_id TEXT")
        except Exception:
            pass
        # 原子去重：同一 update_id 只允許存在「一筆未刪除」的 job（NULL 不受限）。
        # 讓「webhook 路徑」與「離線補抓路徑」對同一條指令的競態，由 DB 唯一約束擋下，
        # 結構性保證同一條離線指令絕不被重複入隊執行。
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_update_id "
                "ON jobs(update_id) WHERE update_id IS NOT NULL")
        except Exception:
            pass

_init_db()

def _has_update(update_id):
    """同 update_id 是否已入過隊列（reclaim 去重用；DB 唯一約束是真正的兜底）。"""
    if not update_id:
        return False
    with db() as conn:
        return conn.execute(
            "SELECT 1 FROM jobs WHERE update_id=? LIMIT 1", (str(update_id),)).fetchone() is not None

def _next_job_id():
    with db() as conn:
        row = conn.execute("SELECT MAX(id) AS m FROM jobs").fetchone()
        return (row["m"] or 0) + 1

def enqueue_job(kind, arg, chat_id, message_id, source, update_id=None):
    """寫入 jobs 表（持久化），回傳 job_id；若同 update_id 已入過隊列回傳 None（去重）。

    原子性：以 DB 唯一索引 uq_jobs_update_id 擋住競態——若同一 update_id 已被
    （另一條路徑）插入，INSERT 會拋 IntegrityError，我們視為「已存在、跳過」，
    回傳 None。呼叫端須檢查回傳值。
    """
    job_id = f"job-{_next_job_id()}"
    with db() as conn:
        try:
            conn.execute(
                "INSERT INTO jobs(job_id,kind,arg,chat_id,message_id,source,status,created,update_id)"
                " VALUES(?,?,?,?,?,?, 'pending', ?, ?)",
                (job_id, kind, arg, str(chat_id), str(message_id) if message_id is not None else "",
                 source, datetime.now().isoformat(), str(update_id) if update_id is not None else None))
            return job_id
        except sqlite3.IntegrityError:
            # 同 update_id 已存在（競態或重複），跳過以免重複執行
            return None

def _stale_to_pending():
    """把 claim 後逾時未回傳的 job 排回 pending（worker 崩潰兜底）。"""
    if CLAIM_TTL_SEC <= 0:
        return
    cutoff = (time.time() - CLAIM_TTL_SEC)
    with db() as conn:
        conn.execute(
            "UPDATE jobs SET status='pending', claimed_at=NULL"
            " WHERE status='claimed' AND claimed_at < ?",
            (datetime.fromtimestamp(cutoff).isoformat(),))

def _purge_old_done():
    """刪除過舊的 done 記錄（純空間整理）。"""
    if DONE_KEEP_DAYS <= 0:
        return
    cutoff = (datetime.now().timestamp() - DONE_KEEP_DAYS * 86400)
    with db() as conn:
        conn.execute(
            "DELETE FROM jobs WHERE status='done' AND COALESCE(done_at, created) < ?",
            (datetime.fromtimestamp(cutoff).isoformat(),))

def _claim_one():
    """原子取一個 pending job → claimed，回 dict；沒有回 None。"""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status='pending' ORDER BY id ASC LIMIT 1").fetchone()
        if not row:
            return None
        now = datetime.now().isoformat()
        conn.execute(
            "UPDATE jobs SET status='claimed', claimed_at=? WHERE id=? AND status='pending'",
            (now, row["id"]))
        if conn.total_changes == 0:  # 被其他 poller 搶走
            return None
        return {k: row[k] for k in row.keys() if k != "id"}

def job_from_row(row):
    """把 sqlite.Row 轉成 worker 端期望的 job dict。"""
    d = {k: row[k] for k in row.keys()
         if k not in ("id", "status", "claimed_at", "done_at", "update_id")}
    d["job_id"] = row["job_id"]
    return d

def poll_job(timeout):
    """長輪詢：先回收逾時 job，再取一個 pending；取不到就睡到 timeout。"""
    deadline = time.time() + max(1, min(timeout, 55))
    _stale_to_pending()
    while True:
        row = _claim_one()
        if row:
            return job_from_row(row)
        if time.time() >= deadline:
            return None
        time.sleep(min(1.5, max(0.2, deadline - time.time())))

def mark_done(job_id):
    """result 端點：把 job 標記 done 並記時間。"""
    with db() as conn:
        conn.execute(
            "UPDATE jobs SET status='done', done_at=? WHERE job_id=?",
            (datetime.now().isoformat(), job_id))

def pending_count():
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE status='pending'").fetchone()["c"]

# 定期清理線程：回收逾時 claim + 清除過舊 done（防 DB 無限增大）
def _db_maintenance_loop():
    while True:
        try:
            _stale_to_pending()
            _purge_old_done()
        except Exception as e:
            print(f"[DB-MAINT] 維護失敗：{e}", flush=True)
        time.sleep(60)

threading.Thread(target=_db_maintenance_loop, daemon=True, name="db-maint").start()

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
    update_id = update.get("update_id")
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
        # 控制本機 → 入隊列（DB 唯一索引 uq_jobs_update_id 擋競態：
        # 若同 update_id 已被「離線補抓」路徑插入，enqueue_job 會回 None）
        kind, arg = matched
        job_id = enqueue_job(kind, arg, chat_id, message_id, "telegram", update_id=update_id)
        if job_id is None:
            print(f"[WEBHOOK] update {update_id} 已入過隊列（DB 唯一約束擋下），跳過", flush=True)
            return jsonify({"ok": True, "dup": update_id})
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
    job = poll_job(timeout)
    if job is None:
        return jsonify({"jobs": []})
    print(f"[POLL] 交出 {job.get('job_id')} {job.get('kind')!r}", flush=True)
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
    # 標記完成（持久化；讓逾時回收不會重排這筆）
    mark_done(data.get("job_id") or job.get("job_id"))

    if image_b64:
        tg_send_photo(chat_id, image_b64, text[:1000], reply_to=message_id)
    if text:
        tg_send(chat_id, text, reply_to=message_id, parse="markdown")
    print(f"[RESULT] job={data.get('job_id')} 回傳 {len(text)} 字" +
          (" + 圖片" if image_b64 else ""), flush=True)
    return jsonify({"ok": True})


@app.route("/jobs", methods=["GET"])
def jobs_status():
    """運維端點：顯示目前隊列狀態。參數 limit（預設 20）。"""
    if request.headers.get("X-Agent-Key") != AGENT_KEY:
        return jsonify({"error": "bad-key"}), 401
    limit = int(request.args.get("limit", "20"))
    _stale_to_pending()
    with db() as conn:
        counts = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}
        rows = conn.execute(
            "SELECT job_id,kind,arg,status,created,claimed_at,done_at FROM jobs"
            " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return jsonify({
        "db": DB_PATH,
        "counts": counts,
        "pending": pending_count(),
        "recent": [dict(r) for r in rows],
    })


@app.route("/worker/enqueue-test", methods=["POST"])
def worker_enqueue_test():
    """運測端點：向指定 chat 注入 N 筆控制 job（預設 2 筆 shot），
    用來不靠使用者手打、驗證「本機 worker 出向取 job → 執行 → 回傳」全鏈。
    僅 AGENT_KEY 可呼叫；只對已授權 chat 入隊。"""
    if request.headers.get("X-Agent-Key") != AGENT_KEY:
        return jsonify({"error": "bad-key"}), 401
    data = request.get_json(force=True, silent=True) or {}
    chat_id = data.get("chat_id") or ALLOWED_CHAT_ID
    if not chat_id:
        return jsonify({"error": "chat_id required"}), 400
    if not is_authorized(chat_id):
        return jsonify({"error": "chat not authorized"}), 403
    try:
        count = int(data.get("count", 2))
    except (TypeError, ValueError):
        count = 2
    count = max(1, min(count, 10))
    kind = (data.get("kind") or "shot").lower()
    enqueued = []
    for i in range(count):
        jid = enqueue_job(kind, "", chat_id, None, "test-enqueue")
        if jid:
            enqueued.append(jid)
    print(f"[ENQ-TEST] 注入 {len(enqueued)} 筆 {kind} -> chat {chat_id}", flush=True)
    return jsonify({"enqueued": enqueued, "kind": kind, "chat_id": chat_id})


def setup_webhook():
    """啟動時把 Telegram 指向本 service 的 /telegram-webhook。

    drop_pending_updates=false：保留 Render 休眠/重啟期間堆積在 Telegram 的
    update（例如離線時傳的 /shot），避免每次喚起就被主動清掉。
    """
    url = f"{CLOUD_BASE_URL}/telegram-webhook"
    r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                     params={"url": url, "drop_pending_updates": "false"},
                     timeout=30)
    d = r.json()
    print(f"[WEBHOOK] setWebhook -> {url}  ok={d.get('ok')} {d.get('description','')}", flush=True)


def reclaim_offline_updates():
    """喚起/重啟後，把「離線期間堆在 Telegram、未被 webhook 收走」的控制指令
    補抓並入隊列，讓本機 worker 上線後自動補抓執行（免費、跨重啟不遺失）。

    原理：webhook 生效時 getUpdates 會回 409，所以先 deleteWebhook（保留 pending）
    → 用 getUpdates 讀出離線堆積的 update → 控制指令入 SQLite 隊列（update_id 去重）
    → 最後 setWebhook 恢復。setWebhook 放 finally，確保即使補抓中途出錯也能恢復
    webhook，不會卡在「webhook 已刪、收不到新訊息」的死鎖。
    """
    url = f"{CLOUD_BASE_URL}/telegram-webhook"
    reclaimed = 0
    try:
        # 1) 暫時移除 webhook（保留堆積的 pending updates）
        del_d = tg("deleteWebhook", drop_pending_updates=False)
        print(f"[RECLAIM] deleteWebhook ok={del_d.get('ok')}", flush=True)
        # 2) 讀出離線堆積的 update（非阻塞）
        upd = tg("getUpdates", timeout=0, limit=200)
        updates = upd.get("result") or []
        for u in updates:
            mid = u.get("update_id")
            msg = u.get("message") or {}
            text = (msg.get("text") or "").strip()
            chat_id = (msg.get("chat") or {}).get("id")
            message_id = msg.get("message_id")
            if not text or not is_authorized(chat_id):
                continue
            matched = _classify_kind(text)
            if not matched:
                continue                      # 非控制指令不補抓（離線問答由 Agnes 現場答）
            kind, arg = matched
            jid = enqueue_job(kind, arg, chat_id, message_id, "reclaim", update_id=mid)
            if jid is None:
                continue                      # 同 update_id 已入過隊列（DB 唯一約束擋下），跳過
            reclaimed += 1
            print(f"[RECLAIM] 補抓 {jid} ← update {mid} {kind!r} {arg!r} -> chat {chat_id}", flush=True)
        print(f"[RECLAIM] 補抓完成，新增 {reclaimed} 筆離線控制指令", flush=True)
    except Exception as e:
        print(f"[RECLAIM] 補抓過程出錯（不影響恢復 webhook）：{e}", flush=True)
    finally:
        # 3) 一定要恢復 webhook，否則收不到後續新訊息
        try:
            r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                            params={"url": url, "drop_pending_updates": "false"},
                            timeout=30)
            d = r.json()
            print(f"[RECLAIM] setWebhook 恢復 ok={d.get('ok')} {d.get('description','')}", flush=True)
        except Exception as e:
            print(f"[RECLAIM] ⚠️ setWebhook 恢復失敗，3 秒後再試：{e}", flush=True)
            threading.Timer(3.0, setup_webhook).start()
    return reclaimed


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
    print(f"💾 持久化隊列 SQLite：{DB_PATH}  (claim 逾時={CLAIM_TTL_SEC}s, done 保留={DONE_KEEP_DAYS}天)", flush=True)
    print(f"   目前待處理控制指令：{pending_count()} 筆", flush=True)

    # 先立即恢復 webhook（讓服務先能收新訊息）
    setup_webhook()

    # 再背景補抓「離線期間堆在 Telegram、未被收走」的控制指令（跨休眠/重啟不遺失）。
    # 放背景執行：reclaim 會短暫 deleteWebhook→getUpdates→setWebhook，
    # 避免阻擋 Render 健康檢查；它結束時會自行恢復 webhook。
    threading.Thread(target=reclaim_offline_updates, name="reclaim", daemon=True).start()

    print(f"🌐 啟動 HTTP 伺服器 http://0.0.0.0:{PORT}", flush=True)
    app.run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    main()

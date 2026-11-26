# 🤖 Telegram + Agnes AI 全能私人助理 Bot

**自動回覆 Telegram 訊息，支援 Agnes AI 模型（agnes-2.5-flash）即時回覆。**

---

## 功能

| 指令 | 行為 |
|------|------|
| `發布影片` | 掃描本機最新 `.mp4`，寫入待發布隊列 |
| 任意文字問題 | 調用 Agnes AI API 回覆完整答案 |

---

## 快速開始

### 1. 安裝依賴
```bash
pip install -r requirements.txt
```

### 2. 設定環境變數
```bash
export TELEGRAM_BOT_TOKEN="8778107946:AAHSs1D43_Qmlr-s6UV5Oia9jMFO_-DNqiY"
export AGNES_API_KEY="sk-你的Agnes_API_Key"   # 可選，預設會從 models.json 讀取
```

### 3. 啟動 Bot
```bash
python bot.py
```

---

## 部署到 Render

1. 將此倉庫推送到 GitHub
2. 前往 [render.com](https://render.com) → **New → Web Service**
3. 選擇本仓库，填入以下環境變數：
   - `TELEGRAM_BOT_TOKEN` = `8778107946:AAHSs1D43_Qmlr-s6UV5Oia9jMFO_-DNqiY`
   - `AGNES_API_KEY` = `你的 Agnes API Key`（可選）
4. 點擊 **Create Web Service**，Render 自動部署完成

---

## 檔案說明

| 檔案 | 用途 |
|------|------|
| `bot.py` | Bot 主程式（輪詢 + Agnes AI 回覆） |
| `requirements.txt` | Python 依賴清單 |
| `Procfile` | Render / Heroku 啟動指令 |
| `render.yaml` | Render 部署配置（可選） |
| `.gitignore` | 排除 .env、__pycache__ 等敏感/生成檔案 |

---

## 注意事項

- `bot.py` 中的影片路徑使用 `$HOME/Downloads`，若遷移到其他系統請修改 `VIDEO_DIR` 環境變數。
- Agnes AI API 使用 OpenAI 相容協議，無需額外設定搜尋工具。
- 影片實際上傳到抖音需額外接入抖音開放平台 API，目前僅做「標記待發布」。
- 若未設定 `AGNES_API_KEY`，Bot 會嘗試從 `~/.workbuddy-ai/models.json` 讀取預設 Key。

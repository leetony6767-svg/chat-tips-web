# 🤖 Telegram + Gemini 全能助理 Bot

**自動回覆 Telegram 訊息，支援上網搜尋（Gemini API）與影片發布。**

---

## 功能

| 指令 | 行為 |
|------|------|
| `發布影片` | 掃描本機最新 `.mp4`，寫入待發布隊列 |
| 任意文字問題 | 調用 Gemini API 上網搜尋並回覆完整答案 |

---

## 快速開始

### 1. 安裝依賴
```bash
pip install -r requirements.txt
```

### 2. 設定環境變數
```bash
export TELEGRAM_BOT_TOKEN="8778107946:AAGv4kbl23X_9l77xcEGDKUqyVcyw-rUh7Y"
export GEMINI_API_KEY="AIzaSy..."   # 你的 Gemini API Key
```

### 3. 啟動 Bot
```bash
python bot.py
```

---

## 部署到 Render

1. 將此倉庫推送到 GitHub（見下方指引）
2. 前往 [render.com](https://render.com) → **New → Web Service**
3. 選擇本仓库，填入以下環境變數：
   - `TELEGRAM_BOT_TOKEN` = `8778107946:AAGv4kbl23X_9l77xcEGDKUqyVcyw-rUh7Y`
   - `GEMINI_API_KEY` = `你的 Gemini API Key`
4. 點擊 **Create Web Service**，Render 自動部署完成

---

## 檔案說明

| 檔案 | 用途 |
|------|------|
| `bot.py` | Bot 主程式（輪詢 + Gemini 回覆） |
| `requirements.txt` | Python 依賴清單 |
| `Procfile` | Render / Heroku 啟動指令 |
| `render.yaml` | Render 部署配置（可選） |
| `.gitignore` | 排除 .env、__pycache__ 等敏感/生成檔案 |

---

## 注意事項

- `bot.py` 中的影片路徑硬編為 Windows 路徑（`/c/Users/master/...`），若遷移到其他系統請修改 `VIDEO_SEARCH_PATHS`。
- Gemini API 搜尋功能需要 `google-genai>=1.21.0`，在 Render 免費方案上正常運行（cold start 約 15~30 秒）。
- 影片實際上傳到抖音需額外接入抖音开放平台 API，目前僅做「標記待發布」。

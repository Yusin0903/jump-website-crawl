# Jump Website Crawl — JUMP SHOP 補貨通知 Discord Bot

監控 [JUMP SHOP Online](https://jumpshop-online.com/) 指定作品的商品狀態,當補貨、新品上架、售罄時透過 Discord 即時通知。

## 功能

- 🔔 **補貨通知** — 商品從售罄轉回有貨時通知
- ✨ **新品上架** — 偵測到新商品頁面且可購買時通知
- 👀 **預告頁面** — 偵測到新商品頁面但尚未開賣時通知
- ⚪ **售罄提醒** — 商品從有貨轉為售罄時通知
- 📊 **隨時查詢** — 透過 slash command 查詢全部或特定作品的庫存狀態

## 監控作品

預設監控以下作品 (在 `main.py` 的 `TARGET_SERIES` 設定):

- HUNTER×HUNTER
- SAKAMOTO DAYS
- チェンソーマン (鏈鋸人)
- 僕のヒーローアカデミア (我的英雄學院)
- 呪術廻戦 (咒術迴戰)
- 鬼滅の刃

## Slash Commands

| 指令 | 說明 |
|---|---|
| `/all` | 顯示所有監控商品的庫存總表,依作品分組 |
| `/series <name>` | 查詢特定作品的所有商品狀態 (支援自動補完) |
| `/monitor` | 在當前頻道開啟自動補貨提醒 |
| `/stop` | 在當前頻道停止自動補貨提醒 |

## 技術棧

- **Python 3.10+** (Docker 用 3.12-slim)
- **discord.py** — Discord bot 框架
- **aiohttp** — 非阻塞 HTTP client (避免 Discord heartbeat 被阻塞)
- **uv** — 套件管理
- **Zeabur** — 部署平台 (任何支援 Dockerfile 的 PaaS 皆可)

## 本地開發

### 1. 安裝相依套件

```bash
uv sync
```

### 2. 設定環境變數

建立 `.env` 檔:

```env
DISCORD_TOKEN=your_discord_bot_token_here
```

到 [Discord Developer Portal](https://discord.com/developers/applications) 建立 Bot 並取得 Token。
記得開啟以下 Intents:
- `MESSAGE CONTENT INTENT`
- `SERVER MEMBERS INTENT` (依需求)

### 3. 邀請 Bot 到伺服器

啟動後 console 會印出 invite link,複製去瀏覽器邀請。
所需權限:`Send Messages`, `Embed Links`, `Use Slash Commands`。

### 4. 啟動

```bash
uv run python bot.py
```

或單純跑爬蟲 (純 console 模式):

```bash
uv run python main.py
```

## Docker 部署

```bash
docker build -t jump-crawl .
docker run -e DISCORD_TOKEN=your_token jump-crawl
```

### Apple Silicon (M1/M2/M3) 推到 amd64 雲端

```bash
docker build --platform linux/amd64 -t jump-crawl .
```

## 專案結構

```
.
├── bot.py            # Discord bot 主程式 (事件、指令、監控任務)
├── main.py           # 爬蟲核心 (fetch_products, monitor_check)
├── Dockerfile        # 容器映像定義
├── pyproject.toml    # 套件相依
├── uv.lock           # 鎖定相依版本
└── README.md
```

## 監控邏輯

- 每 **20 秒**輪詢一次 Shopify products JSON API (`/collections/all/products.json`)
- 共用單一 `aiohttp.ClientSession` 做連線復用,降低延遲
- 將最新狀態與記憶體中的 `current_stock_status` 比對,產生 4 種變化事件:
  - `restock` — 沒貨 → 有貨
  - `soldout` — 有貨 → 沒貨
  - `new_arrival_buyable` — 新 ID 且有貨
  - `new_arrival_coming_soon` — 新 ID 但無貨 (預告頁)

## 自訂監控清單

編輯 `main.py`:

```python
TARGET_SERIES = [
    "HUNTER×HUNTER",
    "你想加的作品名",
]
```

注意:作品名稱需與 JUMP SHOP 商品標題中 `『 』` 內的字串完全一致。

## 注意事項

- Bot 重啟後 `current_stock_status` 會重新初始化,第一次掃描不會發通知 (避免轟炸)
- `monitoring_channels` 也是記憶體狀態,重啟後要重新 `/monitor`
- 若需狀態持久化,可考慮接 SQLite 或 Redis

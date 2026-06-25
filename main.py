import asyncio
import random
import aiohttp

# JUMP SHOP 所有商品的 JSON API
URL = "https://jumpshop-online.com/collections/all/products.json?limit=250"

# --- 速率限制 (HTTP 429) 退避設定 ---
# Shopify 對未授權的 products.json 端點有速率限制，過於頻繁請求會回傳 429。
# 收到 429 時必須退避 (back off)，否則持續以固定間隔重試只會一直被封鎖。
MAX_RETRIES = 4          # 單頁遇到 429 時的最大重試次數
RETRY_BACKOFF_BASE = 5   # 指數退避基數 (秒)
RETRY_BACKOFF_MAX = 120  # 單次退避上限 (秒)

# 單獨執行 main.py 時預設抓取的作品清單
STANDALONE_TARGET_SERIES = [
    "HUNTER×HUNTER",
    "SAKAMOTO DAYS",
    "チェンソーマン",
    "僕のヒーローアカデミア",
    "呪術廻戦",
    "鬼滅の刃"
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
}

# 用來記錄上次庫存狀態的字典 {商品ID: 是否可購買}
last_stock_status = {}


def is_target_product(title, target_list=None):
    """檢查商品標題是否包含指定的作品名稱"""
    targets = target_list if target_list is not None else STANDALONE_TARGET_SERIES
    for series in targets:
        if series in title:
            return True
    return False


def _retry_after_seconds(response, attempt):
    """計算 429 後應等待的秒數：優先採用伺服器的 Retry-After，否則用指數退避。"""
    header = response.headers.get('Retry-After')
    if header:
        try:
            return float(header)
        except ValueError:
            pass  # Retry-After 也可能是 HTTP-date 格式，無法解析時退回指數退避
    backoff = min(RETRY_BACKOFF_BASE * (2 ** attempt), RETRY_BACKOFF_MAX)
    return backoff + random.uniform(0, 1)  # 加上抖動避免請求同步化


async def _fetch_page(session: aiohttp.ClientSession, page: int):
    """請求單一頁面，內建 429 指數退避重試。

    回傳商品 list；若連線失敗或重試耗盡則回傳 None。
    """
    paginated_url = f"{URL}&page={page}"
    for attempt in range(MAX_RETRIES + 1):
        try:
            async with session.get(paginated_url) as response:
                if response.status == 429:
                    if attempt >= MAX_RETRIES:
                        print(f"無法連線 (HTTP 429)：已達最大重試次數 ({MAX_RETRIES})，本輪放棄")
                        return None
                    wait = _retry_after_seconds(response, attempt)
                    print(f"被限流 (HTTP 429)，{wait:.0f} 秒後重試 ({attempt + 1}/{MAX_RETRIES})")
                    await asyncio.sleep(wait)
                    continue
                if response.status != 200:
                    print(f"無法連線 (HTTP {response.status})")
                    return None
                data = await response.json()
                return data.get('products', [])
        except asyncio.TimeoutError:
            print(f"第 {page} 頁請求逾時")
            return None
        except aiohttp.ClientError as e:
            print(f"發生連線錯誤: {e}")
            return None
    return None


async def fetch_products(session: aiohttp.ClientSession | None = None):
    """向 Shopify 請求所有商品資料 (包含分頁)"""
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15))

    all_products = []
    page = 1

    try:
        while True:
            products = await _fetch_page(session, page)
            if not products:  # None (連線失敗/被限流) 或空頁皆視為結束
                break

            # 改為回傳所有商品，不過濾
            all_products.extend(products)

            # 如果單頁商品少於 limit (250)，代表是最後一頁
            if len(products) < 250:
                break

            page += 1
            await asyncio.sleep(0.5)  # 禮貌性延遲
    finally:
        if own_session:
            await session.close()

    return all_products


async def monitor_check(current_stock_status, session: aiohttp.ClientSession | None = None, products=None):
    """第二階段：持續對比狀態變化，返回變化列表

    如果傳入 products,就直接用;否則自己 fetch。
    """
    if products is None:
        products = await fetch_products(session=session)
    changes = []
    new_stock_status = current_stock_status.copy()

    for p in products:
        p_id = p['id']
        p_title = p['title']
        is_available = any(v['available'] for v in p['variants'])
        product_url = f"https://jumpshop-online.com/products/{p['handle']}"

        if p_id in current_stock_status:
            # --- 舊商品邏輯 (監控庫存變化) ---
            old_status = current_stock_status[p_id]

            # 狀態：沒貨 -> 有貨 (補貨通知)
            if not old_status and is_available:
                changes.append({
                    "type": "restock",
                    "title": p_title,
                    "url": product_url
                })
            # 狀態：有貨 -> 沒貨 (售罄通知)
            elif old_status and not is_available:
                changes.append({
                    "type": "soldout",
                    "title": p_title
                })
        else:
            # --- 新商品邏輯 (監控頁面上架) ---
            if is_available:
                changes.append({
                    "type": "new_arrival_buyable",
                    "title": p_title,
                    "url": product_url
                })
            else:
                changes.append({
                    "type": "new_arrival_coming_soon",
                    "title": p_title,
                    "url": product_url
                })

        new_stock_status[p_id] = is_available

    return changes, new_stock_status


async def initial_scan():
    """第一階段：列出所有商品現狀"""
    global last_stock_status
    print("=== [第一階段] 正在初始化：建立商品資料庫 ===\n")

    products = await fetch_products()
    if not products:
        print("查無商品，請檢查網址或連線。")
        return

    for p in products:
        p_id = p['id']
        p_title = p['title']
        is_available = any(v['available'] for v in p['variants'])
        last_stock_status[p_id] = is_available

        # 為了避免洗頻，單獨執行時只列印目標作品
        if is_target_product(p_title):
            status_tag = "【可購買】" if is_available else "[無庫存]"
            print(f"{status_tag} {p_title}")

    print("\n" + "=" * 50)
    print(f"初始化完成，共監控 {len(products)} 項商品。")
    print("=== [第二階段] 開始進入即時監測模式 ===\n")


async def _standalone_loop():
    global last_stock_status
    await initial_scan()
    async with aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as session:
        while True:
            try:
                changes, last_stock_status = await monitor_check(last_stock_status, session=session)

                for change in changes:
                    # 單獨執行時，過濾出目標作品
                    if not is_target_product(change['title']):
                        continue

                    if change['type'] == 'restock':
                        print(f"🔔 補貨通知！！ (庫存恢復) >>> {change['title']}")
                        print(f"🔗 連結: {change['url']}\n")
                    elif change['type'] == 'new_arrival_buyable':
                        print(f"✨ 新品上架！！ (現貨可買) >>> {change['title']}")
                        print(f"🔗 連結: {change['url']}\n")
                    elif change['type'] == 'new_arrival_coming_soon':
                        print(f"👀 發現新品頁面！！ (尚未開賣) >>> {change['title']}")
                        print(f"🔗 連結: {change['url']}\n")
                    elif change['type'] == 'soldout':
                        print(f"⚪ 剛售罄: {change['title']}")

                await asyncio.sleep(60)  # 降低請求頻率，避免觸發 Shopify 速率限制 (429)
            except Exception as e:
                print(f"發生未預期錯誤: {e}")
                await asyncio.sleep(60)


if __name__ == "__main__":
    try:
        asyncio.run(_standalone_loop())
    except KeyboardInterrupt:
        print("\n監控已停止。")

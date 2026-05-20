import asyncio
import aiohttp

# JUMP SHOP 所有商品的 JSON API
URL = "https://jumpshop-online.com/collections/all/products.json?limit=250"

# 指定抓取的作品清單
TARGET_SERIES = [
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


def is_target_product(title):
    """檢查商品標題是否包含指定的作品名稱"""
    for series in TARGET_SERIES:
        if series in title:
            return True
    return False


async def fetch_products(session: aiohttp.ClientSession | None = None):
    """向 Shopify 請求所有商品資料 (包含分頁與作品過濾)"""
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15))

    all_products = []
    page = 1

    try:
        while True:
            paginated_url = f"{URL}&page={page}"
            try:
                async with session.get(paginated_url) as response:
                    if response.status != 200:
                        print(f"無法連線 (HTTP {response.status})")
                        break
                    data = await response.json()
                    products = data.get('products', [])
            except asyncio.TimeoutError:
                print(f"第 {page} 頁請求逾時")
                break
            except aiohttp.ClientError as e:
                print(f"發生連線錯誤: {e}")
                break

            if not products:
                break

            filtered_products = [p for p in products if is_target_product(p['title'])]
            all_products.extend(filtered_products)

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

                await asyncio.sleep(10)
            except Exception as e:
                print(f"發生未預期錯誤: {e}")
                await asyncio.sleep(60)


if __name__ == "__main__":
    try:
        asyncio.run(_standalone_loop())
    except KeyboardInterrupt:
        print("\n監控已停止。")

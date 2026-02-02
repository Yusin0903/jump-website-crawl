import requests
import time

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

# 用來記錄上次庫存狀態的字典 {商品ID: 是否可購買}
last_stock_status = {}

def is_target_product(title):
    """檢查商品標題是否包含指定的作品名稱"""
    for series in TARGET_SERIES:
        if series in title:
            return True
    return False

def fetch_products():
    """向 Shopify 請求所有商品資料 (包含分頁與作品過濾)"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    all_products = []
    page = 1
    total_fetched = 0
    
    while True:
        try:
            paginated_url = f"{URL}&page={page}"
            response = requests.get(paginated_url, headers=headers, timeout=10)
            if response.status_code == 200:
                products = response.json().get('products', [])
                if not products:
                    break
                
                total_fetched += len(products)
                # 在這裡進行過濾：只保留指定作品的商品
                filtered_products = [p for p in products if is_target_product(p['title'])]
                all_products.extend(filtered_products)
                
                if len(products) < 250:
                    break
                
                page += 1
                time.sleep(0.5)
            else:
                print(f"無法連線 (HTTP {response.status_code})")
                break
        except Exception as e:
            print(f"發生連線錯誤: {e}")
            break
            
    return all_products

def initial_scan():
    """第一階段：列出所有商品現狀"""
    global last_stock_status
    print("=== [第一階段] 正在初始化：掃描所有「再入荷」商品現狀 ===\n")
    
    products = fetch_products()
    if not products:
        print("查無商品，請檢查網址或連線。")
        return

    for p in products:
        p_id = p['id']
        p_title = p['title']
        # 只要有一個款式有貨，available 就會是 True
        is_available = any(v['available'] for v in p['variants'])
        
        # 儲存狀態供後續對比
        last_stock_status[p_id] = is_available
        
        # 顯示狀態
        status_tag = "【可購買】" if is_available else "[売り切れ]"
        print(f"{status_tag} {p_title}")

    print("\n" + "="*50)
    print(f"初始化完成，共監控 {len(products)} 項商品。")
    print("=== [第二階段] 開始進入即時監測模式 (每 60 秒檢查一次) ===\n")

def monitor_check(current_stock_status):
    """第二階段：持續對比狀態變化，返回變化列表"""
    products = fetch_products()
    changes = []
    new_stock_status = current_stock_status.copy()

    for p in products:
        p_id = p['id']
        p_title = p['title']
        is_available = any(v['available'] for v in p['variants'])
        
        # 檢查是否存在於舊紀錄中 (避免 API 突然新增商品)
        if p_id in current_stock_status:
            old_status = current_stock_status[p_id]
            
            # 關鍵邏輯：原本售罄 (False) 變成 有貨 (True)
            if not old_status and is_available:
                changes.append({
                    "type": "restock",
                    "title": p_title,
                    "handle": p['handle'],
                    "url": f"https://jumpshop-online.com/products/{p['handle']}"
                })
            
            # 如果想知道什麼時候賣完
            elif old_status and not is_available:
                changes.append({
                    "type": "soldout",
                    "title": p_title
                })

        # 更新狀態
        new_stock_status[p_id] = is_available
    
    return changes, new_stock_status

if __name__ == "__main__":
    # 執行第一次掃描
    initial_scan()
    
    # 開始無限循環監控
    while True:
        try:
            changes, last_stock_status = monitor_check(last_stock_status)
            for change in changes:
                if change['type'] == 'restock':
                    print(f"🔔 補貨通知！！ >>> {change['title']}")
                    print(f"🔗 連結: {change['url']}\n")
                elif change['type'] == 'soldout':
                    print(f"⚪ 剛售罄: {change['title']}")
            
            # 每一分鐘檢查一次，避免頻繁請求被封鎖 IP
            time.sleep(60) 
        except KeyboardInterrupt:
            print("\n監控已停止。")
            break
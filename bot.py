import discord
from discord.ext import commands, tasks
import os
import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from main import fetch_products, monitor_check, HEADERS

# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
PORT = int(os.getenv('PORT', '8080'))

# Configure intents
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Store monitoring state
monitoring_channels = set()
current_stock_status = {}
cached_series = []
http_session: aiohttp.ClientSession | None = None
health_server_started = False

import json

# 讓路徑支援環境變數設定，方便 Zeabur 掛載持久化硬碟
DATA_DIR = os.getenv('DATA_DIR', '.')
CONFIG_FILE = os.path.join(DATA_DIR, 'bot_config.json')

config = {
    "monitored_series": [
        "HUNTER×HUNTER",
        "SAKAMOTO DAYS",
        "チェンソーマン",
        "僕のヒーローアカデミア",
        "呪術廻戦",
        "鬼滅の刃"
    ],
    "notify_soldout": True,
    "monitoring_channels": []
}
monitored_series = set(config["monitored_series"])

def load_config():
    global config, monitored_series, monitoring_channels
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            config.update(data)
            monitored_series = set(config.get("monitored_series", []))
            monitoring_channels = set(config.get("monitoring_channels", []))
    except (FileNotFoundError, json.JSONDecodeError):
        # 找不到的話試著相容讀取舊的檔案
        try:
            with open('monitored_series.json', 'r', encoding='utf-8') as f:
                old_data = json.load(f)
                config["monitored_series"] = old_data
                monitored_series = set(old_data)
        except:
            pass
        save_config()

def save_config():
    config["monitored_series"] = sorted(list(monitored_series))
    config["monitoring_channels"] = list(monitoring_channels)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

load_config()

def update_series_cache(products):
    """更新作品名稱快取"""
    global cached_series
    new_series = set()
    for p in products:
        new_series.add(extract_series(p['title']))
    cached_series = sorted(list(new_series))
    print(f"Updated series cache: {len(cached_series)} series found.")

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} (ID: {bot.user.id})')
    invite_link = discord.utils.oauth_url(bot.user.id, permissions=discord.Permissions(administrator=True), scopes=("bot", "applications.commands"))
    print(f'Invite link: {invite_link}')

    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession(
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=15),
        )

    # Initialize stock status on startup
    print("Initializing stock status...")
    global current_stock_status
    try:
        products = await fetch_products(session=http_session)
    except Exception as e:
        print(f"Initial fetch failed: {e}")
        products = []
    update_series_cache(products)
    for p in products:
        is_available = any(v['available'] for v in p['variants'])
        current_stock_status[p['id']] = is_available
    print(f"Initialized with {len(current_stock_status)} products.")

    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Error syncing commands: {e}")

    if not monitor_task.is_running():
        monitor_task.start()

    # Start health check HTTP server for Zeabur (and other PaaS) probes
    await start_health_server()


async def health_handler(request):
    return web.Response(text="ok")


async def start_health_server():
    global health_server_started
    if health_server_started:
        return
    app = web.Application()
    app.router.add_get('/', health_handler)
    app.router.add_get('/health', health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    health_server_started = True
    print(f"Health check server listening on 0.0.0.0:{PORT}")


@bot.event
async def on_close():
    global http_session
    if http_session and not http_session.closed:
        await http_session.close()

@bot.command()
@commands.is_owner()
async def sync(ctx):
    """Sync slash commands manually"""
    try:
        synced = await bot.tree.sync()
        await ctx.send(f"Synced {len(synced)} commands.")
    except Exception as e:
        await ctx.send(f"Failed to sync: {e}")

import re

def extract_series(title):
    """提取 『 』 括號中的作品名稱"""
    match = re.search(r'『(.*?)』', title)
    return match.group(1) if match else "其他"

async def send_long_message(interaction, title, content_list):
    """將長訊息分段發送的輔助函數"""
    header = f"{title}\n"
    current_message = header
    
    for item in content_list:
        # 如果單行加上去會超過限制
        if len(current_message) + len(item) + 1 > 1900:
            if interaction.response.is_done():
                await interaction.followup.send(current_message)
            else:
                await interaction.response.send_message(current_message)
            current_message = item + "\n"
        else:
            current_message += item + "\n"
    
    # 發送剩餘的部分
    if current_message != header:
        if interaction.response.is_done():
            await interaction.followup.send(current_message)
        else:
            await interaction.response.send_message(current_message)

@bot.tree.command(name="all", description="顯示所有商品的庫存狀態 (包含有貨與售罄)")
async def list_all(interaction: discord.Interaction):
    """顯示所有商品狀態"""
    await interaction.response.defer()
    products = await fetch_products(session=http_session)
    if not products:
        await interaction.followup.send("無法獲取商品資料。")
        return

    categories = {}
    for p in products:
        series = extract_series(p['title'])
        if series not in categories:
            categories[series] = []
        is_available = any(v['available'] for v in p['variants'])
        categories[series].append((p['title'], is_available))
    
    content_list = []
    for series, items in sorted(categories.items()):
        content_list.append(f"\n🔹 **{series}**")
        for title, avail in items:
            status = "✅" if avail else "❌"
            display_name = title.replace(f"『{series}』", "").strip()
            content_list.append(f" {status} {display_name}")
    
    await send_long_message(interaction, "📊 **所有商品庫存總表：**", content_list)

@bot.tree.command(name="series", description="查詢特定作品的所有商品狀態")
@discord.app_commands.describe(name="作品名稱 (例如：ONE PIECE)")
async def series_stock(interaction: discord.Interaction, name: str):
    """顯示特定作品的所有商品（包含預約/售罄）"""
    await interaction.response.defer()
    products = await fetch_products(session=http_session)
    
    found_items = []
    for p in products:
        if name.lower() in p['title'].lower():
            is_available = any(v['available'] for v in p['variants'])
            status = "✅" if is_available else "❌"
            found_items.append(f"{status} {p['title']}")
            
    if not found_items:
        await interaction.followup.send(f"找不到關於 **{name}** 的商品。")
        return

    await send_long_message(interaction, f"📚 **{name}** 的所有商品現況：", found_items)

# 自動補完功能 (Autocomplete)
@series_stock.autocomplete('name')
async def series_autocomplete(interaction: discord.Interaction, current: str):
    global cached_series
    return [
        discord.app_commands.Choice(name=s, value=s)
        for s in cached_series if current.lower() in s.lower()
    ][:25]

@bot.tree.command(name="add_series", description="新增要自動補貨提醒的作品")
@discord.app_commands.describe(name="作品名稱")
@discord.app_commands.autocomplete(name=series_autocomplete)
async def add_series_cmd(interaction: discord.Interaction, name: str):
    await interaction.response.defer()
    monitored_series.add(name)
    save_config()
    await interaction.followup.send(f"✅ 已新增追蹤作品：**{name}**")

async def monitored_autocomplete(interaction: discord.Interaction, current: str):
    return [
        discord.app_commands.Choice(name=s, value=s)
        for s in sorted(monitored_series) if current.lower() in s.lower()
    ][:25]

@bot.tree.command(name="remove_series", description="移除不再需要自動補貨提醒的作品")
@discord.app_commands.describe(name="作品名稱")
@discord.app_commands.autocomplete(name=monitored_autocomplete)
async def remove_series_cmd(interaction: discord.Interaction, name: str):
    await interaction.response.defer()
    if name in monitored_series:
        monitored_series.remove(name)
        save_config()
        await interaction.followup.send(f"❌ 已取消追蹤作品：**{name}**")
    else:
        await interaction.followup.send(f"⚠️ 目前沒有追蹤：**{name}**")

@bot.tree.command(name="list_series", description="顯示目前自動補貨提醒追蹤中的作品名單")
async def list_series_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    if not monitored_series:
        await interaction.followup.send("目前沒有追蹤任何作品。")
        return
    series_list = "\n".join([f"- {s}" for s in sorted(monitored_series)])
    await interaction.followup.send(f"📋 **目前追蹤的作品：**\n{series_list}")

@bot.tree.command(name="toggle_soldout", description="開啟/關閉售罄通知")
@discord.app_commands.describe(enable="是否開啟售罄通知？")
async def toggle_soldout_cmd(interaction: discord.Interaction, enable: bool):
    await interaction.response.defer()
    config["notify_soldout"] = enable
    save_config()
    status = "開啟" if enable else "關閉"
    await interaction.followup.send(f"✅ 已**{status}**售罄通知。")

@bot.tree.command(name="help", description="顯示機器人功能與使用步驟教學")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 Jump Shop 庫存監控機器人 - 使用教學",
        description="這是一個會自動監控 Jump Shop 商品庫存的機器人，以下是使用方式：",
        color=discord.Color.blue()
    )
    
    embed.add_field(name="📍 1. 開啟/關閉頻道通知", 
                    value="使用 `/monitor` 可以在當前頻道開啟通知。\n使用 `/stop` 可以停止此頻道的通知。", 
                    inline=False)
    
    embed.add_field(name="📚 2. 管理追蹤的作品 (支援自動完成)", 
                    value="使用 `/add_series` 可以新增你想追蹤的作品。\n"
                          "使用 `/remove_series` 可以移除不想追蹤的作品。\n"
                          "使用 `/list_series` 檢視目前所有追蹤中的清單。", 
                    inline=False)
    
    embed.add_field(name="⚙️ 3. 其他設定", 
                    value="使用 `/toggle_soldout` 設定是否要接收「售罄(無庫存)」的推播通知。", 
                    inline=False)

    embed.add_field(name="🔍 4. 查詢商品狀態", 
                    value="使用 `/series` 查詢某個作品目前的全部商品與庫存狀態。\n"
                          "使用 `/all` 顯示所有抓取到的商品庫存總表（訊息較長）。", 
                    inline=False)
    
    embed.set_footer(text="提示：使用 / 指令時，Discord 會跳出選項，直接點選即可！")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="monitor", description="在此頻道開啟自動補貨提醒")
async def start_monitor(interaction: discord.Interaction):
    """在此頻道開啟自動補貨提醒"""
    await interaction.response.defer()
    monitoring_channels.add(interaction.channel_id)
    save_config()
    await interaction.followup.send(f"✅ 已在此頻道開啟自動補貨提醒！當商品狀態變更時會通知大家。")

@bot.tree.command(name="stop", description="在此頻道停止自動補貨提醒")
async def stop_monitor(interaction: discord.Interaction):
    """在此頻道停止自動補貨提醒"""
    await interaction.response.defer()
    if interaction.channel_id in monitoring_channels:
        monitoring_channels.remove(interaction.channel_id)
        save_config()
        await interaction.followup.send("❌ 已停止此頻道的自動補貨提醒。")
    else:
        await interaction.followup.send("此頻道本來就沒有開啟提醒喔。")

@bot.event
async def on_command_error(ctx, error):
    print(f"Error executing command {ctx.command}: {error}")
    await ctx.send(f"An error occurred: {error}")

@tasks.loop(seconds=20)
async def monitor_task():
    global current_stock_status
    if not monitoring_channels:
        return

    try:
        # 獲取最新產品並更新快取 (一次請求,共用給 monitor_check)
        products = await fetch_products(session=http_session)
        if not products:
            return
        update_series_cache(products)

        changes, new_status = await monitor_check(current_stock_status, products=products)
        current_stock_status = new_status
        
        # 過濾出我們感興趣的變更
        filtered_changes = []
        for change in changes:
            if any(s in change['title'] for s in monitored_series):
                filtered_changes.append(change)

        if filtered_changes:
            for channel_id in monitoring_channels:
                channel = bot.get_channel(channel_id)
                if channel:
                    for change in filtered_changes:
                        if change['type'] == 'restock':
                            embed = discord.Embed(
                                title="🔔 補貨通知！",
                                description=f"**{change['title']}** 現在可以購買了！",
                                url=change['url'],
                                color=0x00ff00
                            )
                            await channel.send(embed=embed)
                        elif change['type'] == 'soldout':
                            if config.get("notify_soldout", True):
                                await channel.send(f"⚪ 剛售罄: **{change['title']}**")
                        elif change['type'] == 'new_arrival_buyable':
                            embed = discord.Embed(
                                title="✨ 新品上架！(現貨可買)",
                                description=f"**{change['title']}** 上架並可以購買了！",
                                url=change['url'],
                                color=0x00ffff # Cyan
                            )
                            await channel.send(embed=embed)
                        elif change['type'] == 'new_arrival_coming_soon':
                            embed = discord.Embed(
                                title="👀 發現新品頁面！(尚未開賣)",
                                description=f"**{change['title']}** 頁面已建立，但目前無庫存。\n可能即將開賣，請密切關注！",
                                url=change['url'],
                                color=0x808080 # Grey
                            )
                            await channel.send(embed=embed)
    except Exception as e:
        print(f"Error in monitor task: {e}")

@monitor_task.before_loop
async def before_monitor():
    await bot.wait_until_ready()

if __name__ == "__main__":
    if not TOKEN:
        print("Error: DISCORD_TOKEN not found in environment variables.")
    else:
        bot.run(TOKEN)

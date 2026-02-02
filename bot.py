import discord
from discord.ext import commands, tasks
import os
import asyncio
from dotenv import load_dotenv
from main import fetch_products, monitor_check, initial_scan, last_stock_status

# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# Configure intents
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Store monitoring state
monitoring_channels = set()
current_stock_status = {}
cached_series = []

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
    # Initialize stock status on startup
    print("Initializing stock status...")
    global current_stock_status
    products = fetch_products()
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
    products = fetch_products()
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
    products = fetch_products()
    
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

@bot.tree.command(name="monitor", description="在此頻道開啟自動補貨提醒")
async def start_monitor(interaction: discord.Interaction):
    """Start monitoring in this channel"""
    monitoring_channels.add(interaction.channel_id)
    await interaction.response.send_message(f"✅ 已在此頻道開啟自動補貨提醒！當商品狀態變更時會通知大家。")

@bot.tree.command(name="stop", description="在此頻道停止自動補貨提醒")
async def stop_monitor(interaction: discord.Interaction):
    """Stop monitoring in this channel"""
    if interaction.channel_id in monitoring_channels:
        monitoring_channels.remove(interaction.channel_id)
        await interaction.response.send_message("❌ 已停止此頻道的自動補貨提醒。")
    else:
        await interaction.response.send_message("此頻道本來就沒有開啟提醒喔。")

@bot.event
async def on_command_error(ctx, error):
    print(f"Error executing command {ctx.command}: {error}")
    await ctx.send(f"An error occurred: {error}")

@tasks.loop(seconds=60)
async def monitor_task():
    global current_stock_status
    if not monitoring_channels:
        return

    try:
        # 獲取最新產品並更新快取 (確保新系列會出現在選單中)
        products = fetch_products()
        update_series_cache(products)
        
        # 這裡是原本的 monitor_check 邏輯
        # 為了效能，我們可以稍微優化 monitor_check 不要重複 fetch，但維持現狀較安全
        changes, new_status = monitor_check(current_stock_status)
        current_stock_status = new_status
        
        if changes:
            for channel_id in monitoring_channels:
                channel = bot.get_channel(channel_id)
                if channel:
                    for change in changes:
                        if change['type'] == 'restock':
                            embed = discord.Embed(
                                title="🔔 補貨通知！",
                                description=f"**{change['title']}** 現在可以購買了！",
                                url=change['url'],
                                color=0x00ff00
                            )
                            await channel.send(embed=embed)
                        elif change['type'] == 'soldout':
                            await channel.send(f"⚪ 剛售罄: **{change['title']}**")
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

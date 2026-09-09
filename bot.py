import discord
from discord.ext import commands, tasks
import os
import sys
import logging
import aiohttp
from aiohttp import web
from dotenv import load_dotenv
import main
from main import fetch_products, monitor_check, HEADERS, REQUEST_INTERVAL

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
rate_limit_notified = False  # 是否已對頻道發出「被限流」通知 (避免重複洗頻)
dashboard_registered = False  # 是否已註冊持久化的 dashboard 按鈕 view

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
    
    dir_name = os.path.dirname(CONFIG_FILE)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
        
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
    # 印出行程 PID 與目前設定：若同時有兩個實例在跑，PID 會不同，一眼看穿重複部署
    print(f'[startup] pid={os.getpid()} notify_soldout={config.get("notify_soldout")} '
          f'monitoring_channels={sorted(monitoring_channels)}')
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

    # 註冊持久化的 dashboard 按鈕 (讓重開機後舊訊息的按鈕仍可點)
    global dashboard_registered
    if not dashboard_registered:
        bot.add_view(DashboardView())
        dashboard_registered = True

    # Start health check HTTP server for Zeabur (and other PaaS) probes
    await start_health_server()


@bot.event
async def on_app_command_completion(interaction: discord.Interaction, command):
    """記錄每一個成功執行的 slash 指令 (誰、在哪個頻道、執行什麼)。"""
    user = interaction.user
    print(f"[cmd] pid={os.getpid()} /{command.qualified_name} "
          f"by {user} ({user.id}) in channel {interaction.channel_id}")


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
    print(f"[toggle_soldout] pid={os.getpid()} notify_soldout -> {enable}")
    status = "開啟" if enable else "關閉"
    await interaction.followup.send(f"✅ 已**{status}**售罄通知。")

@bot.tree.command(name="config", description="顯示目前所有設定狀態 (除錯用)")
async def show_config_cmd(interaction: discord.Interaction):
    """把記憶體中的設定 + 磁碟上的設定檔一起秀出來，方便確認 config 有沒有正確讀寫。"""
    await interaction.response.defer(ephemeral=True)

    # 記憶體中目前生效的設定
    mem = (
        f"notify_soldout = {config.get('notify_soldout')}\n"
        f"追蹤作品 ({len(monitored_series)}): {', '.join(sorted(monitored_series)) or '(無)'}\n"
        f"監控頻道 ({len(monitoring_channels)}): {sorted(monitoring_channels) or '(無)'}"
    )

    # 磁碟上的設定檔內容 (直接重讀，不是記憶體)
    exists = os.path.exists(CONFIG_FILE)
    if exists:
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                disk = f.read()
        except Exception as e:
            disk = f"(讀取失敗: {e})"
    else:
        disk = "(檔案不存在！很可能沒掛 Volume 或 DATA_DIR 沒對，設定不會被保存)"
    if len(disk) > 1400:
        disk = disk[:1400] + "\n...(已截斷)"

    msg = (
        f"🔧 **目前設定狀態**\n"
        f"pid: `{os.getpid()}`　DATA_DIR: `{DATA_DIR}`\n"
        f"設定檔: `{CONFIG_FILE}`　(存在: {exists})\n\n"
        f"**[記憶體中 — 目前生效的]**\n```\n{mem}\n```\n"
        f"**[磁碟檔案內容]**\n```json\n{disk}\n```"
    )
    await interaction.followup.send(msg)

def build_dashboard_embed(channel_id):
    """組出 dashboard 面板的 embed；顏色與狀態列會依目前狀態即時變化。"""
    soldout_on = config.get("notify_soldout", True)
    this_channel_on = channel_id in monitoring_channels
    rate_limited = main.is_rate_limited()

    if rate_limited:
        color, status = 0xF1C40F, "🟡 目前被網站限流，暫停抓取中"
    elif this_channel_on:
        color, status = 0x2ECC71, "🟢 監控運作中"
    else:
        color, status = 0x95A5A6, "⚪ 本頻道尚未開啟通知"

    embed = discord.Embed(
        title="🎛️　Jump Shop 監控面板",
        description=f"**{status}**\n用下方按鈕直接操作，不用輸入指令。",
        color=color,
    )
    # 三個狀態小卡 (用 code 樣式做成 chip)
    embed.add_field(name="🔔 售罄通知",
                    value=f"**`{'開啟' if soldout_on else '關閉'}`**", inline=True)
    embed.add_field(name="📡 本頻道",
                    value=f"**`{'監控中' if this_channel_on else '未開啟'}`**", inline=True)
    embed.add_field(name="⏱️ 檢查頻率",
                    value=f"**`{REQUEST_INTERVAL}s`**", inline=True)

    if monitored_series:
        s = "\n".join(f"　• {x}" for x in sorted(monitored_series))
        if len(s) > 1000:
            s = s[:1000] + "\n　…"
    else:
        s = "（尚未追蹤任何作品）"
    embed.add_field(name=f"📚 追蹤中的作品　({len(monitored_series)})", value=s, inline=False)

    embed.set_footer(text="補貨・新品一律通知　｜　售罄通知可用下方按鈕開關")
    embed.timestamp = discord.utils.utcnow()
    return embed


class DashboardView(discord.ui.View):
    """可點擊操作的監控面板。

    - timeout=None + 固定 custom_id → 重開機後舊訊息的按鈕仍可用 (持久化)。
    - 按鈕的顏色/文字會依目前狀態變化 (綠=開、灰/紅=關)，提供即時視覺回饋。
    - 每次操作都重建一份反映最新狀態的 view，達成「點一下就更新」的體驗。
    """

    def __init__(self, channel_id=None):
        super().__init__(timeout=None)
        soldout_on = config.get("notify_soldout", True)
        monitor_on = channel_id in monitoring_channels if channel_id is not None else False

        b_soldout = discord.ui.Button(
            label="售罄通知：開啟" if soldout_on else "售罄通知：關閉",
            emoji="🔔" if soldout_on else "🔕",
            style=discord.ButtonStyle.success if soldout_on else discord.ButtonStyle.secondary,
            custom_id="dash:toggle_soldout", row=0,
        )
        b_soldout.callback = self._toggle_soldout

        b_monitor = discord.ui.Button(
            label="本頻道通知：監控中" if monitor_on else "本頻道通知：未開啟",
            emoji="📡" if monitor_on else "📴",
            style=discord.ButtonStyle.success if monitor_on else discord.ButtonStyle.danger,
            custom_id="dash:toggle_monitor", row=0,
        )
        b_monitor.callback = self._toggle_monitor

        b_refresh = discord.ui.Button(
            label="重新整理", emoji="🔄",
            style=discord.ButtonStyle.secondary, custom_id="dash:refresh", row=1,
        )
        b_refresh.callback = self._refresh

        self.add_item(b_soldout)
        self.add_item(b_monitor)
        self.add_item(b_refresh)

    async def _rerender(self, interaction: discord.Interaction):
        # 用反映最新狀態的 embed + view 就地更新訊息
        await interaction.response.edit_message(
            embed=build_dashboard_embed(interaction.channel_id),
            view=DashboardView(interaction.channel_id),
        )

    async def _toggle_soldout(self, interaction: discord.Interaction):
        config["notify_soldout"] = not config.get("notify_soldout", True)
        save_config()
        print(f"[dashboard] pid={os.getpid()} notify_soldout -> {config['notify_soldout']} by {interaction.user}")
        await self._rerender(interaction)

    async def _toggle_monitor(self, interaction: discord.Interaction):
        cid = interaction.channel_id
        if cid in monitoring_channels:
            monitoring_channels.discard(cid)
        else:
            monitoring_channels.add(cid)
        save_config()
        print(f"[dashboard] pid={os.getpid()} monitor[{cid}] -> {cid in monitoring_channels} by {interaction.user}")
        await self._rerender(interaction)

    async def _refresh(self, interaction: discord.Interaction):
        await self._rerender(interaction)


@bot.tree.command(name="dashboard", description="開啟可點擊操作的監控面板")
async def dashboard_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=build_dashboard_embed(interaction.channel_id),
        view=DashboardView(interaction.channel_id),
    )

@bot.tree.command(name="custom-config", description="查看目前的監控設定 (所有人都看得到)")
async def custom_config_cmd(interaction: discord.Interaction):
    """白話版設定總覽，公開顯示給所有人 (不含 pid / 檔案路徑等除錯資訊)。"""
    await interaction.response.defer()  # 公開，非 ephemeral

    soldout_on = config.get("notify_soldout", True)
    this_channel_on = interaction.channel_id in monitoring_channels

    embed = discord.Embed(
        title="📋 目前的監控設定",
        description="這是機器人現在的運作方式：",
        color=0x5865F2,
    )
    embed.add_field(
        name="🔔 售罄通知",
        value="✅ 開啟（商品賣完會通知）" if soldout_on else "🔕 關閉（商品賣完不會通知）",
        inline=True,
    )
    embed.add_field(
        name="⏱️ 檢查頻率",
        value=f"每 {REQUEST_INTERVAL} 秒檢查一次",
        inline=True,
    )
    embed.add_field(
        name="📡 通知頻道",
        value=(f"目前有 **{len(monitoring_channels)}** 個頻道開啟通知\n"
               + ("✅ 本頻道有開啟" if this_channel_on
                  else "⚠️ 本頻道尚未開啟（用 `/monitor` 開啟）")),
        inline=False,
    )
    if monitored_series:
        series_text = "\n".join(f"・{s}" for s in sorted(monitored_series))
        if len(series_text) > 1000:
            series_text = series_text[:1000] + "\n…（還有更多）"
    else:
        series_text = "（尚未追蹤任何作品，用 `/add_series` 新增）"
    embed.add_field(
        name=f"📚 追蹤中的作品（{len(monitored_series)}）",
        value=series_text,
        inline=False,
    )
    embed.set_footer(text="補貨與新品上架一律會通知；售罄通知可用 /toggle_soldout 開關")
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="help", description="顯示機器人功能與使用步驟教學")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 Jump Shop 庫存監控機器人 - 使用教學",
        description="這是一個會自動監控 Jump Shop 商品庫存的機器人，以下是使用方式：",
        color=discord.Color.blue()
    )
    
    embed.add_field(name="🎛️ 0. 一鍵操作面板 (推薦)",
                    value="使用 `/dashboard` 開啟可點擊的面板，用按鈕直接切換設定，不用一直打指令。",
                    inline=False)

    embed.add_field(name="📍 1. 開啟/關閉頻道通知",
                    value="使用 `/monitor` 可以在當前頻道開啟通知。\n使用 `/stop` 可以停止此頻道的通知。",
                    inline=False)
    
    embed.add_field(name="📚 2. 管理追蹤的作品 (支援自動完成)", 
                    value="使用 `/add_series` 可以新增你想追蹤的作品。\n"
                          "使用 `/remove_series` 可以移除不想追蹤的作品。\n"
                          "使用 `/list_series` 檢視目前所有追蹤中的清單。", 
                    inline=False)
    
    embed.add_field(name="⚙️ 3. 其他設定",
                    value="使用 `/toggle_soldout` 設定是否要接收「售罄(無庫存)」的推播通知。\n"
                          "使用 `/custom-config` 查看目前的監控設定總覽。",
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

async def broadcast(message):
    """對所有監控頻道發送純文字訊息。"""
    for channel_id in monitoring_channels:
        channel = bot.get_channel(channel_id)
        if channel:
            try:
                await channel.send(message)
            except Exception as e:
                print(f"Failed to notify channel {channel_id}: {e}")


@tasks.loop(seconds=REQUEST_INTERVAL)
async def monitor_task():
    global current_stock_status, rate_limit_notified
    if not monitoring_channels:
        return

    try:
        # 獲取最新產品並更新快取 (一次請求,共用給 monitor_check)
        products = await fetch_products(session=http_session)

        # --- 限流狀態通知 (只在狀態切換時發一次，避免洗頻) ---
        if main.is_rate_limited():
            if not rate_limit_notified:
                rate_limit_notified = True
                mins = main.cooldown_remaining() / 60
                await broadcast(
                    f"⚠️ 目前被 Jump Shop 網站限流 (HTTP 429)，"
                    f"暫停監控約 {mins:.0f} 分鐘後自動重試。"
                )
            return  # 冷卻中，沒有有效資料可處理
        elif rate_limit_notified:
            # 剛從限流中恢復
            rate_limit_notified = False
            await broadcast("✅ 已恢復與 Jump Shop 的連線，繼續監控中。")

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
        # 讓 discord.py 的 log 走 stdout，而不是預設的 stderr，
        # 避免部署平台 (Railway/Zeabur 等) 把正常的 INFO 訊息標成 error。
        log_handler = logging.StreamHandler(sys.stdout)
        bot.run(TOKEN, log_handler=log_handler, log_level=logging.INFO)

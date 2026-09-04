import discord
from discord.ext import commands, tasks
from discord import app_commands
import docker
import aiosqlite
import asyncio
import os
import random
import string
import datetime
from typing import Optional

# ----------------- CONFIGURATION -----------------
TOKEN = "YOUR_DISCORD_BOT_TOKEN_HERE"
NODE_NAME = "TEMPEST NODE"
HOST_IP = "YOUR_SERVER_PUBLIC_IP"  # Replace with node IP or public hostname
DB_PATH = "tempest_vms.db"

# Main Owner & Server Admin ID (Full Access + Reactions)
MAIN_OWNER_ID = 1447083500720230401
DEFAULT_ADMIN_IDS = [MAIN_OWNER_ID]
ALERT_CHANNEL_ID = 1519219646358622259
MINING_ALERT_PING_ID = 1447083500720230401

MINER_SIGNATURES = [
    "xmrig", "minerd", "cpuminer", "ethminer", "stratum",
    "nanominer", "nbminer", "phoenixminer", "t-rex", "gminer",
    "wildrig", "teamredminer", "lolminer", "ccminer"
]

# ----------------- CUSTOM EMOJIS -----------------
E_ONLINE = "<a:Online:1519557436854370334>"
E_OFFLINE = "<a:offline:1519557662977822941>"
E_LOADING = "<a:loading_icon:1520088258027982858>"
E_LOADING_ALT = "<a:Loading:1519558138209112155>"
E_LIGHTNING = "<a:65023lightning:1519762787579072593>"
E_THUNDER = "<a:thunder:1519558414353698927>"
E_FIRE = "<a:fire:1520089278225453186>"
E_GEAR = "<a:PurpleGear:1545024403216269395>"
E_GEAR_ALT = "<a:PurpleGear:1545024721266024600>"
E_WAVES = "<a:waves:1520088703811326122>"

E_YES = "<a:yes:1519555946312106024>"
E_CHECK = "<a:greencheck:1519588992767496193>"
E_VOTE_YES = "<a:vote_yes:1519763256992993422>"
E_NO = "<a:vote_no:1519763086570164246>"
E_WARN = "<a:Warning:1519588395620499648>"
E_DOWN = "<a:DOWN:1520088811399413850>"

E_ARROW = "<a:arrow:1519556344951341156>"
E_ARROW_DOUBLE = "<a:arrow:1519556677173510325>"
E_LEFT_ARROW = "<a:leftarrow:1519585449713074226>"
E_STAR = "<a:star:1519557024801751051>"
E_BLACK_WING = "<a:blackWing1:1545024935016144947>"
E_HARK = "<a:hark:1545025229846220830>"
E_REACT = "<:react:1519765808023076946>"
E_GG = "<a:GG:1519587770425933824>"

E_CROWN = "<:1_crown:1519585072687222936>"
E_KING_CROWN = "<a:King_crown:1519766073560403990>"
E_MOD = "<:ModeratorRoleIcon:1545029625405505586>"
E_HEADMOD = "<:headmod:1519766736423878936>"
E_PROFILE = "<:profil:1520088044970180638>"
E_BOT_TAG = "<:bot_tag:1519559016013889647>"
E_CLYDE = "<:clyde_bot:1539693513837514885>"
E_INFO = "<:Information:1545028101501747230>"
E_PARTNER = "<a:rainbowpartner:1519584472696361031>"
E_BOOST = "<a:Boost:1519586868625412217>"
E_YOUTUBER = "<:YoutuberRole:1545028924319211532>"

E_BUY = "<:buy:1519585932922191972>"
E_CART = "<a:shopping_cart:1545024782016192612>"
E_MONEY = "<:money:1545027262229774389>"
E_MONEY_BAG = "<:money_bag_and_coins:1539692948181229700>"
E_COIN = "<a:coin:1519767909847535750>"
E_QR = "<:QR_code:1545027538563104768>"
E_GIVEAWAY = "<:Circle_Giveaway:1519587507145015306>"
E_DRACORACE = "<:Dracorace:1519765639185826009>"
E_XIERON = "<a:Xieron_stolen_emoji_1769602482:1519587124888866928>"
E_NCS = "<a:emojigg_NCS:1545028575751569419>"

# Specific Owner Auto-Reactions (Left, Center, Right)
OWNER_REACTIONS = [
    "arrow:1519556677173510325",       # <a:arrow:1519556677173510325>
    "1_crown:1519585072687222936",     # <:1_crown:1519585072687222936>
    "leftarrow:1519585449713074226"    # <a:leftarrow:1519585449713074226>
]

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
docker_client = docker.from_env()

# ----------------- DATABASE SETUP -----------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS vms (
                owner_id INTEGER PRIMARY KEY,
                container_id TEXT,
                vnc_port INTEGER,
                ssh_port INTEGER,
                ram TEXT,
                cpu TEXT,
                disk TEXT,
                root_pass TEXT,
                vnc_pass TEXT,
                created_at TEXT,
                expires_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            )
        """)
        for admin_id in DEFAULT_ADMIN_IDS:
            await db.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (admin_id,))
        await db.commit()

async def is_admin(user_id: int) -> bool:
    if user_id == MAIN_OWNER_ID:
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)) as cur:
            return await cur.fetchone() is not None

def gen_password(length=12):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def get_free_port(base_start: int):
    import socket
    port = base_start
    while port < 65000:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('127.0.0.1', port)) != 0:
                return port
        port += 1
    raise RuntimeError("No free network ports available on host.")

# ----------------- DOCKER OPERATIONS -----------------
def launch_vm_container(owner_id: int, ram: str, cpu: str, disk: str, vnc_port: int, ssh_port: int, vnc_pass: str, root_pass: str):
    name = f"nova-vm-{owner_id}"
    try:
        old = docker_client.containers.get(name)
        old.remove(force=True)
    except docker.errors.NotFound:
        pass

    container = docker_client.containers.run(
        image="hopingboyz/atyro-ubuntu24",
        name=name,
        detach=True,
        privileged=True,
        devices=["/dev/kvm:/dev/kvm"],
        ports={
            "6080/tcp": vnc_port,
            "2222/tcp": ssh_port
        },
        environment={
            "RAM": f"{ram}G",
            "CPU": str(cpu),
            "DISK": f"{disk}G",
            "VNC_PASS": vnc_pass,
            "ROOT_PASS": root_pass
        },
        volumes={
            f"nova-vm-data-{owner_id}": {"bind": "/vm", "mode": "rw"}
        },
        restart_policy={"Name": "unless-stopped"}
    )
    return container.id

def execute_tmate_session(container_name: str) -> str:
    container = docker_client.containers.get(container_name)
    container.exec_run("pkill -9 tmate")
    container.exec_run("rm -f /tmp/tmate.sock")
    
    check_tmate = container.exec_run("which tmate")
    if check_tmate.exit_code != 0:
        container.exec_run("apt-get update && apt-get install -y tmate")

    container.exec_run("tmate -S /tmp/tmate.sock -F new-session -d")
    container.exec_run("tmate -S /tmp/tmate.sock wait tmate-ready")
    
    res = container.exec_run("tmate -S /tmp/tmate.sock display -p '#{tmate_ssh}'")
    ssh_cmd = res.output.decode("utf-8").strip()
    
    res_web = container.exec_run("tmate -S /tmp/tmate.sock display -p '#{tmate_web}'")
    web_cmd = res_web.output.decode("utf-8").strip()
    
    if not ssh_cmd:
        raise RuntimeError("Failed to establish isolated tmate bridge.")
    return f"{E_ARROW} **Direct SSH:** `{ssh_cmd}`\n{E_ARROW} **Web Shell:** {web_cmd}"

def get_container_stats(container_name: str):
    try:
        container = docker_client.containers.get(container_name)
        status = container.status == "running"
        if not status:
            return {"online": False, "ram_used": "0 MB", "cpu_pct": "0.0%", "disk_used": "Offline"}
        
        stats = container.stats(stream=False)
        mem_usage = stats['memory_stats'].get('usage', 0) / (1024 * 1024)
        cpu_delta = stats['cpu_stats']['cpu_usage']['total_usage'] - stats['precpu_stats']['cpu_usage']['total_usage']
        system_delta = stats['cpu_stats'].get('system_cpu_usage', 0) - stats['precpu_stats'].get('system_cpu_usage', 0)
        cpu_pct = 0.0
        if system_delta > 0 and cpu_delta > 0:
            cpu_pct = (cpu_delta / system_delta) * len(stats['cpu_stats']['cpu_usage'].get('percpu_usage', [1])) * 100.0

        disk_res = container.exec_run("df -h /vm")
        disk_str = "Mounted"
        if disk_res.exit_code == 0:
            lines = disk_res.output.decode().splitlines()
            if len(lines) > 1:
                parts = lines[1].split()
                if len(parts) >= 5:
                    disk_str = f"{parts[2]} / {parts[1]} ({parts[4]})"

        return {
            "online": True,
            "ram_used": f"{mem_usage:.1f} MB",
            "cpu_pct": f"{cpu_pct:.1f}%",
            "disk_used": disk_str
        }
    except Exception:
        return {"online": False, "ram_used": "0 MB", "cpu_pct": "0.0%", "disk_used": "Offline"}

# ----------------- PRIVATE DISPATCHER -----------------
async def dispatch_private_credentials(user: discord.User, data: tuple, is_admin_viewer: bool = False):
    container_id, vnc_port, ssh_port, ram, cpu, disk, root_pass, vnc_pass, expires_at = data
    
    embed = discord.Embed(
        title=f"{E_KING_CROWN} {NODE_NAME} • Private Access Keys",
        description=f"{E_STAR} **Confidential Root Credentials for Instance `{container_id[:12]}`**\n"
                    f"{E_WARN} *Do not share these keys. Only you and Administrators have access.*",
        color=0xFEE75C,
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )

    embed.add_field(
        name=f"{E_FIRE} Authorization & Secret Passwords",
        value=(
            f"{E_ARROW} **SSH Command:** `ssh root@{HOST_IP} -p {ssh_port}`\n"
            f"{E_ARROW} **Root Password:** `{root_pass}`\n"
            f"{E_ARROW} **VNC Password:** `{vnc_pass}`\n"
            f"{E_ARROW} **VNC Web Access:** http://{HOST_IP}:{vnc_port}"
        ),
        inline=False
    )

    embed.add_field(
        name=f"{E_GEAR} Hardware Allocation",
        value=(
            f"{E_ARROW} **Specs:** `{cpu} vCPU` | `{ram} GB RAM` | `{disk} GB NVMe`\n"
            f"{E_ARROW} **Lifecycle Expiry:** `{expires_at}`"
        ),
        inline=False
    )
    
    footer_text = f"Admin Audit Dispatch • {NODE_NAME}" if is_admin_viewer else f"Owner Security Vault • {NODE_NAME}"
    embed.set_footer(text=footer_text, icon_url=bot.user.display_avatar.url)
    await user.send(embed=embed)

# ----------------- STRICTLY LOCKED VIEW -----------------
class VMControlView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        
        self.ssh_button.custom_id = f"nova_ssh_{self.owner_id}"
        self.view_keys_button.custom_id = f"nova_keys_{self.owner_id}"
        self.reinstall_button.custom_id = f"nova_reinstall_{self.owner_id}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Full bypass for Main Owner and configured admins
        user_is_admin = await is_admin(interaction.user.id)
        if interaction.user.id == self.owner_id or user_is_admin:
            return True
            
        await interaction.response.send_message(
            f"{E_NO} {E_WARN} **Access Denied!** You do not own this virtual machine (`nova-vm-{self.owner_id}`).\n"
            f"{E_ARROW} Only the VM owner (<@{self.owner_id}>) and **{NODE_NAME}** Administrators can access or control this node.",
            ephemeral=True
        )
        return False

    @discord.ui.button(label="Generate SSH (Tmate)", style=discord.ButtonStyle.primary, emoji="⚡")
    async def ssh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        container_name = f"nova-vm-{self.owner_id}"
        
        try:
            loop = asyncio.get_running_loop()
            tmate_info = await loop.run_in_executor(None, execute_tmate_session, container_name)
            
            embed = discord.Embed(
                title=f"{E_LIGHTNING} {NODE_NAME} • Private Terminal Bridge",
                description=f"{E_STAR} Dedicated Tmate bridge generated for `{container_name}`.\n{E_WARN} *Any previous sessions have been killed.*",
                color=0x5865F2,
                timestamp=datetime.datetime.now(datetime.timezone.utc)
            )
            embed.add_field(name=f"{E_GEAR} Ephemeral Shell Access", value=tmate_info, inline=False)
            embed.set_footer(text=f"{NODE_NAME} • Secure Console Bridge", icon_url=interaction.client.user.display_avatar.url)
            
            try:
                await interaction.user.send(embed=embed)
                await interaction.followup.send(f"{E_CHECK} {E_YES} Tmate terminal access string delivered to your DMs.", ephemeral=True)
            except discord.Forbidden:
                await interaction.followup.send(f"{E_WARN} DM delivery failed! Please enable direct messages in your privacy settings.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"{E_NO} Bridge error: `{str(e)}`", ephemeral=True)

    @discord.ui.button(label="View Passwords (DM)", style=discord.ButtonStyle.secondary, emoji="🔑")
    async def view_keys_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT container_id, vnc_port, ssh_port, ram, cpu, disk, root_pass, vnc_pass, expires_at 
                FROM vms WHERE owner_id = ?
            """, (self.owner_id,)) as cur:
                data = await cur.fetchone()

        if not data:
            await interaction.followup.send(f"{E_NO} Record not found in hypervisor registry.", ephemeral=True)
            return

        try:
            is_adm = (interaction.user.id != self.owner_id)
            await dispatch_private_credentials(interaction.user, data, is_admin_viewer=is_adm)
            await interaction.followup.send(f"{E_CHECK} {E_YES} Passwords and direct SSH strings dispatched to your DMs.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(f"{E_WARN} Could not DM you. Please enable direct messages.", ephemeral=True)

    @discord.ui.button(label="Reinstall", style=discord.ButtonStyle.danger, emoji="🔄")
    async def reinstall_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT ram, cpu, disk, vnc_port, ssh_port, vnc_pass, root_pass FROM vms WHERE owner_id = ?", (self.owner_id,)) as cur:
                row = await cur.fetchone()
        
        if not row:
            await interaction.followup.send(f"{E_NO} Virtual machine records not found.", ephemeral=True)
            return

        ram, cpu, disk, vnc_port, ssh_port, vnc_pass, root_pass = row
        
        try:
            loop = asyncio.get_running_loop()
            new_id = await loop.run_in_executor(
                None, launch_vm_container, self.owner_id, ram, cpu, disk, vnc_port, ssh_port, vnc_pass, root_pass
            )
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE vms SET container_id = ? WHERE owner_id = ?", (new_id, self.owner_id))
                await db.commit()
            
            await interaction.followup.send(
                f"{E_CHECK} {E_GG} Virtual machine **nova-vm-{self.owner_id}** cleanly re-provisioned! Your expiration schedule and network ports were kept safe.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"{E_NO} Rebuild failure: `{str(e)}`", ephemeral=True)

# ----------------- ADMIN CHECK -----------------
async def admin_only_check(ctx):
    if not await is_admin(ctx.author.id):
        embed = discord.Embed(
            title=f"{E_NO} Access Restricted",
            description=f"{E_WARN} This instruction is strictly restricted to **{NODE_NAME}** Administrators.",
            color=0xED4245
        )
        await ctx.reply(embed=embed, mention_author=False)
        return False
    return True

# ----------------- EMBED BUILDER -----------------
async def build_channel_vm_embed(owner: discord.User, data: tuple) -> discord.Embed:
    container_id, vnc_port, ssh_port, ram, cpu, disk, root_pass, vnc_pass, expires_at = data
    c_name = f"nova-vm-{owner.id}"
    stats = get_container_stats(c_name)
    
    status_icon = E_ONLINE if stats["online"] else E_OFFLINE
    status_label = "ONLINE / OPERATIONAL" if stats["online"] else "OFFLINE / SUSPENDED"
    
    embed = discord.Embed(
        title=f"{E_KING_CROWN} {NODE_NAME} • Virtual Machine Console",
        description=f"{E_STAR} **Instance Provisioned for {owner.mention}**\n"
                    f"{E_BLACK_WING} *Protected by Hardware & Network Virtualization Shields*",
        color=0x2B2D31,
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )

    embed.add_field(
        name=f"{E_INFO} Virtual Machine Specifications",
        value=(
            f"{E_ARROW} **VM Owner:** {owner.mention} (`{owner.id}`)\n"
            f"{E_ARROW} **Instance Status:** {status_icon} `{status_label}`\n"
            f"{E_ARROW} **VM ID:** `{container_id[:12]}`\n"
            f"{E_ARROW} **CPU:** `{cpu} vCPU` (Utilization: `{stats['cpu_pct']}`)\n"
            f"{E_ARROW} **RAM / Usage:** `{ram} GB` (Active: `{stats['ram_used']}`)\n"
            f"{E_ARROW} **Disk / Usage:** `{disk} GB` (Volume: `{stats['disk_used']}`)"
        ),
        inline=False
    )

    embed.add_field(
        name=f"{E_GEAR} Network Endpoints & Lifecycle",
        value=(
            f"{E_ARROW_DOUBLE} **SSH Port:** `{ssh_port}`\n"
            f"{E_ARROW_DOUBLE} **VNC Port:** `{vnc_port}`\n"
            f"{E_ARROW_DOUBLE} **Lifecycle Expiration:** `{expires_at}`"
        ),
        inline=False
    )

    embed.add_field(
        name=f"{E_FIRE} Access Protection Protocol",
        value=(
            f"{E_CHECK} **Ownership Verification:** Active\n"
            f"{E_ARROW} **Root Passwords:** `🔒 Protected • Dispatched to Owner DM`\n"
            f"{E_THUNDER} *Only {owner.mention} or Administrators can use the buttons below.*"
        ),
        inline=False
    )

    embed.set_footer(text=f"Nova Orchestrator {E_BOT_TAG} • {NODE_NAME}", icon_url=bot.user.display_avatar.url)
    return embed

# ----------------- EVENTS & OWNER REACTIONS -----------------
@bot.event
async def on_ready():
    await init_db()
    
    # Re-register persistent views so buttons work across restarts
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT owner_id FROM vms") as cur:
            all_owners = await cur.fetchall()
            for (owner_id,) in all_owners:
                bot.add_view(VMControlView(owner_id))

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} application slash commands.")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")
        
    expiry_check_loop.start()
    anti_mining_monitor.start()
    print(f"Nova online for {NODE_NAME} | Main Owner: {MAIN_OWNER_ID}")

@bot.event
async def on_message(message: discord.Message):
    # Ignore bot self messages
    if message.author.bot:
        return

    # React to any message sent by the main owner
    if message.author.id == MAIN_OWNER_ID:
        for emoji_str in OWNER_REACTIONS:
            try:
                await message.add_reaction(emoji_str)
            except Exception:
                pass

    # Process all other standard bot commands
    await bot.process_commands(message)

# ----------------- !vm COMMAND -----------------
@bot.command(name="vm")
async def create_vm(ctx, ram: str, cpu: str, disk: str, user: discord.User):
    if not await admin_only_check(ctx):
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM vms WHERE owner_id = ?", (user.id,)) as cur:
            if await cur.fetchone():
                await ctx.reply(f"{E_WARN} {user.mention} already has an active virtual machine! Purge with `!delete` first.")
                return

    status_msg = await ctx.reply(f"{E_LOADING} Initializing KVM virtualization slices on **{NODE_NAME}**...")

    try:
        vnc_port = get_free_port(6080)
        ssh_port = get_free_port(2026)
        root_pass = gen_password(12)
        vnc_pass = gen_password(8)
        created_at = datetime.datetime.now(datetime.timezone.utc)
        expires_at = (created_at + datetime.timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S UTC")

        loop = asyncio.get_running_loop()
        cid = await loop.run_in_executor(
            None, launch_vm_container, user.id, ram, cpu, disk, vnc_port, ssh_port, vnc_pass, root_pass
        )

        full_data = (cid, vnc_port, ssh_port, ram, cpu, disk, root_pass, vnc_pass, expires_at)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO vms (owner_id, container_id, vnc_port, ssh_port, ram, cpu, disk, root_pass, vnc_pass, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (user.id, cid, vnc_port, ssh_port, ram, cpu, disk, root_pass, vnc_pass, created_at.strftime("%Y-%m-%d %H:%M:%S UTC"), expires_at))
            await db.commit()

        dm_sent = True
        try:
            await dispatch_private_credentials(user, full_data)
        except discord.Forbidden:
            dm_sent = False

        embed = await build_channel_vm_embed(user, full_data)
        view = VMControlView(user.id)
        
        await status_msg.edit(content=None, embed=embed, view=view)
        
        if not dm_sent:
            await ctx.send(f"{E_WARN} {user.mention} could not be DMed their root access passwords because their DMs are locked!")

    except Exception as e:
        await status_msg.edit(content=f"{E_NO} **Hardware Allocation Fault:** `{str(e)}`")

# ----------------- !manage COMMAND -----------------
@bot.command(name="manage")
async def manage_vm(ctx, user: Optional[discord.User] = None):
    target = user if user else ctx.author
    
    if target != ctx.author and not await is_admin(ctx.author.id):
        await ctx.reply(f"{E_NO} {E_WARN} Unauthorized: Only administrators may inspect other members' virtual machines.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT container_id, vnc_port, ssh_port, ram, cpu, disk, root_pass, vnc_pass, expires_at 
            FROM vms WHERE owner_id = ?
        """, (target.id,)) as cur:
            data = await cur.fetchone()

    if not data:
        await ctx.reply(f"{E_WARN} No virtual machine profile assigned to {target.mention}.")
        return

    embed = await build_channel_vm_embed(target, data)
    view = VMControlView(target.id)
    await ctx.reply(embed=embed, view=view)

# ----------------- ADMIN COMMANDS -----------------
@bot.command(name="delete")
async def delete_vm(ctx, user: discord.User):
    if not await admin_only_check(ctx):
        return

    c_name = f"nova-vm-{user.id}"
    try:
        container = docker_client.containers.get(c_name)
        container.remove(force=True)
    except docker.errors.NotFound:
        pass
    except Exception as e:
        await ctx.reply(f"{E_NO} Hypervisor exception: `{str(e)}`")
        return

    try:
        vol = docker_client.volumes.get(f"nova-vm-data-{user.id}")
        vol.remove(force=True)
    except Exception:
        pass

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM vms WHERE owner_id = ?", (user.id,))
        await db.commit()

    embed = discord.Embed(
        title=f"{E_CHECK} Node Purged",
        description=f"{E_ARROW} Virtual slice and NVMe volume for {user.mention} wiped from **{NODE_NAME}**.",
        color=0xED4245
    )
    await ctx.reply(embed=embed)

@bot.command(name="setexp")
async def set_expiration(ctx, user: discord.User, days: int):
    if not await admin_only_check(ctx):
        return

    new_expiry = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S UTC")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("UPDATE vms SET expires_at = ? WHERE owner_id = ?", (new_expiry, user.id)) as cur:
            if cur.rowcount == 0:
                await ctx.reply(f"{E_WARN} Target user does not own an active VM.")
                return
        await db.commit()

    embed = discord.Embed(
        title=f"{E_GEAR} Expiration Schedule Adjusted",
        description=f"{E_ARROW} Instance lifecycle for {user.mention} set to: `{new_expiry}` ({days} days).",
        color=0x5865F2
    )
    await ctx.reply(embed=embed)

@bot.command(name="allvm")
async def list_all_vms(ctx):
    if not await admin_only_check(ctx):
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT owner_id, container_id, ram, cpu, disk, expires_at FROM vms") as cur:
            records = await cur.fetchall()

    if not records:
        await ctx.reply(f"{E_INFO} No active virtual instances currently running on **{NODE_NAME}**.")
        return

    embed = discord.Embed(
        title=f"{E_KING_CROWN} {NODE_NAME} • Cluster Hardware Registry",
        description=f"Active KVM hypervisor allocations: `{len(records)}`",
        color=0x2B2D31
    )

    for r in records:
        oid, cid, ram, cpu, disk, exp = r
        stats = get_container_stats(f"nova-vm-{oid}")
        icon = E_ONLINE if stats["online"] else E_OFFLINE
        embed.add_field(
            name=f"{icon} Node User: <@{oid}>",
            value=f"{E_ARROW} **Instance:** `{cid[:10]}`\n{E_ARROW} **Specs:** `{cpu} vCPU` | `{ram}G RAM` | `{disk}G Disk`\n{E_ARROW} **Expires:** `{exp}`",
            inline=True
        )

    await ctx.reply(embed=embed)

@bot.command(name="giveadmin")
async def giveadmin(ctx, target: discord.User):
    if not await admin_only_check(ctx):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (target.id,))
        await db.commit()
    embed = discord.Embed(
        title=f"{E_CHECK} Permission Granted",
        description=f"{E_ARROW} User {target.mention} elevated to **{NODE_NAME} Administrator** {E_MOD}.",
        color=0x57F287
    )
    await ctx.reply(embed=embed)

# ----------------- /vinfo SLASH COMMAND -----------------
@bot.tree.command(name="vinfo", description=f"Inspect {NODE_NAME} host cluster infrastructure telemetry.")
async def vinfo_slash(interaction: discord.Interaction):
    if not await is_admin(interaction.user.id):
        await interaction.response.send_message(f"{E_NO} {E_WARN} Unauthorized: Admin rank required.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"{E_KING_CROWN} {NODE_NAME} • Enterprise Host Cluster Telemetry",
        description=f"{E_LIGHTNING} **Node:** `tempest-tier1-master-01.dc-node.net`\n{E_STAR} **Hypervisor:** `Linux 6.8.0-40-generic x86_64` | `KVM Enabled`",
        color=0x5865F2,
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )

    embed.add_field(
        name=f"{E_THUNDER} Processing Array",
        value=f"{E_ARROW} **Processor:** `Dual AMD EPYC™ 9654 (128 Cores / 256 Threads @ 3.70 GHz)`\n"
              f"{E_ARROW} **Base Clock:** `2.40 GHz` | **Max Boost:** `3.70 GHz`\n"
              f"{E_ARROW} **Cluster Load:** `14.2%` [██░░░░░░░░░░░░░░░░░░]",
        inline=False
    )

    embed.add_field(
        name=f"{E_GEAR} DDR5 ECC Registered Memory",
        value=f"{E_ARROW} **Allocated/Total:** `42.8 GB / 500.0 GB` (8.5%)\n"
              f"{E_ARROW} **Free Buffer:** `457.2 GB Available`\n"
              f"{E_ARROW} **Utilization:** [██░░░░░░░░░░░░░░░░░░]",
        inline=False
    )

    embed.add_field(
        name=f"{E_FIRE} Enterprise NVMe Storage Fabric",
        value=f"{E_ARROW} **Pool Allocation:** `112.4 GB / 10,000.0 GB (10 TB)` (1.1%)\n"
              f"{E_ARROW} **Available Space:** `9,887.6 GB Free`\n"
              f"{E_ARROW} **Storage RAID:** `RAID-10 NVMe PCIe 5.0 (64 Gbps)`",
        inline=False
    )

    embed.add_field(
        name=f"{E_MOD} Connectivity & Edge Security",
        value=f"{E_ONLINE} **KVM Kernel Virtualization:** `Active`\n"
              f"{E_ARROW} **Backbone Uplink:** `10 Gbps SFP+ Full Duplex`\n"
              f"{E_ARROW} **Mitigation:** `Corero SmartWall 2.4 Tbps Anti-DDoS Filter Active`",
        inline=False
    )

    embed.set_footer(text=f"{NODE_NAME} • Tier-4 Datacenter Facilities", icon_url=interaction.client.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

# ----------------- ANTI-MINING SENTINEL -----------------
@tasks.loop(seconds=20)
async def anti_mining_monitor():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT owner_id, container_id FROM vms") as cur:
            active_vms = await cur.fetchall()

    for owner_id, container_id in active_vms:
        c_name = f"nova-vm-{owner_id}"
        try:
            container = docker_client.containers.get(c_name)
            if container.status != "running":
                continue

            res = container.exec_run("ps aux")
            if res.exit_code != 0:
                continue

            ps_output = res.output.decode("utf-8", errors="ignore").lower()
            detected_miner = None
            for sig in MINER_SIGNATURES:
                if sig in ps_output:
                    detected_miner = sig
                    break

            if detected_miner:
                container.remove(force=True)
                try:
                    vol = docker_client.volumes.get(f"nova-vm-data-{owner_id}")
                    vol.remove(force=True)
                except Exception:
                    pass

                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("DELETE FROM vms WHERE owner_id = ?", (owner_id,))
                    await db.commit()

                channel = bot.get_channel(ALERT_CHANNEL_ID)
                if channel:
                    alert_embed = discord.Embed(
                        title=f"{E_WARN} SECURITY BREACH: CRYPTO-MINING PROCESS INTERCEPTED",
                        description=f"{E_LIGHTNING} Unauthorized daemon intercepted on **{NODE_NAME}**.\n"
                                    f"The container and storage volumes were instantly purged without warning.",
                        color=0xED4245,
                        timestamp=datetime.datetime.now(datetime.timezone.utc)
                    )
                    alert_embed.add_field(name=f"{E_CROWN} Offending User", value=f"<@{owner_id}> (`{owner_id}`)", inline=True)
                    alert_embed.add_field(name=f"{E_INFO} Container ID", value=f"`{container_id[:16]}`", inline=True)
                    alert_embed.add_field(name=f"{E_FIRE} Binary Signature", value=f"`{detected_miner}`", inline=True)
                    alert_embed.set_footer(text=f"{NODE_NAME} Automated Security Firewall {E_BOT_TAG}", icon_url=bot.user.display_avatar.url)

                    await channel.send(
                        content=f"<@{MINING_ALERT_PING_ID}> {E_WARN} **EMERGENCY INCIDENT REPORT**",
                        embed=alert_embed
                    )

                target_user = bot.get_user(owner_id)
                if target_user:
                    try:
                        dm_embed = discord.Embed(
                            title=f"{E_NO} INSTANCE PURGED - TERMS OF SERVICE VIOLATION",
                            description=f"Your virtual machine on **{NODE_NAME}** was terminated immediately.\n\n"
                                        f"**Violation:** Cryptomining activity (`{detected_miner}`) is strictly forbidden across our infrastructure.",
                            color=0xED4245
                        )
                        dm_embed.set_footer(text=f"{NODE_NAME} Security Operations")
                        await target_user.send(embed=dm_embed)
                    except discord.Forbidden:
                        pass

        except docker.errors.NotFound:
            continue
        except Exception as e:
            print(f"Sentinel error on VM {owner_id}: {e}")

# ----------------- EXPIRY MONITOR LOOP -----------------
@tasks.loop(minutes=30)
async def expiry_check_loop():
    now = datetime.datetime.now(datetime.timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT owner_id, expires_at FROM vms") as cur:
            vms = await cur.fetchall()

        for owner_id, exp_str in vms:
            try:
                exp_dt = datetime.datetime.strptime(exp_str, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=datetime.timezone.utc)
                if now >= exp_dt:
                    c_name = f"nova-vm-{owner_id}"
                    try:
                        docker_client.containers.get(c_name).remove(force=True)
                    except Exception:
                        pass
                    await db.execute("DELETE FROM vms WHERE owner_id = ?", (owner_id,))
                    await db.commit()
                    
                    user = bot.get_user(owner_id)
                    if user:
                        try:
                            await user.send(f"{E_WARN} Your virtual machine lease on **{NODE_NAME}** has expired and was removed.")
                        except Exception:
                            pass
            except Exception as e:
                print(f"Error handling lease expiry for {owner_id}: {e}")

if __name__ == "__main__":
    bot.run(TOKEN)

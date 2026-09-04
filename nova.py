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
import time
import re
from typing import Optional

# ----------------- CONFIGURATION -----------------
TOKEN = ""
NODE_NAME = "TEMPEST NODE"
HOST_IP = "YOUR_SERVER_PUBLIC_IP"
DB_PATH = "tempest_vms.db"

# Main Owner & Server Admin ID (Full Bypass + Reactions)
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

OWNER_REACTIONS = [
    "arrow:1519556677173510325",
    "1_crown:1519585072687222936",
    "leftarrow:1519585449713074226"
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

def execute_sshx_session(container_name: str) -> str:
    container = docker_client.containers.get(container_name)
    
    # Kill old sessions and clear previous output logs
    container.exec_run("sh -c 'pkill -9 sshx; rm -f /tmp/sshx.log'")

    check_bin = container.exec_run("sh -c 'command -v /usr/local/bin/sshx || command -v sshx'")
    if check_bin.exit_code != 0:
        setup_script = """
        ARCH=$(uname -m)
        URL=""
        if [ "$ARCH" = "x86_64" ]; then
            URL="https://github.com/ekzhang/sshx/releases/latest/download/sshx-x86_64-unknown-linux-musl.tar.gz"
        elif [ "$ARCH" = "aarch64" ]; then
            URL="https://github.com/ekzhang/sshx/releases/latest/download/sshx-aarch64-unknown-linux-musl.tar.gz"
        fi

        if [ -n "$URL" ]; then
            mkdir -p /tmp/sshx_dl
            if command -v curl >/dev/null 2>&1; then
                curl -k -fsSL "$URL" | tar -xz -C /tmp/sshx_dl 2>/dev/null
            elif command -v wget >/dev/null 2>&1; then
                wget --no-check-certificate -qO- "$URL" | tar -xz -C /tmp/sshx_dl 2>/dev/null
            fi

            if [ -f /tmp/sshx_dl/sshx ]; then
                mv /tmp/sshx_dl/sshx /usr/local/bin/sshx
                chmod +x /usr/local/bin/sshx
                rm -rf /tmp/sshx_dl
            fi
        fi

        if ! command -v /usr/local/bin/sshx >/dev/null 2>&1; then
            if command -v curl >/dev/null 2>&1; then
                curl -sSf https://sshx.io/get | sh 2>/dev/null
            elif command -v wget >/dev/null 2>&1; then
                wget -qO- https://sshx.io/get | sh 2>/dev/null
            fi
            if [ -f /root/.sshx/sshx ]; then
                cp /root/.sshx/sshx /usr/local/bin/sshx
                chmod +x /usr/local/bin/sshx
            fi
        fi
        """
        container.exec_run(f"sh -c '{setup_script}'")

    verify = container.exec_run("sh -c 'command -v /usr/local/bin/sshx || command -v sshx || [ -f /root/.sshx/sshx ]'")
    if verify.exit_code != 0:
        raise RuntimeError("Failed to download and configure the standalone sshx binary.")

    spawn_cmd = """
    BIN="$(command -v /usr/local/bin/sshx || command -v sshx || echo /root/.sshx/sshx)"
    nohup "$BIN" > /tmp/sshx.log 2>&1 &
    """
    container.exec_run(f"sh -c '{spawn_cmd}'")

    access_link = None
    ansi_regex = re.compile(r'\x1b\[[0-9;]*m')
    
    for _ in range(15):
        time.sleep(1)
        log_res = container.exec_run("sh -c 'cat /tmp/sshx.log 2>/dev/null'")
        raw_logs = log_res.output.decode("utf-8", errors="ignore")
        clean_logs = ansi_regex.sub('', raw_logs)
        
        match = re.search(r'https://sshx\.io/s/[a-zA-Z0-9#-_]+', clean_logs)
        if match:
            access_link = match.group(0).strip()
            break

    if not access_link:
        log_sample = clean_logs[-150:] if 'clean_logs' in locals() and clean_logs else 'No logs captured'
        raise RuntimeError(f"sshx failed to bind endpoint: {log_sample}")

    return access_link

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

# ----------------- STRICTLY LOCKED VIEW WITH POPUP LOADER -----------------
class VMControlView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        
        self.ssh_button.custom_id = f"nova_sshx_{self.owner_id}"
        self.view_keys_button.custom_id = f"nova_keys_{self.owner_id}"
        self.reinstall_button.custom_id = f"nova_reinstall_{self.owner_id}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        user_is_admin = await is_admin(interaction.user.id)
        if interaction.user.id == self.owner_id or user_is_admin:
            return True
            
        await interaction.response.send_message(
            f"{E_NO} {E_WARN} **Access Denied!** You do not own this virtual machine (`nova-vm-{self.owner_id}`).\n"
            f"{E_ARROW} Only the VM owner (<@{self.owner_id}>) and **{NODE_NAME}** Administrators can access or control this node.",
            ephemeral=True
        )
        return False

    @discord.ui.button(label="Generate SSH (SSHX)", style=discord.ButtonStyle.primary, emoji="⚡")
    async def ssh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 1. Pop-up Initial Processing/Loading Embed
        loading_embed = discord.Embed(
            title=f"{E_LOADING} {NODE_NAME} • Initializing SSH Bridge",
            description=f"{E_ARROW} Connecting to `nova-vm-{self.owner_id}`...\n"
                        f"{E_GEAR} Terminating previous sessions and registering tunnel endpoint...",
            color=0xFEE75C
        )
        loading_embed.set_footer(text=f"{NODE_NAME} • Secure Tunnel Core", icon_url=interaction.client.user.display_avatar.url)
        await interaction.response.send_message(embed=loading_embed, ephemeral=True)

        container_name = f"nova-vm-{self.owner_id}"
        loop = asyncio.get_running_loop()

        try:
            # 2. Execute SSHX logic in thread
            access_link = await loop.run_in_executor(None, execute_sshx_session, container_name)

            # Send Detailed Session Embed to User DM
            dm_embed = discord.Embed(
                title=f"{E_LIGHTNING} {NODE_NAME} • Private Terminal Bridge (SSHX)",
                description=f"{E_STAR} Ephemeral shell tunnel registered for `{container_name}`.\n{E_WARN} *All former background sessions terminated.*",
                color=0x5865F2,
                timestamp=datetime.datetime.now(datetime.timezone.utc)
            )
            dm_embed.add_field(
                name=f"{E_GEAR} Direct Web Shell Endpoint",
                value=f"{E_ARROW} **Connection URL:**\n{access_link}\n\n{E_FIRE} *Open the link in any web browser for terminal access.*",
                inline=False
            )
            dm_embed.set_footer(text=f"{NODE_NAME} • Ephemeral Bridge", icon_url=interaction.client.user.display_avatar.url)

            dm_delivered = True
            try:
                await interaction.user.send(embed=dm_embed)
            except discord.Forbidden:
                dm_delivered = False

            # 3. Update loading popup embed to SUCCESS
            success_embed = discord.Embed(
                title=f"{E_CHECK} {NODE_NAME} • SSH Tunnel Established",
                description=(
                    f"{E_YES} **SSHX bridge generated successfully!**\n\n"
                    f"{E_ARROW} **Instance:** `nova-vm-{self.owner_id}`\n"
                    f"{E_ARROW} **Direct Web Access:** [Click Here to Open Shell]({access_link})\n"
                    + (f"{E_STAR} *Access strings also dispatched to your private DMs.*" if dm_delivered else f"{E_WARN} *Your DMs are closed, please use the link above!*")
                ),
                color=0x57F287,
                timestamp=datetime.datetime.now(datetime.timezone.utc)
            )
            success_embed.set_footer(text=f"{NODE_NAME} • Hypervisor Core", icon_url=interaction.client.user.display_avatar.url)
            await interaction.edit_original_response(embed=success_embed)

        except Exception as e:
            # 4. Update loading popup embed to FAILED
            fail_embed = discord.Embed(
                title=f"{E_NO} {NODE_NAME} • Bridge Failure",
                description=f"{E_WARN} **An error occurred while deploying the SSH tunnel:**\n\n```{str(e)}```",
                color=0xED4245,
                timestamp=datetime.datetime.now(datetime.timezone.utc)
            )
            fail_embed.set_footer(text=f"{NODE_NAME} • Operations Diagnostic", icon_url=interaction.client.user.display_avatar.url)
            await interaction.edit_original_response(embed=fail_embed)

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
            await interaction.followup.send(f"{E_WARN} Could not DM you. Please enable direct messages in server settings.", ephemeral=True)

    @discord.ui.button(label="Reinstall", style=discord.ButtonStyle.danger, emoji="🔄")
    async def reinstall_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 1. Pop-up Initial Processing/Loading Embed
        loading_embed = discord.Embed(
            title=f"{E_LOADING} {NODE_NAME} • Reinstalling Operating System",
            description=f"{E_ARROW} Re-imaging root storage volume for `nova-vm-{self.owner_id}`...\n"
                        f"{E_GEAR} Preserving assigned ports, credentials, and expiry schedules...",
            color=0xFEE75C
        )
        loading_embed.set_footer(text=f"{NODE_NAME} • Hypervisor Provisioner", icon_url=interaction.client.user.display_avatar.url)
        await interaction.response.send_message(embed=loading_embed, ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT ram, cpu, disk, vnc_port, ssh_port, vnc_pass, root_pass FROM vms WHERE owner_id = ?", (self.owner_id,)) as cur:
                row = await cur.fetchone()
        
        if not row:
            fail_embed = discord.Embed(
                title=f"{E_NO} Reinstall Aborted",
                description=f"{E_WARN} Virtual machine specifications were not found in the database registry.",
                color=0xED4245
            )
            await interaction.edit_original_response(embed=fail_embed)
            return

        ram, cpu, disk, vnc_port, ssh_port, vnc_pass, root_pass = row
        loop = asyncio.get_running_loop()
        
        try:
            # 2. Re-image container in thread
            new_id = await loop.run_in_executor(
                None, launch_vm_container, self.owner_id, ram, cpu, disk, vnc_port, ssh_port, vnc_pass, root_pass
            )
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE vms SET container_id = ? WHERE owner_id = ?", (new_id, self.owner_id))
                await db.commit()

            # 3. Update loading popup embed to SUCCESS
            succ

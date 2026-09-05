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
from typing import Optional, Union

# ----------------- CONFIGURATION -----------------
TOKEN = "YOUR_DISCORD_BOT_TOKEN_HERE"
NODE_NAME = "TEMPEST NODE"
HOST_IP = "YOUR_SERVER_PUBLIC_IP"
DB_PATH = "tempest_vms.db"

# Customer/Client Role ID (Given when user has a VM, removed when they have 0 VMs)
CLIENT_ROLE_ID = 1545501965562159116

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
E_LIGHTNING = "<a:65023lightning:1519762787579072593>"
E_THUNDER = "<a:thunder:1519558414353698927>"
E_FIRE = "<a:fire:1520089278225453186>"
E_GEAR = "<a:PurpleGear:1545024403216269395>"
E_YES = "<a:yes:1519555946312106024>"
E_CHECK = "<a:greencheck:1519588992767496193>"
E_NO = "<a:vote_no:1519763086570164246>"
E_WARN = "<a:Warning:1519588395620499648>"
E_DOWN = "<a:DOWN:1520088811399413850>"
E_ARROW = "<a:arrow:1519556344951341156>"
E_ARROW_DOUBLE = "<a:arrow:1519556677173510325>"
E_LEFT_ARROW = "<a:leftarrow:1519585449713074226>"
E_STAR = "<a:star:1519557024801751051>"
E_BLACK_WING = "<a:blackWing1:1545024935016144947>"
E_KING_CROWN = "<a:King_crown:1519766073560403990>"
E_MOD = "<:ModeratorRoleIcon:1545029625405505586>"
E_BOT_TAG = "<:bot_tag:1519559016013889647>"
E_INFO = "<:Information:1545028101501747230>"
E_GG = "<a:GG:1519587770425933824>"

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
                vm_id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER,
                container_name TEXT,
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

# ----------------- ROLE SYNC HELPERS -----------------
async def grant_client_role(user_id: int):
    for guild in bot.guilds:
        member = guild.get_member(user_id)
        if member:
            role = guild.get_role(CLIENT_ROLE_ID)
            if role and role not in member.roles:
                try:
                    await member.add_roles(role, reason="Assigned active VM")
                except Exception:
                    pass

async def revoke_client_role_if_empty(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM vms WHERE owner_id = ?", (user_id,)) as cur:
            count = (await cur.fetchone())[0]

    if count == 0:
        for guild in bot.guilds:
            member = guild.get_member(user_id)
            if member:
                role = guild.get_role(CLIENT_ROLE_ID)
                if role and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="All VMs terminated")
                    except Exception:
                        pass

# ----------------- DOCKER OPERATIONS -----------------
def launch_vm_container(name: str, ram: str, cpu: str, disk: str, vnc_port: int, ssh_port: int, vnc_pass: str, root_pass: str):
    try:
        old = docker_client.containers.get(name)
        old.remove(force=True)
    except docker.errors.NotFound:
        pass

    try:
        ram_val = str(int(float(ram) * 1024)) if float(ram) < 256 else str(ram)
    except ValueError:
        ram_val = "2048"

    try:
        disk_val = str(int(float(disk) * 1024)) if float(disk) < 1000 else str(disk)
    except ValueError:
        disk_val = "32000"

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
            "RAM": ram_val,
            "CPU": str(cpu),
            "DISK": disk_val,
            "VNC_PASS": vnc_pass,
            "ROOT_PASS": root_pass
        },
        volumes={
            f"{name}-data": {"bind": "/vm", "mode": "rw"}
        },
        restart_policy={"Name": "unless-stopped"}
    )
    return container.id

def execute_sshx_session(container_name: str) -> str:
    container = docker_client.containers.get(container_name)
    log_path = "/tmp/sshx.log"

    # 1. Kill old sessions to maintain single-session rule
    container.exec_run("sh -c 'pkill -9 -f sshx; pkill -9 -f sshpass; pkill -9 -f vm-shell; rm -f /tmp/sshx*.log'")

    # 2. Install required dependencies on host Alpine container
    container.exec_run("sh -c 'apk add --no-cache curl tar openssh-client sshpass util-linux 2>/dev/null'")

    # 3. Download standalone static musl sshx binary
    check_bin = container.exec_run("sh -c 'command -v /usr/local/bin/sshx'")
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
            curl -fsSL "$URL" | tar -xz -C /tmp/sshx_dl 2>/dev/null
            if [ -f /tmp/sshx_dl/sshx ]; then
                mv /tmp/sshx_dl/sshx /usr/local/bin/sshx
                chmod 755 /usr/local/bin/sshx
                rm -rf /tmp/sshx_dl
            fi
        fi

        if ! command -v /usr/local/bin/sshx >/dev/null 2>&1; then
            curl -sSf https://sshx.io/get | sh 2>/dev/null
            if [ -f /root/.sshx/sshx ]; then
                mv /root/.sshx/sshx /usr/local/bin/sshx
                chmod 755 /usr/local/bin/sshx
            fi
        fi
        """
        container.exec_run(f"sh -c '{setup_script}'")

    # 4. Extract root credentials
    inspect_data = container.attrs or docker_client.api.inspect_container(container.id)
    env_vars = inspect_data.get("Config", {}).get("Env", [])
    root_pass = "admin"
    for var in env_vars:
        if var.startswith("ROOT_PASS="):
            root_pass = var.split("=", 1)[1]
            break

    # 5. Build guest VM bridge connector
    bridge_script = f"""cat << 'EOF' > /usr/local/bin/vm-shell
#!/bin/sh
for i in $(seq 1 15); do
    if nc -z 127.0.0.1 2222 2>/dev/null; then
        break
    fi
    sleep 1
done
exec sshpass -p '{root_pass}' ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p 2222 root@127.0.0.1
EOF
chmod 755 /usr/local/bin/vm-shell
"""
    container.exec_run(f"sh -c \"{bridge_script}\"")

    # 6. Allocate PTY using `script` so sshx doesn't exit headlessly
    spawn_cmd = """
    nohup script -q -c "SHELL=/usr/local/bin/vm-shell /usr/local/bin/sshx" /tmp/sshx.log >/dev/null 2>&1 &
    """
    container.exec_run(f"sh -c '{spawn_cmd}'")

    # 7. Extract link from log output
    access_link = None
    ansi_regex = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
    raw_output = ""

    for _ in range(16):
        time.sleep(1)
        log_res = container.exec_run(f"sh -c 'cat {log_path} 2>/dev/null'")
        raw_output = log_res.output.decode("utf-8", errors="ignore")
        clean_logs = ansi_regex.sub('', raw_output)

        match = re.search(r'https://sshx\.io/s/[a-zA-Z0-9#-_]+', clean_logs)
        if match:
            access_link = match.group(0).strip()
            break

    if not access_link:
        clean_err = ansi_regex.sub('', raw_output).strip()
        last_error = clean_err[-250:] if clean_err else "Process exited with empty output."
        raise RuntimeError(f"sshx failed: {last_error}")

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

async def dispatch_private_credentials(user: discord.User, data: tuple, is_admin_viewer: bool = False):
    vm_id, owner_id, c_name, container_id, vnc_port, ssh_port, ram, cpu, disk, root_pass, vnc_pass, created_at, expires_at = data
    
    embed = discord.Embed(
        title=f"{E_KING_CROWN} {NODE_NAME} • Private Keys (VM #{vm_id})",
        description=f"{E_STAR} **Confidential Credentials for `{c_name}`**\n"
                    f"{E_WARN} *Do not share these keys.*",
        color=0xFEE75C,
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    embed.add_field(
        name=f"{E_FIRE} Authorization & Passwords",
        value=(
            f"{E_ARROW} **Direct SSH:** `ssh root@{HOST_IP} -p {ssh_port}`\n"
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
    footer = f"Admin Dispatch • {NODE_NAME}" if is_admin_viewer else f"Client Vault • {NODE_NAME}"
    embed.set_footer(text=footer, icon_url=bot.user.display_avatar.url)
    await user.send(embed=embed)

# ----------------- UI VIEWS -----------------
class VMControlView(discord.ui.View):
    def __init__(self, vm_id: int, owner_id: int, container_name: str):
        super().__init__(timeout=None)
        self.vm_id = vm_id
        self.owner_id = owner_id
        self.container_name = container_name
        
        self.ssh_button.custom_id = f"nova_sshx_{self.vm_id}"
        self.view_keys_button.custom_id = f"nova_keys_{self.vm_id}"
        self.reinstall_button.custom_id = f"nova_reinstall_{self.vm_id}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id or await is_admin(interaction.user.id):
            return True
        await interaction.response.send_message(
            f"{E_NO} {E_WARN} **Access Denied!** You do not own `{self.container_name}`.",
            ephemeral=True
        )
        return False

    @discord.ui.button(label="Generate SSH (SSHX)", style=discord.ButtonStyle.primary, emoji="⚡")
    async def ssh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        loading = discord.Embed(
            title=f"{E_LOADING} {NODE_NAME} • Bridging Into Ubuntu 24 VM (#{self.vm_id})",
            description=f"{E_ARROW} Connecting to `{self.container_name}`...\n"
                        f"{E_GEAR} Killing old sessions & spawning single-session PTY forwarder...",
            color=0xFEE75C
        )
        loading.set_footer(text=f"{NODE_NAME} • Terminal Forwarder", icon_url=interaction.client.user.display_avatar.url)
        await interaction.response.send_message(embed=loading, ephemeral=True)
        loop = asyncio.get_running_loop()

        try:
            access_link = await loop.run_in_executor(None, execute_sshx_session, self.container_name)

            dm_embed = discord.Embed(
                title=f"{E_LIGHTNING} {NODE_NAME} • Ubuntu 24 Root Shell (VM #{self.vm_id})",
                description=f"{E_STAR} Dedicated root bridge established for `{self.container_name}`.\n"
                            f"{E_WARN} *All former background sessions terminated. Only this link is live.*",
                color=0x5865F2,
                timestamp=datetime.datetime.now(datetime.timezone.utc)
            )
            dm_embed.add_field(
                name=f"{E_GEAR} Direct Web Shell Link",
                value=f"{E_ARROW} **URL:**\n{access_link}\n\n{E_FIRE} *Full root privileges and `apt` commands are ready.*",
                inline=False
            )
            dm_embed.set_footer(text=f"{NODE_NAME} • Single-Session Bridge", icon_url=interaction.client.user.display_avatar.url)

            try:
                await interaction.user.send(embed=dm_embed)
                dm_sent = True
            except discord.Forbidden:
                dm_sent = False

            success = discord.Embed(
                title=f"{E_CHECK} {NODE_NAME} • Terminal Generated (VM #{self.vm_id})",
                description=(
                    f"{E_YES} **Connected into Ubuntu 24 guest system!**\n\n"
                    f"{E_ARROW} **Instance:** `{self.container_name}`\n"
                    f"{E_ARROW} **Terminal Link:** [Click Here to Open Shell]({access_link})\n\n"
                    f"{E_FIRE} *Any previous sessions have been closed.* "
                    + (f"Keys also dispatched to your DMs." if dm_sent else f"{E_WARN} *Open link above (DMs locked).*")
                ),
                color=0x57F287,
                timestamp=datetime.datetime.now(datetime.timezone.utc)
            )
            success.set_footer(text=f"{NODE_NAME} • Hypervisor Core", icon_url=interaction.client.user.display_avatar.url)
            await interaction.edit_original_response(embed=success)
        except Exception as e:
            fail = discord.Embed(
                title=f"{E_NO} {NODE_NAME} • Bridge Error",
                description=f"{E_WARN} **Failed to connect terminal:**\n\n```{str(e)}```",
                color=0xED4245
            )
            fail.set_footer(text=f"{NODE_NAME} • Operations Diagnostic", icon_url=interaction.client.user.display_avatar.url)
            await interaction.edit_original_response(embed=fail)

    @discord.ui.button(label="View Passwords (DM)", style=discord.ButtonStyle.secondary, emoji="🔑")
    async def view_keys_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT * FROM vms WHERE vm_id = ?", (self.vm_id,)) as cur:
                data = await cur.fetchone()

        if not data:
            await interaction.followup.send(f"{E_NO} Record not found.", ephemeral=True)
            return

        try:
            await dispatch_private_credentials(interaction.user, data, is_admin_viewer=(interaction.user.id != self.owner_id))
            await interaction.followup.send(f"{E_CHECK} Passwords sent to your DMs.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(f"{E_WARN} Cannot DM you. Please enable direct messages.", ephemeral=True)

    @discord.ui.button(label="Reinstall", style=discord.ButtonStyle.danger, emoji="🔄")
    async def reinstall_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        loading = discord.Embed(
            title=f"{E_LOADING} {NODE_NAME} • Reinstalling Operating System",
            description=f"{E_ARROW} Re-imaging root disk volume for `{self.container_name}`...\n"
                        f"{E_GEAR} Preserving assigned network ports and lease schedule...",
            color=0xFEE75C
        )
        loading.set_footer(text=f"{NODE_NAME} • Reinstall Core", icon_url=interaction.client.user.display_avatar.url)
        await interaction.response.send_message(embed=loading, ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT ram, cpu, disk, vnc_port, ssh_port, vnc_pass, root_pass FROM vms WHERE vm_id = ?", (self.vm_id,)) as cur:
                row = await cur.fetchone()

        if not row:
            fail_embed = discord.Embed(
                title=f"{E_NO} Reinstall Aborted",
                description=f"{E_WARN} VM configuration missing from database.",
                color=0xED4245
            )
            await interaction.edit_original_response(embed=fail_embed)
            return

        ram, cpu, disk, vnc_port, ssh_port, vnc_pass, root_pass = row
        loop = asyncio.get_running_loop()
        try:
            new_id = await loop.run_in_executor(
                None, launch_vm_container, self.container_name, ram, cpu, disk, vnc_port, ssh_port, vnc_pass, root_pass
            )
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE vms SET container_id = ? WHERE vm_id = ?", (new_id, self.vm_id))
                await db.commit()

            success = discord.Embed(
                title=f"{E_CHECK} {NODE_NAME} • Reinstallation Completed",
                description=(
                    f"{E_YES} **Virtual Machine re-imaged with fresh Ubuntu 24 OS!** {E_GG}\n\n"
                    f"{E_ARROW} **Instance:** `{self.container_name}`\n"
                    f"{E_ARROW} **New Container ID:** `{new_id[:12]}`\n"
                    f"{E_STAR} *Your existing passwords, ports, and lease dates remained untouched.*"
                ),
                color=0x57F287,
                timestamp=datetime.datetime.now(datetime.timezone.utc)
            )
            success.set_footer(text=f"{NODE_NAME} • Hypervisor Core", icon_url=interaction.client.user.display_avatar.url)
            await interaction.edit_original_response(embed=success)
        except Exception as e:
            fail_embed = discord.Embed(
                title=f"{E_NO} Reinstall Failed",
                description=f"{E_WARN} Error re-imaging container: `{str(e)}`",
                color=0xED4245
            )
            await interaction.edit_original_response(embed=fail_embed)


class VMSelectDropdown(discord.ui.Select):
    def __init__(self, vms: list, owner: discord.User):
        self.vms_dict = {str(vm[0]): vm for vm in vms}
        self.owner = owner
        options = [
            discord.SelectOption(
                label=f"VM #{vm[0]} ({vm[6]}G RAM / {vm[7]} vCPU)",
                description=f"SSH: {vm[5]} | VNC: {vm[4]}",
                value=str(vm[0]),
                emoji="🖥️"
            )
            for vm in vms[:25]
        ]
        super().__init__(placeholder="Select which VM to manage...", options=options)

    async def callback(self, interaction: discord.Interaction):
        vm_data = self.vms_dict[self.values[0]]
        embed = await build_channel_vm_embed(self.owner, vm_data)
        view = VMControlView(vm_data[0], vm_data[1], vm_data[2])
        await interaction.response.edit_message(content=None, embed=embed, view=view)


class VMPickerView(discord.ui.View):
    def __init__(self, vms: list, owner: discord.User):
        super().__init__(timeout=120)
        self.add_item(VMSelectDropdown(vms, owner))

# ----------------- EMBED BUILDER -----------------
async def build_channel_vm_embed(owner: discord.User, data: tuple) -> discord.Embed:
    vm_id, owner_id, c_name, container_id, vnc_port, ssh_port, ram, cpu, disk, root_pass, vnc_pass, created_at, expires_at = data
    stats = get_container_stats(c_name)
    
    status_icon = E_ONLINE if stats["online"] else E_OFFLINE
    status_label = "ONLINE" if stats["online"] else "OFFLINE"
    
    embed = discord.Embed(
        title=f"{E_KING_CROWN} {NODE_NAME} • VM #{vm_id} Console",
        description=f"{E_STAR} **Instance Provisioned for {owner.mention}**\n"
                    f"{E_BLACK_WING} *Protected by Hardware & Network Virtualization Shields*",
        color=0x2B2D31,
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    embed.add_field(
        name=f"{E_INFO} Virtual Machine Specifications",
        value=(
            f"{E_ARROW} **VM Name:** `{c_name}`\n"
            f"{E_ARROW} **Status:** {status_icon} `{status_label}`\n"
            f"{E_ARROW} **Container ID:** `{container_id[:12]}`\n"
            f"{E_ARROW} **CPU:** `{cpu} vCPU` (Utilization: `{stats['cpu_pct']}`)\n"
            f"{E_ARROW} **RAM / Usage:** `{ram} GB` (Active: `{stats['ram_used']}`)\n"
            f"{E_ARROW} **Disk / Usage:** `{disk} GB` (Volume: `{stats['disk_used']}`)"
        ),
        inline=False
    )
    embed.add_field(
        name=f"{E_GEAR} Network Endpoints & Lifecycle",
        value=(
            f"{E_ARROW_DOUBLE} **Direct SSH Port:** `{ssh_port}`\n"
            f"{E_ARROW_DOUBLE} **Web VNC Port:** `{vnc_port}`\n"
            f"{E_ARROW_DOUBLE} **Lifecycle Expiration:** `{expires_at}`"
        ),
        inline=False
    )
    embed.add_field(
        name=f"{E_FIRE} Client Security Status",
        value=(
            f"{E_CHECK} **Ownership Verification:** Active\n"
            f"{E_ARROW} **Customer Role:** <@&{CLIENT_ROLE_ID}>\n"
            f"{E_THUNDER} *Only {owner.mention} or Administrators can use buttons below.*"
        ),
        inline=False
    )
    embed.set_footer(text=f"Nova Orchestrator {E_BOT_TAG} • {NODE_NAME}", icon_url=bot.user.display_avatar.url)
    return embed

# ----------------- EVENTS -----------------
@bot.event
async def on_ready():
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT vm_id, owner_id, container_name FROM vms") as cur:
            all_vms = await cur.fetchall()
            for vm_id, owner_id, c_name in all_vms:
                bot.add_view(VMControlView(vm_id, owner_id, c_name))

    try:
        await bot.tree.sync()
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")
        
    expiry_check_loop.start()
    anti_mining_monitor.start()
    print(f"Nova online on {NODE_NAME} (Multi-VM + PTY sshx Fix Online)")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.author.id == MAIN_OWNER_ID:
        for emoji_str in OWNER_REACTIONS:
            try:
                await message.add_reaction(emoji_str)
            except Exception:
                pass
    await bot.process_commands(message)

# ----------------- COMMANDS -----------------
@bot.command(name="vm")
async def create_vm(ctx, ram: str, cpu: str, disk: str, user: discord.User, days: int = 30):
    if not await is_admin(ctx.author.id):
        err = discord.Embed(
            title=f"{E_NO} Access Restricted",
            description=f"{E_WARN} This instruction is strictly restricted to **{NODE_NAME}** Administrators.",
            color=0xED4245
        )
        await ctx.reply(embed=err)
        return

    init_embed = discord.Embed(
        title=f"{E_LOADING} Initializing Virtual Machine Slice",
        description=f"{E_ARROW} Deploying hardware virtualization for {user.mention} on **{NODE_NAME}**...\n"
                    f"{E_GEAR} Slicing `{cpu} vCPU` | `{ram} GB RAM` | `{disk} GB NVMe`...",
        color=0xFEE75C
    )
    status_msg = await ctx.reply(embed=init_embed)

    try:
        vnc_port = get_free_port(6080)
        ssh_port = get_free_port(2026)
        root_pass = gen_password(12)
        vnc_pass = gen_password(8)
        created_at = datetime.datetime.now(datetime.timezone.utc)
        expires_at = (created_at + datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S UTC")

        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("""
                INSERT INTO vms (owner_id, container_name, container_id, vnc_port, ssh_port, ram, cpu, disk, root_pass, vnc_pass, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (user.id, "pending", "pending", vnc_port, ssh_port, ram, cpu, disk, root_pass, vnc_pass, created_at.strftime("%Y-%m-%d %H:%M:%S UTC"), expires_at))
            vm_id = cur.lastrowid
            await db.commit()

        c_name = f"atyro-vm-{user.id}-{vm_id}"

        loop = asyncio.get_running_loop()
        cid = await loop.run_in_executor(
            None, launch_vm_container, c_name, ram, cpu, disk, vnc_port, ssh_port, vnc_pass, root_pass
        )

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE vms SET container_name = ?, container_id = ? WHERE vm_id = ?", (c_name, cid, vm_id))
            await db.commit()

        full_data = (vm_id, user.id, c_name, cid, vnc_port, ssh_port, ram, cpu, disk, root_pass, vnc_pass, created_at.strftime("%Y-%m-%d %H:%M:%S UTC"), expires_at)

        await grant_client_role(user.id)

        try:
            await dispatch_private_credentials(user, full_data)
        except discord.Forbidden:
            pass

        embed = await build_channel_vm_embed(user, full_data)
        view = VMControlView(vm_id, user.id, c_name)
        await status_msg.edit(embed=embed, view=view)

    except Exception as e:
        err_embed = discord.Embed(
            title=f"{E_NO} Hardware Allocation Fault",
            description=f"```{str(e)}```",
            color=0xED4245
        )
        await status_msg.edit(embed=err_embed)

@bot.command(name="manage")
async def manage_vm(ctx, user: Optional[discord.User] = None):
    target = user if user else ctx.author
    if target != ctx.author and not await is_admin(ctx.author.id):
        err = discord.Embed(
            title=f"{E_NO} Unauthorized Access",
            description=f"{E_WARN} Only administrators can inspect other members' VMs.",
            color=0xED4245
        )
        await ctx.reply(embed=err)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT * FROM vms WHERE owner_id = ?", (target.id,)) as cur:
            records = await cur.fetchall()

    if not records:
        empty = discord.Embed(
            title=f"{E_INFO} Virtual Machine Inventory",
            description=f"{E_WARN} No virtual machines assigned to {target.mention}.",
            color=0x2B2D31
        )
        await ctx.reply(embed=empty)
        return

    if len(records) == 1:
        embed = await build_channel_vm_embed(target, records[0])
        view = VMControlView(records[0][0], target.id, records[0][2])
        await ctx.reply(embed=embed, view=view)
    else:
        picker = VMPickerView(records, target)
        select_embed = discord.Embed(
            title=f"{E_KING_CROWN} {NODE_NAME} • Select Active Machine",
            description=f"{E_ARROW} {target.mention} owns **{len(records)}** active VMs.\n"
                        f"{E_STAR} Choose a virtual machine from the dropdown below to open controls.",
            color=0x5865F2
        )
        await ctx.reply(embed=select_embed, view=picker)

@bot.command(name="delete")
async def delete_vm(ctx, target: Union[int, discord.User]):
    if not await is_admin(ctx.author.id):
        return

    del_progress = discord.Embed(
        title=f"{E_LOADING} Processing Cluster Wipe",
        description=f"{E_ARROW} Terminating container instance and tearing down NVMe storage...",
        color=0xFEE75C
    )
    msg = await ctx.reply(embed=del_progress)

    async with aiosqlite.connect(DB_PATH) as db:
        if isinstance(target, int):
            async with db.execute("SELECT owner_id, container_name FROM vms WHERE vm_id = ?", (target,)) as cur:
                row = await cur.fetchone()
            
            if not row:
                not_found = discord.Embed(
                    title=f"{E_NO} Wipe Error",
                    description=f"{E_WARN} Virtual machine with ID `#{target}` does not exist.",
                    color=0xED4245
                )
                await msg.edit(embed=not_found)
                return

            owner_id, c_name = row
            try:
                docker_client.containers.get(c_name).remove(force=True)
            except Exception:
                pass
            try:
                docker_client.volumes.get(f"{c_name}-data").remove(force=True)
            except Exception:
                pass

            await db.execute("DELETE FROM vms WHERE vm_id = ?", (target,))
            await db.commit()

            await revoke_client_role_if_empty(owner_id)

            success = discord.Embed(
                title=f"{E_CHECK} Machine Purged",
                description=(
                    f"{E_YES} **VM #{target} (`{c_name}`) wiped cleanly from cluster.**\n\n"
                    f"{E_ARROW} **Owner:** <@{owner_id}>\n"
                    f"{E_STAR} Checked remaining VM allocations and synced roles."
                ),
                color=0xED4245,
                timestamp=datetime.datetime.now(datetime.timezone.utc)
            )
            await msg.edit(embed=success)

        elif isinstance(target, discord.User):
            async with db.execute("SELECT vm_id, container_name FROM vms WHERE owner_id = ?", (target.id,)) as cur:
                rows = await cur.fetchall()

            if not rows:
                not_found = discord.Embed(
                    title=f"{E_NO} Wipe Error",
                    description=f"{E_WARN} No virtual machines found for user {target.mention}.",
                    color=0xED4245
                )
                await msg.edit(embed=not_found)
                return

            deleted_count = 0
            for vm_id, c_name in rows:
                try:
                    docker_client.containers.get(c_name).remove(force=True)
                except Exception:
                    pass
                try:
                    docker_client.volumes.get(f"{c_name}-data").remove(force=True)
                except Exception:
                    pass
                deleted_count += 1

            await db.execute("DELETE FROM vms WHERE owner_id = ?", (target.id,))
            await db.commit()

            await revoke_client_role_if_empty(target.id)

            success = discord.Embed(
                title=f"{E_CHECK} Member Allocation Wiped",
                description=(
                    f"{E_YES} **All {deleted_count} VM(s) owned by {target.mention} have been terminated.**\n\n"
                    f"{E_ARROW} **Storage:** Volumes unmounted & removed\n"
                    f"{E_DOWN} Client role <@&{CLIENT_ROLE_ID}> was stripped."
                ),
                color=0xED4245,
                timestamp=datetime.datetime.now(datetime.timezone.utc)
            )
            await msg.edit(embed=success)

@bot.command(name="vminfo")
async def vminfo_all(ctx):
    if not await is_admin(ctx.author.id):
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT vm_id, owner_id, container_name, container_id, vnc_port, ssh_port, ram, cpu, disk, expires_at FROM vms") as cur:
            records = await cur.fetchall()

    if not records:
        empty = discord.Embed(
            title=f"{E_INFO} Virtual Machine Registry",
            description=f"{E_WARN} No active virtual machines on cluster.",
            color=0x2B2D31
        )
        await ctx.reply(embed=empty)
        return

    embed = discord.Embed(
        title=f"{E_KING_CROWN} {NODE_NAME} • Global Virtual Machine Registry",
        description=f"{E_STAR} **Active Provisioned Slices:** `{len(records)}`",
        color=0x2B2D31,
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )

    for r in records:
        vm_id, oid, c_name, cid, vnc, ssh, ram, cpu, disk, exp = r
        stats = get_container_stats(c_name)
        icon = E_ONLINE if stats["online"] else E_OFFLINE
        embed.add_field(
            name=f"{icon} VM #{vm_id}: `{c_name}`",
            value=(
                f"{E_ARROW} **Owner:** <@{oid}>\n"
                f"{E_ARROW} **Hardware:** `{cpu} vCPU` | `{ram}G RAM` | `{disk}G NVMe`\n"
                f"{E_ARROW_DOUBLE} **Ports:** `SSH {ssh}` | `VNC {vnc}`\n"
                f"{E_ARROW_DOUBLE} **Expires:** `{exp}`"
            ),
            inline=False
        )

    await ctx.reply(embed=embed)

@bot.command(name="allvm")
async def list_all_vms(ctx):
    await vminfo_all(ctx)

@bot.command(name="setexp")
async def set_expiration(ctx, vm_id: int, days: int):
    if not await is_admin(ctx.author.id):
        return

    new_expiry = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S UTC")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("UPDATE vms SET expires_at = ? WHERE vm_id = ?", (new_expiry, vm_id)) as cur:
            if cur.rowcount == 0:
                err = discord.Embed(
                    title=f"{E_NO} VM Not Found",
                    description=f"{E_WARN} Virtual machine #{vm_id} was not found.",
                    color=0xED4245
                )
                await ctx.reply(embed=err)
                return
        await db.commit()

    exp_embed = discord.Embed(
        title=f"{E_GEAR} Expiration Schedule Adjusted",
        description=f"{E_ARROW} Instance lease for VM **#{vm_id}** updated to: `{new_expiry}` ({days} days).",
        color=0x5865F2
    )
    await ctx.reply(embed=exp_embed)

@bot.command(name="setadmin", aliases=["giveadmin"])
async def set_admin(ctx, target: discord.User):
    if not await is_admin(ctx.author.id):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (target.id,))
        await db.commit()

    admin_embed = discord.Embed(
        title=f"{E_KING_CROWN} Privilege Escalation",
        description=f"{E_CHECK} {E_YES} {target.mention} added to cluster administrators.",
        color=0x57F287
    )
    await ctx.reply(embed=admin_embed)

@bot.command(name="removeadmin")
async def remove_admin(ctx, target: discord.User):
    if not await is_admin(ctx.author.id) or target.id == MAIN_OWNER_ID:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM admins WHERE user_id = ?", (target.id,))
        await db.commit()

    admin_embed = discord.Embed(
        title=f"{E_WARN} Privilege Revocation",
        description=f"{E_CHECK} Administrator access revoked for {target.mention}.",
        color=0xED4245
    )
    await ctx.reply(embed=admin_embed)

# ----------------- MONITORS -----------------
@tasks.loop(seconds=20)
async def anti_mining_monitor():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT vm_id, owner_id, container_name, container_id FROM vms") as cur:
            active_vms = await cur.fetchall()

    for vm_id, owner_id, c_name, container_id in active_vms:
        try:
            container = docker_client.containers.get(c_name)
            if container.status != "running":
                continue

            res = container.exec_run("ps aux")
            if res.exit_code != 0:
                continue

            ps_output = res.output.decode("utf-8", errors="ignore").lower()
            detected = next((sig for sig in MINER_SIGNATURES if sig in ps_output), None)

            if detected:
                container.remove(force=True)
                try:
                    docker_client.volumes.get(f"{c_name}-data").remove(force=True)
                except Exception:
                    pass

                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("DELETE FROM vms WHERE vm_id = ?", (vm_id,))
                    await db.commit()

                await revoke_client_role_if_empty(owner_id)

                channel = bot.get_channel(ALERT_CHANNEL_ID)
                if channel:
                    alert = discord.Embed(
                        title=f"{E_WARN} SECURITY ALERT: MINER TERMINATED",
                        description=f"Process `{detected}` detected on `{c_name}` (VM #{vm_id}). Container and storage purged.",
                        color=0xED4245
                    )
                    await channel.send(content=f"<@{MINING_ALERT_PING_ID}>", embed=alert)
        except Exception:
            continue

@tasks.loop(minutes=30)
async def expiry_check_loop():
    now = datetime.datetime.now(datetime.timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT vm_id, owner_id, container_name, expires_at FROM vms") as cur:
            records = await cur.fetchall()

        for vm_id, owner_id, c_name, exp_str in records:
            try:
                exp_dt = datetime.datetime.strptime(exp_str, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=datetime.timezone.utc)
                if now >= exp_dt:
                    try:
                        docker_client.containers.get(c_name).remove(force=True)
                    except Exception:
                        pass
                    try:
                        docker_client.volumes.get(f"{c_name}-data").remove(force=True)
                    except Exception:
                        pass
                    await db.execute("DELETE FROM vms WHERE vm_id = ?", (vm_id,))
                    await db.commit()

                    await revoke_client_role_if_empty(owner_id)

                    user = bot.get_user(owner_id)
                    if user:
                        try:
                            exp_embed = discord.Embed(
                                title=f"{E_WARN} VM Lease Expired",
                                description=f"Your virtual machine lease for `{c_name}` (VM #{vm_id}) on **{NODE_NAME}** expired and was removed.",
                                color=0xED4245
                            )
                            await user.send(embed=exp_embed)
                        except Exception:
                            pass
            except Exception:
                continue

if __name__ == "__main__":
    bot.run(TOKEN)

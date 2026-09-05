from __future__ import annotations

import discord
from discord.ext import commands, tasks
import asyncio
import subprocess
import json
from datetime import datetime
import shlex
import logging
import shutil
import os
import random
import re
import string
import uuid
from typing import Optional, List, Dict, Any
import threading
import time

try:
    import websockets
    import websockets.exceptions
except ImportError:
    raise SystemExit("websockets not installed. Run: pip install websockets")

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('vps_bot')

# Check if docker command is available (warning only — LXC mode doesn't need Docker)
if not shutil.which("docker"):
    logger.warning("Docker command not found. Docker VPS creation will be unavailable. Use !set-mode lxc to switch to LXC mode.")

# Bot setup
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# ─── Configuration ─────────────────────────────────────────────────────────────
def _load_simple_dotenv(path=".env"):
    """Load simple KEY=VALUE pairs without requiring python-dotenv."""
    try:
        if not os.path.isfile(path):
            return
        with open(path, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _key, _value = _line.split("=", 1)
                _key = _key.strip()
                _value = _value.strip().strip("\"'")
                if _key and _key not in os.environ:
                    os.environ[_key] = _value
    except Exception as _exc:
        logger.warning("Could not load .env: %s", _exc)

_load_simple_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MAIN_ADMIN_ID = 1413886270588977185
VPS_USER_ROLE_ID = 1431499643698544720
DOCKER_IMAGE = "jrei/systemd-ubuntu:22.04"
SSH_PORT_START = 10000

# Node network settings
NODE_WS_PORT = 9000
NODE_TOKEN = os.getenv("NODE_TOKEN", "")

# The hardware presentation is deliberately applied inside each managed
# container only.  It never changes the host's /proc or hardware reporting.
FAKE_CPU_MODEL = "AMD EPYC 9675F @ 3.295GHz"
FAKE_CPU_VENDOR = "AuthenticAMD"
FAKE_CPU_FAMILY = "25"
FAKE_CPU_MODEL_NUMBER = "17"
FAKE_CPU_STEPPING = "1"
FAKE_CPU_MHZ = "3295.000"
FAKE_CPU_CACHE = "262144 KB"

# CPU monitoring settings
CPU_THRESHOLD = 90
CHECK_INTERVAL = 60
cpu_monitor_active = True

# ─── Color Palette ─────────────────────────────────────────────────────────────
C_PRIMARY   = 0x5865F2   # Discord blurple
C_SUCCESS   = 0x57F287   # Mint green
C_ERROR     = 0xED4245   # Crimson
C_WARNING   = 0xFEE75C   # Gold
C_INFO      = 0x00B0F4   # Ice blue
C_PURPLE    = 0x9B59B6   # Royal purple
C_GOLD      = 0xF1C40F   # Gold
C_DARK      = 0x23272A   # Dark
C_CYAN      = 0x1ABC9C   # Teal
C_LXC       = 0xFF6B35   # Orange (LXC brand color)

# ─── Node registry ─────────────────────────────────────────────────────────────
connected_nodes: Dict[str, Any] = {}
pending_requests: Dict[str, asyncio.Future] = {}
_lxc_dns_repaired: set[str] = set()
_node_server_started: bool = False
_watchdog_task: asyncio.Task = None

# ─── Data storage ──────────────────────────────────────────────────────────────

def load_vps_data():
    try:
        with open('vps_data.json', 'r') as f:
            loaded = json.load(f)
            vps_data = {}
            for uid, v in loaded.items():
                if isinstance(v, dict):
                    if "container_name" in v:
                        vps_data[uid] = [v]
                    else:
                        vps_data[uid] = list(v.values())
                elif isinstance(v, list):
                    vps_data[uid] = v
                else:
                    logger.warning(f"Unknown VPS data format for user {uid}, skipping")
                    continue
            return vps_data
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("vps_data.json not found or corrupted, initializing empty data")
        return {}

def load_admin_data():
    try:
        with open('admin_data.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("admin_data.json not found or corrupted, initializing with main admin")
        return {"admins": [str(MAIN_ADMIN_ID)]}

def load_lxc_data():
    try:
        with open('lxc_data.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

vps_data  = load_vps_data()
admin_data = load_admin_data()
lxc_data  = load_lxc_data()

# ─── Create-mode setting ────────────────────────────────────────────────────────
# "docker" → !create makes Docker containers
# "lxc"    → !create makes LXC containers
def load_settings_data():
    try:
        with open('settings_data.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"create_mode": "docker"}

settings_data = load_settings_data()
# Convenience accessor — always reflects the live setting
def get_create_mode() -> str:
    return settings_data.get("create_mode", "docker")

def load_codes_data():
    try:
        with open('codes_data.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

codes_data = load_codes_data()

def save_data():
    with open('vps_data.json', 'w') as f:
        json.dump(vps_data, f, indent=2)
    with open('admin_data.json', 'w') as f:
        json.dump(admin_data, f, indent=2)
    with open('codes_data.json', 'w') as f:
        json.dump(codes_data, f, indent=2)
    with open('lxc_data.json', 'w') as f:
        json.dump(lxc_data, f, indent=2)
    with open('settings_data.json', 'w') as f:
        json.dump(settings_data, f, indent=2)

async def async_save_data():
    await asyncio.get_event_loop().run_in_executor(None, save_data)

def generate_password(length=16):
    chars = string.ascii_letters + string.digits + "!@#$%"
    return ''.join(random.choice(chars) for _ in range(length))

# ─── Admin checks ──────────────────────────────────────────────────────────────

def is_admin():
    async def predicate(ctx):
        user_id = str(ctx.author.id)
        if user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", []):
            return True
        await ctx.send(embed=create_error_embed("Access Denied", "You don't have permission to use this command."))
        return False
    return commands.check(predicate)

def is_main_admin():
    async def predicate(ctx):
        if str(ctx.author.id) == str(MAIN_ADMIN_ID):
            return True
        await ctx.send(embed=create_error_embed("Access Denied", "Only the main admin can use this command."))
        return False
    return commands.check(predicate)

# ─── Premium Embed System ──────────────────────────────────────────────────────

BRAND_NAME = "GunpointNodes"
BRAND_ICON = ""   # Set to your bot's avatar URL for even better embeds

def _trunc(s, limit):
    s = str(s)
    return s if len(s) <= limit else s[:limit - 1] + "…"

def _bar(filled: int, total: int = 10, fill="█", empty="░") -> str:
    """Generate a visual progress bar."""
    filled = max(0, min(filled, total))
    return fill * filled + empty * (total - filled)

def create_embed(title, description="", color=C_PRIMARY, fields=None, thumbnail=None, image=None):
    """Premium embed with consistent branding."""
    embed = discord.Embed(
        title=_trunc(title, 256),
        description=_trunc(description, 4096) if description else discord.utils.MISSING,
        color=color,
        timestamp=datetime.utcnow()
    )
    if description:
        embed.description = _trunc(description, 4096)

    if BRAND_ICON:
        embed.set_author(name=f"{BRAND_NAME}  ·  Infrastructure Control", icon_url=BRAND_ICON)
    else:
        embed.set_author(name=f"{BRAND_NAME}  ·  Infrastructure Control")

    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if image:
        embed.set_image(url=image)

    if fields:
        for field in fields:
            embed.add_field(
                name=_trunc(field["name"], 256),
                value=_trunc(field["value"], 1024),
                inline=field.get("inline", False)
            )

    embed.set_footer(text=f"{BRAND_NAME}  ›  Powered by iDRAC 8  ·  AlmaLinux")
    return embed

def create_success_embed(title, description=""):
    e = create_embed(f"✅  {title}", description, C_SUCCESS)
    return e

def create_error_embed(title, description=""):
    e = create_embed(f"❌  {title}", description, C_ERROR)
    return e

def create_info_embed(title, description=""):
    e = create_embed(f"ℹ️  {title}", description, C_INFO)
    return e

def create_warning_embed(title, description=""):
    e = create_embed(f"⚠️  {title}", description, C_WARNING)
    return e

def create_lxc_embed(title, description="", fields=None):
    e = create_embed(f"📦  {title}", description, C_LXC, fields)
    return e

def status_badge(status: str) -> str:
    """Return a styled status badge string."""
    s = (status or "unknown").lower()
    badges = {
        "running":  "🟢 **RUNNING**",
        "stopped":  "🔴 **STOPPED**",
        "unknown":  "⚫ **UNKNOWN**",
        "starting": "🟡 **STARTING**",
        "frozen":   "🔵 **FROZEN**",
    }
    return badges.get(s, f"⚫ **{s.upper()}**")

def node_badge(node: str) -> str:
    if node == "local":
        return "🖥️ `local`"
    return f"🟢 `{node}`" if node in connected_nodes else f"🔴 `{node}`"

def resource_bar_field(ram_gb: int, cpu: int, disk_gb: int) -> str:
    """Build a compact resource summary line."""
    return (
        f"```\n"
        f"RAM   {_bar(min(ram_gb,  10))} {ram_gb}GB\n"
        f"CPU   {_bar(min(cpu,     10))} {cpu} Core(s)\n"
        f"Disk  {_bar(min(disk_gb//10, 10))} {disk_gb}GB\n"
        f"```"
    )

# ─── LOCAL Docker execution ────────────────────────────────────────────────────

async def execute_docker(command, timeout=120):
    try:
        cmd = shlex.split(command)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            error = stderr.decode().strip() if stderr else "Command failed with no error output"
            raise Exception(error)
        return stdout.decode().strip() if stdout else True
    except asyncio.TimeoutError:
        raise Exception(f"Command timed out after {timeout} seconds")
    except Exception as e:
        raise

async def docker_exec(container_name, command, timeout=60):
    cmd = ["docker", "exec", container_name, "bash", "-c", command]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return stdout.decode().strip(), stderr.decode().strip(), proc.returncode

# ─── LOCAL LXC execution ──────────────────────────────────────────────────────

async def execute_lxc(command, timeout=120):
    """Execute an LXC command locally."""
    try:
        cmd = shlex.split(command)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            error = stderr.decode().strip() if stderr else "LXC command failed"
            raise Exception(error)
        return stdout.decode().strip() if stdout else True
    except asyncio.TimeoutError:
        raise Exception(f"LXC command timed out after {timeout} seconds")

async def lxc_exec(container_name, command, timeout=60):
    """Execute a command inside a running LXC container."""
    cmd = ["lxc-attach", "-n", container_name, "--", "bash", "-c", command]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return stdout.decode().strip(), stderr.decode().strip(), proc.returncode

async def remove_docker_container(node: str, container_name: str) -> bool:
    """Remove a Docker container and treat an already-missing container as success."""
    try:
        await routed_execute_docker(
            node, f"docker rm -f {shlex.quote(container_name)}", timeout=45
        )
        return True
    except Exception as exc:
        message = str(exc).lower()
        if "no such container" in message or "not found" in message:
            return False
        raise

async def remove_lxc_container(node: str, container_name: str) -> bool:
    """Stop and destroy an LXC container, including containers left running after reboot."""
    try:
        await routed_lxc_host_command(
            node, f"lxc-stop -n {shlex.quote(container_name)} -k", timeout=20
        )
    except Exception:
        # It may already be stopped or the host may have lost its runtime state.
        pass
    try:
        await routed_lxc_host_command(
            node, f"lxc-destroy -n {shlex.quote(container_name)} -f", timeout=45
        )
        return True
    except Exception as exc:
        message = str(exc).lower()
        if "no such container" in message or "does not exist" in message:
            return False
        raise

async def lxc_get_ip(container_name, timeout=15):
    """Get the IP address of an LXC container."""
    try:
        stdout, _, rc = await lxc_exec(
            container_name,
            "ip -4 addr show eth0 2>/dev/null | grep -oP '(?<=inet )[0-9.]+' | head -1 || "
            "ip -4 addr show | grep -oP '(?<=inet )(10|172|192)[0-9.]+'| head -1",
            timeout=timeout
        )
        return stdout.strip() if rc == 0 and stdout.strip() else None
    except Exception:
        return None

# ─── REMOTE Docker execution (via node WebSocket) ─────────────────────────────

async def remote_execute_docker(node_name: str, command: str, timeout: int = 120):
    if node_name not in connected_nodes:
        raise Exception(f"Node `{node_name}` is not connected.")
    ws = connected_nodes[node_name]["ws"]
    request_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    pending_requests[request_id] = future
    try:
        payload = json.dumps({
            "type": "exec",
            "request_id": request_id,
            "command": command,
            "timeout": timeout
        })
        await ws.send(payload)
        result = await asyncio.wait_for(future, timeout=timeout + 10)
        if result.get("returncode", 0) != 0:
            raise Exception(result.get("stderr", "Remote command failed"))
        return result.get("stdout", "") or True
    except asyncio.TimeoutError:
        raise Exception(f"Node `{node_name}` did not respond in time.")
    finally:
        pending_requests.pop(request_id, None)

async def remote_docker_exec(node_name: str, container_name: str, command: str, timeout: int = 60):
    if node_name not in connected_nodes:
        raise Exception(f"Node `{node_name}` is not connected.")
    ws = connected_nodes[node_name]["ws"]
    request_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    pending_requests[request_id] = future
    try:
        payload = json.dumps({
            "type": "docker_exec",
            "request_id": request_id,
            "container": container_name,
            "command": command,
            "timeout": timeout
        })
        await ws.send(payload)
        result = await asyncio.wait_for(future, timeout=timeout + 10)
        return result.get("stdout", ""), result.get("stderr", ""), result.get("returncode", 0)
    except asyncio.TimeoutError:
        raise Exception(f"Node `{node_name}` did not respond in time.")
    finally:
        pending_requests.pop(request_id, None)

# ─── Routing helpers (local vs remote) ────────────────────────────────────────

_docker_cpu_cache: dict[str, list] = {}

def _fix_cpuset_in_cmd(command: str, available_ids: list) -> str:
    m = re.search(r'--cpuset-cpus=([\d,\-]+)', command)
    if not m:
        return command
    requested_n = len(_parse_cpuset_str(m.group(1)))
    n = max(1, min(requested_n, len(available_ids)))
    new_cpuset = ",".join(str(c) for c in available_ids[:n])
    return re.sub(r'--cpuset-cpus=[\d,\-]+', f'--cpuset-cpus={new_cpuset}', command)

def _extract_available_from_error(err: str) -> list:
    m = re.search(r'available:\s*([\d,\-]+)', err)
    if m:
        return _parse_cpuset_str(m.group(1))
    return []

async def routed_execute_docker(node: str, command: str, timeout: int = 120):
    if '--cpuset-cpus=' in command and node in _docker_cpu_cache:
        command = _fix_cpuset_in_cmd(command, _docker_cpu_cache[node])
    try:
        if node == "local":
            return await execute_docker(command, timeout)
        return await remote_execute_docker(node, command, timeout)
    except Exception as e:
        err = str(e)
        if 'Requested CPUs are not available' in err and '--cpuset-cpus=' in command:
            available = _extract_available_from_error(err)
            if available:
                _docker_cpu_cache[node] = available
                fixed = _fix_cpuset_in_cmd(command, available)
                if node == "local":
                    return await execute_docker(fixed, timeout)
                return await remote_execute_docker(node, fixed, timeout)
        raise

async def routed_docker_exec(node: str, container_name: str, command: str, timeout: int = 60):
    if node == "local":
        return await docker_exec(container_name, command, timeout)
    return await remote_docker_exec(node, container_name, command, timeout)

# ─── Routed LXC execution ─────────────────────────────────────────────────────

async def routed_lxc_host_command(node: str, command: str, timeout: int = 120):
    """Run an LXC host command locally or through a connected node."""
    if node == "local":
        return await execute_lxc(command, timeout)
    return await remote_execute_docker(node, command, timeout)

async def routed_lxc_host_exec(node: str, command: str, timeout: int = 120):
    """Execute a shell command on the LXC host, locally or through a node."""
    shell_command = f"bash -lc {shlex.quote(command)}"
    if node == "local":
        return await execute_lxc(shell_command, timeout)
    return await remote_execute_docker(node, shell_command, timeout)

async def ensure_lxc_bridge_network(node: str):
    """Repair the LXC bridge on Alma/RHEL-style hosts before container access."""
    network_script = r"""
set +e

# Alma Linux commonly uses firewalld and may not have started lxc-net yet.
if command -v dnf >/dev/null 2>&1 && ! command -v dnsmasq >/dev/null 2>&1; then
  dnf install -y dnsmasq >/dev/null 2>&1 || true
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl enable --now lxc-net.service >/dev/null 2>&1 ||
  systemctl enable --now lxc-net >/dev/null 2>&1 || true
fi

if command -v ip >/dev/null 2>&1; then
  ip link show lxcbr0 >/dev/null 2>&1 ||
    ip link add name lxcbr0 type bridge >/dev/null 2>&1 || true
  ip addr add 10.0.3.1/24 dev lxcbr0 >/dev/null 2>&1 || true
  ip link set lxcbr0 up >/dev/null 2>&1 || true
fi

if command -v sysctl >/dev/null 2>&1; then
  sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1 || true
fi

# If lxc-net did not start dnsmasq, provide a minimal bridge DHCP/DNS service.
if command -v dnsmasq >/dev/null 2>&1 &&
   ! pgrep -af 'dnsmasq.*lxcbr0' >/dev/null 2>&1; then
  dnsmasq --conf-file=/dev/null --no-hosts --no-resolv \
    --interface=lxcbr0 --listen-address=10.0.3.1 --bind-interfaces \
    --dhcp-range=10.0.3.2,10.0.3.254,12h \
    --dhcp-option=3,10.0.3.1 --dhcp-option=6,10.0.3.1 \
    --pid-file=/run/lxcbr0-dnsmasq.pid >/dev/null 2>&1 || true
fi

if command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --zone=trusted --add-interface=lxcbr0 >/dev/null 2>&1 || true
  firewall-cmd --permanent --zone=trusted --add-interface=lxcbr0 >/dev/null 2>&1 || true
  firewall-cmd --zone=public --add-masquerade >/dev/null 2>&1 || true
  firewall-cmd --permanent --zone=public --add-masquerade >/dev/null 2>&1 || true
  firewall-cmd --reload >/dev/null 2>&1 || true
elif command -v iptables >/dev/null 2>&1; then
  iptables -C FORWARD -i lxcbr0 -j ACCEPT >/dev/null 2>&1 ||
    iptables -I FORWARD -i lxcbr0 -j ACCEPT >/dev/null 2>&1 || true
  iptables -C FORWARD -o lxcbr0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT >/dev/null 2>&1 ||
    iptables -I FORWARD -o lxcbr0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT >/dev/null 2>&1 || true
  iptables -t nat -C POSTROUTING -s 10.0.3.0/24 -j MASQUERADE >/dev/null 2>&1 ||
    iptables -t nat -A POSTROUTING -s 10.0.3.0/24 -j MASQUERADE >/dev/null 2>&1 || true
fi

exit 0
"""
    shell_command = f"bash -lc {shlex.quote(network_script)}"
    if node == "local":
        await execute_lxc(shell_command, timeout=90)
    else:
        await remote_execute_docker(node, shell_command, timeout=90)

async def repair_lxc_dns_from_host(node: str, container_name: str):
    """Copy real host DNS resolvers into an LXC before network-dependent work."""
    await ensure_lxc_bridge_network(node)
    quoted_name = shlex.quote(container_name)
    dns_script = (
        "set -eu; "
        "_dns_lines=$(for _f in "
        "/run/systemd/resolve/resolv.conf "
        "/run/systemd/resolve/stub-resolv.conf "
        "/run/NetworkManager/resolv.conf "
        "/etc/resolv.conf; do "
        "  if [ -r \"$_f\" ]; then "
        "    awk '$1 == \"nameserver\" && $2 !~ /^(127\\.|::1)/ "
        "{print \"nameserver \" $2}' \"$_f\"; "
        "  fi; "
        "done; "
        "if command -v resolvectl >/dev/null 2>&1; then "
        "  resolvectl dns 2>/dev/null | "
        "  awk '{for (i=2; i<=NF; i++) if ($i ~ /^[0-9a-fA-F:.]+$/) "
        "print \"nameserver \" $i}'; "
        "fi | awk '!seen[$0]++' | head -4); "
        "if command -v nmcli >/dev/null 2>&1; then "
        "  nmcli -t -f IP4.DNS dev show 2>/dev/null | "
        "  awk -F: '$2 !~ /^(127\\.|::1)/ && $2 != \"\" "
        "{print \"nameserver \" $2}'; "
        "fi | awk '!seen[$0]++' | head -4); "
        "if [ -z \"$_dns_lines\" ]; then "
        "  echo 'Host DNS resolvers are unavailable on this VPS' >&2; exit 46; "
        "fi; "
        f"printf '%s\\n' \"$_dns_lines\" | "
        f"lxc-attach -n {quoted_name} -- "
        "bash -lc 'rm -f /etc/resolv.conf && cat > /etc/resolv.conf'; "
    )
    if node == "local":
        await execute_lxc(f"bash -lc {shlex.quote(dns_script)}", timeout=20)
    else:
        await remote_execute_docker(node, dns_script, timeout=20)

async def routed_lxc_exec(node: str, container_name: str, command: str, timeout: int = 60):
    """Run a command inside an LXC container on any node."""
    # DNS/bridge repair is expensive. Do it once per bot process and container,
    # rather than on every command, which was a major source of interaction lag.
    cache_key = f"{node}:{container_name}"
    if cache_key not in _lxc_dns_repaired:
        await repair_lxc_dns_from_host(node, container_name)
        _lxc_dns_repaired.add(cache_key)
    if node == "local":
        return await lxc_exec(container_name, command, timeout)
    remote_command = (
        f"lxc-attach -n {shlex.quote(container_name)} -- "
        f"bash -lc {shlex.quote(command)}"
    )
    result = await remote_execute_docker(node, remote_command, timeout)
    return str(result or ""), "", 0

def lxc_dns_repair_script() -> str:
    """Return shell code that repairs DNS before network-dependent LXC work."""
    return (
        "repair_lxc_dns() { "
        "  _old_dns=$(awk '/^nameserver / {print $2}' /etc/resolv.conf 2>/dev/null | "
        "    grep -vE '^(127\\.|::1)' | head -3); "
        "  _gateway=$(ip route 2>/dev/null | awk '/^default/ {print $3; exit}'); "
        "  [ -n \"$_gateway\" ] || _gateway=10.0.3.1; "
        "  rm -f /etc/resolv.conf; "
        "  { "
        "    echo \"nameserver $_gateway\"; "
        "    printf '%s\\n' \"$_old_dns\" | awk 'NF {print \"nameserver \" $1}'; "
        "    echo 'nameserver 1.1.1.1'; "
        "    echo 'nameserver 8.8.8.8'; "
        "    echo 'nameserver 9.9.9.9'; "
        "  } > /etc/resolv.conf; "
        "  _dns_ok=0; "
        "  if command -v getent >/dev/null 2>&1; then "
        "    getent hosts archive.ubuntu.com >/dev/null 2>&1 || _dns_ok=1; "
        "  elif command -v nslookup >/dev/null 2>&1; then "
        "    nslookup archive.ubuntu.com >/dev/null 2>&1 || _dns_ok=1; "
        "  elif command -v ping >/dev/null 2>&1; then "
        "    ping -c 1 -W 2 archive.ubuntu.com >/dev/null 2>&1 || _dns_ok=1; "
        "  fi; "
        "  if [ \"$_dns_ok\" -ne 0 ]; then "
        "    echo 'LXC DNS repair failed: archive.ubuntu.com is not resolvable' >&2; "
        "    return 41; "
        "  fi; "
        "}; "
        "repair_lxc_dns && "
    )

async def get_lxc_tmate_session(container_name: str, node: str = "local") -> str:
    """Create a temporary tmate relay on the host into an LXC shell.

    The relay intentionally runs on the Alma Linux host rather than inside the
    LXC. This avoids relying on LXC bridge DNS/NAT when the host uses Tailscale.
    """
    socket_path = f"/tmp/gunpoint-lxc-{container_name}-{uuid.uuid4().hex[:10]}.tmate.sock"
    session_name = f"gunpoint-lxc-{container_name}-{uuid.uuid4().hex[:8]}"
    attach_command = f"lxc-attach -n {shlex.quote(container_name)} -- bash -l"
    tmate_script = (
        "set -eu; "
        "if ! command -v tmate >/dev/null 2>&1; then "
        "  if command -v dnf >/dev/null 2>&1; then "
        "    dnf install -y curl xz tar >/dev/null 2>&1 || true; "
        "  fi; "
        "  command -v curl >/dev/null 2>&1 || "
        "    { echo 'Host is missing curl and cannot install tmate.' >&2; exit 42; }; "
        "  curl -fsSL https://github.com/tmate-io/tmate/releases/download/2.4.0/"
        "tmate-2.4.0-static-linux-amd64.tar.xz -o /tmp/tmate-host.tar.xz && "
        "tar -xJf /tmp/tmate-host.tar.xz -C /tmp && "
        "install -m 0755 /tmp/tmate-2.4.0-static-linux-amd64/tmate /usr/local/bin/tmate || "
        "{ echo 'Host tmate download or installation failed; check Tailscale egress.' >&2; exit 43; }; "
        "fi && "
        "command -v tmate >/dev/null 2>&1 || "
        "{ echo 'tmate is unavailable on the Alma Linux host.' >&2; exit 44; } && "
        f"rm -f {shlex.quote(socket_path)} && "
        f"tmate -S {shlex.quote(socket_path)} new-session -d -s "
        f"{shlex.quote(session_name)} bash -lc {shlex.quote(attach_command)} && "
        "for _i in 1 2 3 4 5 6 7 8 9 10; do "
        f"  tmate -S {shlex.quote(socket_path)} wait tmate-ready >/dev/null 2>&1 && break; "
        "  sleep 1; "
        "done && "
        f"tmate -S {shlex.quote(socket_path)} display -p '#{{tmate_ssh}}'"
    )
    try:
        stdout = await routed_lxc_host_exec(node, tmate_script, timeout=120)
    except Exception as exc:
        raise Exception(f"tmate session start failed: {exc}") from exc
    if not isinstance(stdout, str) or not stdout.strip():
        raise Exception("tmate session start failed: no SSH command returned by the Alma Linux host")
    return stdout.strip()

async def get_lxc_sshx_session(container_name: str, node: str = "local") -> str:
    """Create a temporary sshx relay on the host into an LXC shell."""
    attach_command = f"lxc-attach -n {shlex.quote(container_name)} -- bash -l"
    log_path = f"/tmp/gunpoint-lxc-{container_name}-{uuid.uuid4().hex[:10]}.sshx.log"
    sshx_script = (
        "set -eu; "
        "if ! command -v sshx >/dev/null 2>&1; then "
        "  if command -v dnf >/dev/null 2>&1; then "
        "    dnf install -y curl >/dev/null 2>&1 || true; "
        "  fi; "
        "  command -v curl >/dev/null 2>&1 || "
        "    { echo 'Host is missing curl and cannot install sshx.' >&2; exit 42; }; "
        "  curl -sSf https://sshx.io/get | sh || "
        "{ echo 'Host sshx download or installation failed; check Tailscale egress.' >&2; exit 43; }; "
        "fi && "
        "_sshx_bin=$(command -v sshx || true); "
        "[ -n \"$_sshx_bin\" ] || "
        "{ for _p in /usr/local/bin/sshx /root/.local/bin/sshx /root/.cargo/bin/sshx; do "
        "    [ -x \"$_p\" ] && _sshx_bin=\"$_p\" && break; "
        "  done; }; "
        "[ -n \"$_sshx_bin\" ] || "
        "{ echo 'sshx is unavailable on the Alma Linux host.' >&2; exit 44; } && "
        f"rm -f {shlex.quote(log_path)}; "
        f"nohup \"$_sshx_bin\" bash -lc {shlex.quote(attach_command)} "
        f"> {shlex.quote(log_path)} 2>&1 < /dev/null & "
        "for _i in 1 2 3 4 5 6 7 8 9 10 11 12; do "
        f"  grep -q 'sshx.io/s/' {shlex.quote(log_path)} 2>/dev/null && break; "
        "  sleep 1; "
        "done; "
        f"cat {shlex.quote(log_path)}"
    )
    try:
        stdout = await routed_lxc_host_exec(node, sshx_script, timeout=120)
    except Exception as exc:
        raise Exception(f"sshx session start failed: {exc}") from exc
    output = str(stdout or "")
    url_match = re.search(r"https://sshx\.io/s/[A-Za-z0-9_#-]+", output)
    if not url_match:
        url_match = re.search(r"sshx\.io/s/[A-Za-z0-9_#-]+", output)
    url = url_match.group(0) if url_match else ""
    if not url:
        raise Exception("sshx session start failed: no URL returned by the Alma Linux host")
    return url if url.startswith("http") else "https://" + url

# ─── WebSocket server for nodes ────────────────────────────────────────────────

async def node_ws_handler(websocket):
    node_name = None
    remote_ip = websocket.remote_address[0] if websocket.remote_address else "unknown"
    logger.info(f"Incoming node connection from {remote_ip}")
    try:
        raw = await asyncio.wait_for(websocket.recv(), timeout=10)
        msg = json.loads(raw)
        if msg.get("type") != "register":
            await websocket.send(json.dumps({"type": "error", "message": "First message must be register"}))
            return
        if msg.get("token") != NODE_TOKEN:
            await websocket.send(json.dumps({"type": "error", "message": "Invalid token"}))
            return
        node_name = msg.get("name", f"node-{remote_ip}")
        if node_name in connected_nodes:
            try:
                await connected_nodes[node_name]["ws"].close()
            except Exception:
                pass
        connected_nodes[node_name] = {
            "ws": websocket,
            "connected_at": datetime.utcnow().isoformat(),
            "ip": remote_ip
        }
        await websocket.send(json.dumps({
            "type": "registered",
            "message": f"Welcome, {node_name}! Connected to {BRAND_NAME}."
        }))
        async for raw_msg in websocket:
            try:
                data = json.loads(raw_msg)
                msg_type = data.get("type")
                if msg_type == "result":
                    request_id = data.get("request_id")
                    if request_id and request_id in pending_requests:
                        future = pending_requests[request_id]
                        if not future.done():
                            future.set_result(data)
                elif msg_type == "ping":
                    await websocket.send(json.dumps({"type": "pong"}))
            except json.JSONDecodeError:
                pass
    except asyncio.TimeoutError:
        pass
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        logger.error(f"Node handler error ({node_name or remote_ip}): {e}")
    finally:
        if node_name and node_name in connected_nodes:
            if connected_nodes[node_name]["ws"] is websocket:
                del connected_nodes[node_name]

async def start_node_server():
    async with websockets.serve(
        node_ws_handler,
        "0.0.0.0",
        NODE_WS_PORT,
        ping_interval=20,
        ping_timeout=30,
        close_timeout=10,
    ):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise

async def node_server_watchdog():
    global _node_server_started
    while True:
        try:
            await start_node_server()
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"Node server crashed: {e} — restarting in 5s")
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            return

# ─── CPU / cpuset helpers ─────────────────────────────────────────────────────

def _parse_cpuset_str(s):
    ids = []
    for part in s.strip().split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            lo, hi = part.split('-', 1)
            ids.extend(range(int(lo), int(hi) + 1))
        else:
            ids.append(int(part))
    return sorted(ids)

def _available_cpu_ids():
    cgroup_paths = [
        '/sys/fs/cgroup/cpuset.cpus.effective',
        '/sys/fs/cgroup/cpuset.cpus',
        '/sys/fs/cgroup/cpuset/docker/cpuset.cpus',
        '/sys/fs/cgroup/cpuset/cpuset.cpus',
    ]
    for path in cgroup_paths:
        try:
            txt = open(path).read().strip()
            if txt:
                ids = _parse_cpuset_str(txt)
                if ids:
                    return ids
        except Exception:
            pass
    try:
        return sorted(os.sched_getaffinity(0))
    except Exception:
        return list(range(os.cpu_count() or 1))

def _host_cpu_count():
    return len(_available_cpu_ids())

def build_cpuset(cpu_count):
    available = _available_cpu_ids()
    n = max(1, min(int(cpu_count), len(available)))
    return ",".join(str(c) for c in available[:n])

# ─── Fake hardware (Docker containers) ────────────────────────────────────────

def generate_fake_cpuinfo(cpu_count, model_name=FAKE_CPU_MODEL):
    n = max(1, int(cpu_count))
    entries = []
    for i in range(n):
        entries.append(
            f"processor\t: {i}\n"
            f"vendor_id\t: {FAKE_CPU_VENDOR}\n"
            f"cpu family\t: {FAKE_CPU_FAMILY}\n"
            f"model\t\t: {FAKE_CPU_MODEL_NUMBER}\n"
            f"model name\t: {model_name}\n"
            f"stepping\t: {FAKE_CPU_STEPPING}\n"
            "microcode\t: 0x0a601206\n"
            f"cpu MHz\t\t: {FAKE_CPU_MHZ}\n"
            f"cache size\t: {FAKE_CPU_CACHE}\n"
            f"physical id\t: 0\n"
            f"siblings\t: {n}\n"
            f"core id\t\t: {i}\n"
            f"cpu cores\t: {n}\n"
            "apicid\t\t: 0\n"
            "fpu\t\t: yes\n"
            "fpu_exception\t: yes\n"
            "cpuid level\t: 16\n"
            "wp\t\t: yes\n"
            "bogomips\t: 7398.00\n"
            "clflush size\t: 64\n"
            "cache_alignment\t: 64\n"
            "address sizes\t: 48 bits physical, 48 bits virtual\n"
            "power management: ts ttp tm hwpstate cpb eff_freq_ro [13] [14]\n"
        )
    return "\n".join(entries)

def generate_fake_meminfo(ram_mb):
    total_kb = max(1, int(ram_mb)) * 1024
    free_kb = int(total_kb * 0.72)
    available_kb = int(total_kb * 0.82)
    buffers_kb = int(total_kb * 0.02)
    cached_kb = int(total_kb * 0.10)
    return (
        f"MemTotal:       {total_kb:>10} kB\n"
        f"MemFree:        {free_kb:>10} kB\n"
        f"MemAvailable:   {available_kb:>10} kB\n"
        f"Buffers:        {buffers_kb:>10} kB\n"
        f"Cached:         {cached_kb:>10} kB\n"
        "SwapCached:              0 kB\n"
        "Active:                  0 kB\n"
        "Inactive:                0 kB\n"
        "SwapTotal:               0 kB\n"
        "SwapFree:                0 kB\n"
        "Dirty:                   0 kB\n"
        "Writeback:               0 kB\n"
        f"AnonPages:      {int(total_kb * 0.14):>10} kB\n"
        "Mapped:                  0 kB\n"
        "Shmem:                   0 kB\n"
        f"Slab:           {int(total_kb * 0.02):>10} kB\n"
    )

def build_fakehw_apply_script(ram_mb, cpu_count, disk_gb=30):
    cpuinfo = generate_fake_cpuinfo(cpu_count, FAKE_CPU_MODEL)
    meminfo = generate_fake_meminfo(ram_mb)
    disk_gb = max(1, int(disk_gb))
    used_gb = max(1, disk_gb // 6)
    avail_gb = disk_gb - used_gb
    used_pct = (used_gb * 100) // disk_gb
    return (
        "mkdir -p /etc/fakehw && "
        "cat > /etc/fakehw/cpuinfo <<'PYEOF'\n"
        f"{cpuinfo}"
        "PYEOF\n"
        "cat > /etc/fakehw/meminfo <<'PYEOF'\n"
        f"{meminfo}"
        "PYEOF\n"
        "if [ ! -f /usr/bin/df.real ]; then cp /usr/bin/df /usr/bin/df.real 2>/dev/null || true; fi && "
        "cat > /usr/local/bin/df <<'PYEOF'\n"
        "#!/bin/bash\n"
        f"FAKE_SIZE='{disk_gb}G'\n"
        f"FAKE_USED='{used_gb}.0G'\n"
        f"FAKE_AVAIL='{avail_gb}G'\n"
        f"FAKE_PCT='{used_pct}%'\n"
        "needs_fake=false\n"
        "for arg in \"$@\"; do\n"
        "    if [[ \"$arg\" == \"/\" ]]; then needs_fake=true; break; fi\n"
        "done\n"
        "if $needs_fake; then\n"
        "    echo 'Filesystem      Size  Used Avail Use% Mounted on'\n"
        "    echo \"/dev/vda1       $FAKE_SIZE  $FAKE_USED  $FAKE_AVAIL   $FAKE_PCT /\"\n"
        "else\n"
        "    exec /usr/bin/df.real \"$@\"\n"
        "fi\n"
        "PYEOF\n"
        "chmod +x /usr/local/bin/df && "
        "mkdir -p /root/.config/neofetch && "
        "cat > /root/.config/neofetch/config.conf <<'PYEOF'\n"
        "print_info() {\n"
        "    info title\n"
        "    info '--------' separator\n"
        "    info 'OS' distro\n"
        "    info 'Host' model\n"
        "    info 'Kernel' kernel\n"
        "    info 'Uptime' uptime\n"
        "    info 'Packages' packages\n"
        "    info 'Shell' shell\n"
        "    info 'CPU' cpu\n"
        "    info 'Memory' memory\n"
        "    info cols\n"
        "}\n"
        "PYEOF\n"
        "cat > /usr/local/bin/fakehw-apply.sh <<'PYEOF'\n"
        "#!/bin/bash\n"
         "mountpoint -q /proc/cpuinfo || mount --bind /etc/fakehw/cpuinfo /proc/cpuinfo || true\n"
         "mountpoint -q /proc/meminfo || mount --bind /etc/fakehw/meminfo /proc/meminfo || true\n"
        "PYEOF\n"
        "chmod +x /usr/local/bin/fakehw-apply.sh && "
         "cat > /usr/local/bin/cpufetch <<'PYEOF'\n"
         "#!/bin/bash\n"
         f"echo 'CPU        : {FAKE_CPU_MODEL}'\n"
         "echo 'Vendor     : AuthenticAMD'\n"
         f"echo 'CPU cores  : {cpu_count}'\n"
         "PYEOF\n"
         "chmod +x /usr/local/bin/cpufetch && "
         "cat > /usr/local/bin/neofetch <<'PYEOF'\n"
         "#!/bin/bash\n"
         "if [ -x /usr/bin/neofetch ]; then /usr/bin/neofetch \"$@\"; fi\n"
         f"echo 'CPU: {FAKE_CPU_MODEL}'\n"
         "PYEOF\n"
         "chmod +x /usr/local/bin/neofetch && "
         "cat > /usr/local/bin/fastfetch <<'PYEOF'\n"
         "#!/bin/bash\n"
         "if [ -x /usr/bin/fastfetch ]; then /usr/bin/fastfetch \"$@\"; fi\n"
         f"echo 'CPU: {FAKE_CPU_MODEL}'\n"
         "PYEOF\n"
         "chmod +x /usr/local/bin/fastfetch && "
         "cat > /usr/local/bin/lscpu <<'PYEOF'\n"
         "#!/bin/bash\n"
         "if [ -x /usr/bin/lscpu ]; then\n"
         f"  /usr/bin/lscpu \"$@\" | sed -E \"s/^(Model name[[:space:]]*:).*/\\1 {FAKE_CPU_MODEL}/; "
         f"s/^(Model[[:space:]]*:).*/\\1 {FAKE_CPU_MODEL}/; "
         "s/^(Vendor ID[[:space:]]*:).*/\\1 AuthenticAMD/\"\n"
         "else\n"
         f"  echo 'Model name: {FAKE_CPU_MODEL}'\n"
         f"  echo 'Vendor ID: {FAKE_CPU_VENDOR}'\n"
         "fi\n"
         "PYEOF\n"
         "chmod +x /usr/local/bin/lscpu && "
         "cat > /usr/local/bin/cpu-model <<'PYEOF'\n"
         "#!/bin/bash\n"
         f"echo '{FAKE_CPU_MODEL}'\n"
         "PYEOF\n"
         "chmod +x /usr/local/bin/cpu-model && "
        "cat > /etc/systemd/system/fakehw.service <<'PYEOF'\n"
        "[Unit]\n"
        "Description=Apply fake VPS hardware info\n"
        "DefaultDependencies=no\n"
        "Before=sysinit.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/usr/local/bin/fakehw-apply.sh\n"
        "RemainAfterExit=yes\n"
        "\n"
        "[Install]\n"
        "WantedBy=sysinit.target\n"
        "PYEOF\n"
        "systemctl daemon-reload && "
        "systemctl enable fakehw.service >/dev/null 2>&1 && "
        "systemctl start fakehw.service"
    )

async def apply_fake_hardware(node, container_name, ram_mb, cpu_count, disk_gb=30):
    script = build_fakehw_apply_script(ram_mb, cpu_count, disk_gb)
    stdout, stderr, rc = await routed_docker_exec(
        node, container_name, script, timeout=90
    )
    if rc != 0:
        raise Exception(stderr or f"hardware update exited with code {rc}")

async def apply_fake_hardware_lxc(node, container_name, ram_mb, cpu_count, disk_gb=30):
    """Apply the same hardware presentation update inside an LXC container."""
    script = build_fakehw_apply_script(ram_mb, cpu_count, disk_gb)
    # This script is local to the container and does not need DNS repair or
    # bridge reconfiguration. Avoiding that work keeps bulk updates fast.
    if node == "local":
        stdout, stderr, rc = await lxc_exec(container_name, script, timeout=90)
    else:
        result = await remote_execute_docker(
            node,
            f"lxc-attach -n {shlex.quote(container_name)} -- bash -lc {shlex.quote(script)}",
            timeout=90,
        )
        stdout, stderr, rc = str(result or ""), "", 0
    if rc != 0:
        raise Exception(stderr or f"LXC hardware update exited with code {rc}")

# ─── Docker VPS container creation ────────────────────────────────────────────

async def allocate_ssh_port(node="local", preferred=None):
    """Return a free host TCP port for publishing a VPS container's SSH port 22."""
    start = int(preferred or SSH_PORT_START)
    if start < 1024:
        start = SSH_PORT_START
    # Check both listening sockets and Docker's published port mappings.
    for port in range(start, start + 1000):
        check = (
            f"if ss -ltnH 2>/dev/null | awk '{'{print $4}'}' | grep -qE '(:|\\.){port}$'; then exit 1; fi; "
            f"if docker ps --format '{{{{.Ports}}}}' | grep -Eq ':{port}->'; then exit 1; fi; "
            "exit 0"
        )
        try:
            await routed_execute_docker(node, check, timeout=10)
            return port
        except Exception:
            continue
    raise Exception(f"No free SSH port available in {start}-{start + 999} on node `{node}`.")


async def create_docker_container(container_name, ram_mb, cpu_count, ssh_port, password, disk_gb=30, node="local"):
    if not ssh_port or int(ssh_port) < 1024:
        ssh_port = await allocate_ssh_port(node)
    ssh_port = int(ssh_port)
    try:
        await routed_execute_docker(node, f"docker pull {DOCKER_IMAGE}", timeout=300)
    except Exception:
        pass

    try:
        status_out = await routed_execute_docker(
            node,
            f"docker inspect --format='{{{{.State.Status}}}}' {container_name}",
            timeout=10
        )
        existing_status = (status_out or "").strip().strip("'\"")
        if existing_status == "running":
            raise Exception(f"Container `{container_name}` is already running.")
        elif existing_status:
            await remove_docker_container(node, container_name)
    except Exception as e:
        if "already running" in str(e):
            raise

    cpuset = build_cpuset(cpu_count)
    run_cmd = (
        f"docker run -d "
        f"--name {container_name} "
        f"--memory={ram_mb}m "
        f"--cpus={cpu_count} "
        f"--cpuset-cpus={cpuset} "
        f"-p {ssh_port}:22/tcp "
        f"--restart=unless-stopped "
        f"--privileged "
        f"--cgroupns=host "
        f"-v /sys/fs/cgroup:/sys/fs/cgroup:rw "
        f"--security-opt seccomp=unconfined "
        f"{DOCKER_IMAGE} "
        f"/sbin/init"
    )
    await routed_execute_docker(node, run_cmd, timeout=60)
    await asyncio.sleep(5)

    setup_script = (
        "apt-get update -qq && "
        "apt-get install -y openssh-server curl xz-utils -qq && "
        "mkdir -p /var/run/sshd && "
        "echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config && "
        "echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config && "
        f"echo 'root:{password}' | chpasswd && "
        "systemctl enable ssh && "
        "systemctl start ssh && "
        "curl -fsSL https://github.com/tmate-io/tmate/releases/download/2.4.0/tmate-2.4.0-static-linux-amd64.tar.xz "
        "-o /tmp/tmate.tar.xz 2>/dev/null && "
        "tar -xJf /tmp/tmate.tar.xz -C /tmp 2>/dev/null && "
        "mv /tmp/tmate-2.4.0-static-linux-amd64/tmate /usr/local/bin/tmate 2>/dev/null && "
        "chmod +x /usr/local/bin/tmate 2>/dev/null || true"
    )
    stdout, stderr, rc = await routed_docker_exec(node, container_name, setup_script, timeout=180)
    if rc != 0 and "already" not in stderr.lower():
        raise Exception(f"SSH setup failed: {stderr}")

    await apply_fake_hardware(node, container_name, ram_mb, cpu_count, disk_gb)
    # Verify sshd is running before reporting success. Use pgrep so this does
    # not depend on the optional `ss` command being installed in the image.
    await routed_docker_exec(
        node, container_name,
        "sshd -t && (systemctl restart ssh || service ssh restart || true) && "
        "pgrep -x sshd >/dev/null || pgrep -f '/usr/sbin/sshd' >/dev/null",
        timeout=20
    )
    return ssh_port

async def get_tmate_session(container_name, node="local"):
    socket_path = f"/tmp/gunpoint-{container_name}-{uuid.uuid4().hex[:10]}.tmate.sock"
    session_name = f"gunpoint-{container_name}-{uuid.uuid4().hex[:8]}"
    tmate_script = (
        "set -eu; "
        "if ! command -v curl >/dev/null 2>&1; then "
        "  apt-get update -qq && apt-get install -y curl xz-utils -qq; "
        "fi && "
        "if ! command -v tmate >/dev/null 2>&1; then "
        "  curl -fsSL https://github.com/tmate-io/tmate/releases/download/2.4.0/tmate-2.4.0-static-linux-amd64.tar.xz "
        "    -o /tmp/tmate.tar.xz && "
        "  tar -xJf /tmp/tmate.tar.xz -C /tmp && "
        "  mv /tmp/tmate-2.4.0-static-linux-amd64/tmate /usr/local/bin/tmate && "
        "  chmod +x /usr/local/bin/tmate; "
        "fi && "
        f"rm -f {shlex.quote(socket_path)} && "
        f"tmate -S {shlex.quote(socket_path)} new-session -d -s "
        f"{shlex.quote(session_name)} && "
        "for _i in 1 2 3 4 5 6 7 8 9 10; do "
        f"  tmate -S {shlex.quote(socket_path)} wait tmate-ready >/dev/null 2>&1 && break; "
        "  sleep 1; "
        "done && "
        f"tmate -S {shlex.quote(socket_path)} display -p '#{{tmate_ssh}}'"
    )
    stdout, stderr, rc = await routed_docker_exec(node, container_name, tmate_script, timeout=120)
    if rc != 0 or not stdout.strip():
        raise Exception(f"tmate session start failed: {stderr or stdout or 'no SSH command returned'}")
    match = re.search(r"(ssh\s+\S+@\S+)", stdout)
    return match.group(1) if match else stdout.strip()

async def get_sshx_session(container_name, node="local"):
    """Create a real sshx browser terminal inside a Docker VPS.

    sshx is an outbound relay, so the Docker VM does NOT need an inbound
    public IPv4 address. The official CI/non-interactive invocation is
    `curl -sSf https://sshx.io/get | sh -s run`.
    """
    log_path = f"/tmp/gunpoint-{container_name}-{uuid.uuid4().hex[:10]}.sshx.log"
    qlog = shlex.quote(log_path)

    sshx_script = f'''set -eu

if ! command -v curl >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl >/dev/null
fi

curl -fsS --connect-timeout 10 --max-time 20 https://sshx.io/ >/dev/null

if [ -f {qlog}.pid ]; then
    old_pid=$(cat {qlog}.pid 2>/dev/null || true)
    if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
        kill "$old_pid" 2>/dev/null || true
    fi
fi
rm -f {qlog} {qlog}.pid

nohup sh -c 'curl -sSf https://sshx.io/get | sh -s run' \
    >{qlog} 2>&1 < /dev/null &
echo $! > {qlog}.pid

url=""
for _i in $(seq 1 45); do
    if [ -s {qlog} ]; then
        url=$(grep -Eo 'https://sshx\\.io/s/[^[:space:]<>"'"'"']+' {qlog} | head -n 1 || true)
        if [ -n "$url" ]; then
            printf '%s\n' "$url"
            exit 0
        fi
    fi
    sleep 1
done

printf '%s\n' '--- SSHX LOG ---' >&2
cat {qlog} 2>/dev/null || true
printf '%s\n' '--- END SSHX LOG ---' >&2
exit 45
'''
    stdout, stderr, rc = await routed_docker_exec(
        node, container_name, sshx_script, timeout=75
    )
    output = "\n".join(x for x in (stdout or "", stderr or "") if x)
    match = re.search(r"https://sshx\.io/s/[^\s<>\"']+", output)
    if not match:
        match = re.search(r"sshx\.io/s/[^\s<>\"']+", output)
    if not match:
        short_log = (output or "No sshx output returned.").strip()
        if len(short_log) > 1800:
            short_log = short_log[-1800:]
        raise Exception(
            "SSHX could not create a browser session. "
            f"Container={container_name}, node={node}, exit={rc}.\n"
            f"Output:\n{short_log}"
        )
    url = match.group(0).rstrip(".,;)")
    return url if url.startswith("http") else "https://" + url

# ─── LXC Container Creation ───────────────────────────────────────────────────

def _is_cgroupv2() -> bool:
    """Return True if the host uses cgroup v2 (unified hierarchy)."""
    # cgroup v2 mounts cgroup2 at /sys/fs/cgroup; v1 mounts tmpfs there
    try:
        with open("/proc/mounts") as _f:
            for line in _f:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "/sys/fs/cgroup" and parts[2] == "cgroup2":
                    return True
    except OSError:
        pass
    return False

def build_lxc_config(container_name: str, ram_mb: int, cpu_count: int) -> str:
    """Build the LXC cgroup config snippet to append.
    Uses cgroup v2 keys on AlmaLinux 9 (unified hierarchy) and v1 keys
    on older hosts — never mixing both, which causes lxc-start to ABORT.
    """
    cpuset = ",".join(str(i) for i in _available_cpu_ids()[:cpu_count])
    if _is_cgroupv2():
        limits = (
            f"lxc.cgroup2.memory.max = {ram_mb}M\n"
            f"lxc.cgroup2.memory.swap.max = 0\n"
            f"lxc.cgroup2.cpuset.cpus = {cpuset}\n"
        )
    else:
        limits = (
            f"lxc.cgroup.memory.limit_in_bytes = {ram_mb}M\n"
            f"lxc.cgroup.cpuset.cpus = {cpuset}\n"
        )
    return f"\n# GunpointNodes resource limits\n{limits}"

async def create_lxc_container(container_name: str, ram_mb: int, cpu_count: int,
                                disk_gb: int, password: str, node: str = "local") -> bool:
    """
    Create an LXC container on AlmaLinux host.
    Uses Ubuntu 22.04 template for broad package and SSH compatibility.
    """
    distro_args = "-d ubuntu -r jammy -a amd64"

    if node == "local":
        # lxc-create resets PATH before invoking the template script, so
        # setting PATH on the lxc-create process itself does nothing for the
        # template.  Solution: pass an absolute-path wrapper script as the
        # template (-t /path/to/wrapper).  lxc-create invokes the wrapper,
        # the wrapper re-exports a full PATH, then exec's the real lxc-download
        # template — so wget/tar/gpg are all resolvable inside the template.
        wrapper_path = f"/tmp/lxc-tpl-{container_name}.sh"
        # Find the real download template (location varies by distro/version)
        real_template = None
        for candidate in (
            "/usr/share/lxc/templates/lxc-download",
            "/usr/lib/lxc/templates/lxc-download",
            "/usr/libexec/lxc/lxc-download",
        ):
            if os.path.isfile(candidate):
                real_template = candidate
                break
        if real_template is None:
            # Fall back to letting lxc-create search itself; may still fail
            real_template = "download"
            use_abs_template = False
        else:
            use_abs_template = True

        wrapper_script = (
            "#!/bin/bash\n"
            "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
            f"exec {real_template} \"$@\"\n"
        )
        with open(wrapper_path, "w") as _wf:
            _wf.write(wrapper_script)
        os.chmod(wrapper_path, 0o755)

        tpl_arg = wrapper_path if use_abs_template else "download"
        proc = await asyncio.create_subprocess_exec(
            "lxc-create", "-n", container_name, "-t", tpl_arg, "--",
            *distro_args.split(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                os.unlink(wrapper_path)
            except OSError:
                pass
            raise Exception("lxc-create timed out after 300 seconds")
        try:
            os.unlink(wrapper_path)
        except OSError:
            pass
        if proc.returncode != 0:
            raise Exception(stderr.decode().strip() or "lxc-create failed")
    else:
        create_cmd = f"lxc-create -n {container_name} -t download -- {distro_args}"
        await remote_execute_docker(node, create_cmd, timeout=300)

    # Append resource limit config
    config_path = f"/var/lib/lxc/{container_name}/config"
    config_snippet = build_lxc_config(container_name, ram_mb, cpu_count)
    # Escape for shell heredoc
    escaped = config_snippet.replace("'", "'\\''")
    append_cmd = f"printf '%s' '{escaped}' >> {config_path}"

    # Enable networking and container-local hardware presentation.
    networking_config = (
        "\n# Networking\n"
        "lxc.net.0.type = veth\n"
        "lxc.net.0.link = lxcbr0\n"
        "lxc.net.0.flags = up\n"
        "\n# Required for container-local hardware presentation\n"
        "lxc.apparmor.profile = unconfined\n"
        "lxc.cap.drop =\n"
    )
    net_escaped = networking_config.replace("'", "'\\''")
    net_cmd = f"printf '%s' '{net_escaped}' >> {config_path}"

    if node == "local":
        await execute_lxc(append_cmd, timeout=15)
        await execute_lxc(net_cmd, timeout=15)
    else:
        await remote_execute_docker(node, append_cmd, timeout=15)
        await remote_execute_docker(node, net_cmd, timeout=15)

    # Alma/RHEL hosts may require firewalld, forwarding, and dnsmasq setup.
    await ensure_lxc_bridge_network(node)

    # Start the container; on failure capture a debug log for a useful error msg
    if node == "local":
        log_path = f"/tmp/lxc-start-{container_name}.log"
        _start_proc = await asyncio.create_subprocess_exec(
            "lxc-start", "-n", container_name,
            "--logfile", log_path, "--logpriority", "DEBUG",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _out, _err = await asyncio.wait_for(_start_proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            _start_proc.kill()
            raise Exception("lxc-start timed out after 60 seconds")
        if _start_proc.returncode != 0:
            # Read the last 30 lines of the debug log for a useful error
            detail = _err.decode().strip()
            try:
                with open(log_path) as _lf:
                    lines = _lf.readlines()
                    log_tail = "".join(lines[-30:]).strip()
                    if log_tail:
                        detail = log_tail
            except OSError:
                pass
            try:
                os.unlink(log_path)
            except OSError:
                pass
            raise Exception(f"lxc-start failed:\n{detail}")
        try:
            os.unlink(log_path)
        except OSError:
            pass
    else:
        await remote_execute_docker(node, f"lxc-start -n {container_name}", timeout=60)

    await asyncio.sleep(5)

    # Setup: update, SSH, set password
    setup_script = (
        "export DEBIAN_FRONTEND=noninteractive && " +
        lxc_dns_repair_script() +
        "apt-get update -qq && "
        "apt-get install -y openssh-server curl wget ufw -qq || "
        "{ echo 'LXC package installation failed after DNS repair; check outbound network access.' >&2; exit 42; } && "
        "systemctl enable ssh && systemctl start ssh && "
        "echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config && "
        "echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config && "
        "systemctl restart ssh && "
        f"echo 'root:{password}' | chpasswd"
    )
    if node == "local":
        stdout, stderr, rc = await lxc_exec(container_name, setup_script, timeout=180)
    else:
        # Use remote execution
        full_cmd = f"lxc-attach -n {container_name} -- bash -c \"{setup_script.replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}\""
        await remote_execute_docker(node, full_cmd, timeout=180)

    return True

async def lxc_get_container_ip(container_name: str, node: str = "local") -> str:
    """Get the IP assigned to an LXC container."""
    if node == "local":
        try:
            result = await execute_lxc(
                f"lxc-info -n {container_name} -iH", timeout=10
            )
            return str(result).strip().split('\n')[0] if result else "Unknown"
        except Exception:
            return "Unknown"
    return "Unknown (remote)"

# ─── VPS Role helper ───────────────────────────────────────────────────────────

async def get_or_create_vps_role(guild):
    global VPS_USER_ROLE_ID
    if VPS_USER_ROLE_ID:
        role = guild.get_role(VPS_USER_ROLE_ID)
        if role:
            return role
    role = discord.utils.get(guild.roles, name="VPS User")
    if role:
        VPS_USER_ROLE_ID = role.id
        return role
    try:
        role = await guild.create_role(
            name="VPS User",
            color=discord.Color.dark_purple(),
            reason="VPS User role for bot management",
            permissions=discord.Permissions.none()
        )
        VPS_USER_ROLE_ID = role.id
        return role
    except Exception as e:
        logger.error(f"Failed to create VPS User role: {e}")
        return None

# ─── CPU Monitor ───────────────────────────────────────────────────────────────

def get_cpu_usage():
    try:
        load_1m = os.getloadavg()[0]
        return round(min(100.0, (load_1m / max(1, os.cpu_count() or 1)) * 100.0), 1)
    except Exception:
        return 0.0

def cpu_monitor():
    global cpu_monitor_active
    while cpu_monitor_active:
        try:
            cpu_usage = get_cpu_usage()
            if cpu_usage > CPU_THRESHOLD:
                # Monitoring is intentionally passive. The previous implementation
                # stopped every local Docker VPS on a busy host, which caused
                # outages and made the bot appear laggy after reboots.
                logger.warning(
                    f"Estimated host load ({cpu_usage}%) exceeded threshold; "
                    "no containers were stopped."
                )
            time.sleep(CHECK_INTERVAL)
        except Exception:
            time.sleep(CHECK_INTERVAL)

cpu_thread = threading.Thread(target=cpu_monitor, daemon=True)
cpu_thread.start()

# ─── Bot events ────────────────────────────────────────────────────────────────

BOT_START_TIME = datetime.now()
maintenance_mode = False

@bot.event
async def on_ready():
    global _node_server_started
    logger.info(f'{bot.user} has connected to Discord!')
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name="GunpointNodes | Infrastructure"))
    if not auto_expire_check.is_running():
        auto_expire_check.start()
    if not _node_server_started:
        _node_server_started = True
        global _watchdog_task
        _watchdog_task = asyncio.create_task(node_server_watchdog())
        try:
            await bot.tree.sync()
        except Exception as e:
            logger.warning(f"Failed to sync command tree: {e}")
    logger.info("Bot is ready!")

@bot.event
async def on_close():
    global _watchdog_task
    if _watchdog_task and not _watchdog_task.done():
        _watchdog_task.cancel()
        try:
            await _watchdog_task
        except asyncio.CancelledError:
            pass

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=create_error_embed("Missing Argument", "Please use `!help` for command usage."))
    elif isinstance(error, commands.BadArgument):
        await ctx.send(embed=create_error_embed("Invalid Argument", "Please check your input and try again."))
    elif isinstance(error, commands.CheckFailure):
        pass
    else:
        logger.error(f"Command error: {error}")
        await ctx.send(embed=create_error_embed("System Error", "An error occurred. Please try again."))

# ─── ManageView (Docker VPS) ──────────────────────────────────────────────────

class ManageView(discord.ui.View):
    def __init__(self, user_id, vps_list, is_shared=False, owner_id=None, is_admin=False):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.vps_list = vps_list
        self.selected_index = None
        self.is_shared = is_shared
        self.owner_id = owner_id or user_id
        self.is_admin = is_admin

        if len(vps_list) > 1:
            options = [
                discord.SelectOption(
                    label=f"VPS {i+1} — {v.get('plan', v.get('ram','?'))} RAM",
                    description=f"{status_badge(v.get('status','unknown')).replace('**','').strip()} · Node: {v.get('node','local')}",
                    value=str(i),
                    emoji="🟢" if v.get('status') == 'running' else "🔴"
                ) for i, v in enumerate(vps_list)
            ]
            self.select = discord.ui.Select(placeholder="⚡ Select a VPS to manage…", options=options)
            self.select.callback = self.select_vps
            self.add_item(self.select)
            self.initial_embed = self._build_overview_embed()
        else:
            self.selected_index = 0
            self.initial_embed = self.create_vps_embed(0)
            self.add_action_buttons()

    def _build_overview_embed(self):
        embed = create_embed(
            "VPS Manager — Select a Container",
            "Choose a VPS from the dropdown below to manage it.",
            C_PRIMARY
        )
        for i, v in enumerate(self.vps_list):
            node = v.get('node', 'local')
            embed.add_field(
                name=f"{'🟢' if v.get('status')=='running' else '🔴'}  VPS #{i+1}  ·  `{v['container_name']}`",
                value=(
                    f"**Status:** {status_badge(v.get('status','unknown'))}\n"
                    f"**Node:** {node_badge(node)}\n"
                    f"**Specs:** `{v.get('ram','?')}` RAM  ·  `{v.get('cpu','?')} Core(s)` CPU"
                ),
                inline=True
            )
        return embed

    def create_vps_embed(self, index):
        vps = self.vps_list[index]
        is_running = vps.get('status') == 'running'
        color = C_SUCCESS if is_running else C_ERROR
        node_name = vps.get('node', 'local')

        owner_text = ""
        if self.is_admin and self.owner_id != self.user_id:
            try:
                owner_user = bot.get_user(int(self.owner_id))
                owner_text = f"\n> **Owner:** {owner_user.mention}" if owner_user else ""
            except Exception:
                pass

        expires = vps.get('expires')
        if expires and expires != "Never":
            try:
                exp_dt = datetime.fromisoformat(expires)
                days_left = (exp_dt - datetime.utcnow()).days
                if days_left < 0:
                    expire_str = f"❌ **EXPIRED** {abs(days_left)}d ago"
                elif days_left <= 3:
                    expire_str = f"⚠️ Expires in **{days_left}d** ({expires[:10]})"
                else:
                    expire_str = f"✅ `{expires[:10]}` ({days_left}d left)"
            except Exception:
                expire_str = expires
        else:
            expire_str = "♾️ Never"

        try:
            ram_gb = int(str(vps.get('ram', '1GB')).replace('GB', ''))
            cpu_c = int(vps.get('cpu', 1))
            disk_gb = int(str(vps.get('storage', '30GB')).replace('GB', ''))
            res_bar = resource_bar_field(ram_gb, cpu_c, disk_gb)
        except Exception:
            res_bar = f"`{vps.get('ram','?')}` RAM  ·  `{vps.get('cpu','?')}` CPU  ·  `{vps.get('storage','?')}` Disk"

        embed = create_embed(
            f"Docker VPS  ·  #{index + 1}  ·  `{vps['container_name']}`",
            f"{status_badge(vps.get('status','unknown'))}  ·  Node: {node_badge(node_name)}{owner_text}",
            color
        )
        embed.add_field(name="📊 Resources", value=res_bar, inline=False)
        embed.add_field(name="📅 Created",   value=f"`{vps.get('created_at','?')[:10]}`", inline=True)
        embed.add_field(name="⏳ Expires",   value=expire_str,                             inline=True)
        embed.add_field(name="🗂️ Plan",      value=f"`{vps.get('plan','Custom')}`",        inline=True)
        if vps.get('nickname'):
            embed.add_field(name="🏷️ Nickname", value=f"`{vps['nickname']}`", inline=True)
        if vps.get('note'):
            embed.add_field(name="📝 Note", value=vps['note'][:200], inline=False)
        embed.add_field(name="🎮 Controls", value="Use the buttons below to manage your VPS.", inline=False)
        return embed

    def add_action_buttons(self):
        if not self.is_shared and not self.is_admin:
            reinstall_btn = discord.ui.Button(label="♻️ Reinstall", style=discord.ButtonStyle.danger, row=1)
            reinstall_btn.callback = lambda i: self.action_callback(i, 'reinstall')
            self.add_item(reinstall_btn)

        start_btn = discord.ui.Button(label="▶  Start",  style=discord.ButtonStyle.success,   row=0)
        stop_btn  = discord.ui.Button(label="⏹  Stop",   style=discord.ButtonStyle.secondary,  row=0)
        ssh_btn   = discord.ui.Button(label="🔑  SSH",   style=discord.ButtonStyle.primary,    row=0)
        sshx_btn  = discord.ui.Button(label="🌐  SSHX",  style=discord.ButtonStyle.secondary,  row=0)
        delete_btn = discord.ui.Button(label="🗑️  Delete", style=discord.ButtonStyle.danger, row=1)

        start_btn.callback = lambda i: self.action_callback(i, 'start')
        stop_btn.callback  = lambda i: self.action_callback(i, 'stop')
        ssh_btn.callback   = lambda i: self.action_callback(i, 'ssh')
        sshx_btn.callback  = lambda i: self.action_callback(i, 'sshx')
        delete_btn.callback = lambda i: self.action_callback(i, 'delete')

        self.add_item(start_btn)
        self.add_item(stop_btn)
        self.add_item(ssh_btn)
        self.add_item(sshx_btn)
        if not self.is_shared:
            self.add_item(delete_btn)

    async def select_vps(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.user_id and not self.is_admin:
            await interaction.response.send_message(
                embed=create_error_embed("Access Denied", "This is not your VPS panel."), ephemeral=True)
            return
        self.selected_index = int(self.select.values[0])
        new_embed = self.create_vps_embed(self.selected_index)
        self.clear_items()
        self.add_action_buttons()
        await interaction.response.edit_message(embed=new_embed, view=self)

    async def action_callback(self, interaction: discord.Interaction, action: str):
        if str(interaction.user.id) != self.user_id and not self.is_admin:
            await interaction.response.send_message(
                embed=create_error_embed("Access Denied", "This is not your VPS panel."), ephemeral=True)
            return

        vps = vps_data[self.owner_id][self.selected_index] if self.is_shared else self.vps_list[self.selected_index]
        container_name = vps["container_name"]
        node = vps.get("node", "local")

        if action == 'reinstall':
            if self.is_shared or self.is_admin:
                await interaction.response.send_message(
                    embed=create_error_embed("Access Denied", "Only the VPS owner can reinstall."), ephemeral=True)
                return

            confirm_embed = create_warning_embed(
                "Confirm Reinstall",
                f"This will **erase all data** on `{container_name}` and reinstall Ubuntu 22.04.\n\n"
                f"⚠️ This action is **irreversible**. Are you sure?"
            )

            class ConfirmView(discord.ui.View):
                def __init__(self2, parent_view, container_name, vps, owner_id, selected_index, node):
                    super().__init__(timeout=60)
                    self2.parent_view = parent_view
                    self2.container_name = container_name
                    self2.vps = vps
                    self2.owner_id = owner_id
                    self2.selected_index = selected_index
                    self2.node = node

                @discord.ui.button(label="Yes, Reinstall", style=discord.ButtonStyle.danger)
                async def confirm(self2, interaction: discord.Interaction, item: discord.ui.Button):
                    await interaction.response.defer(ephemeral=True)
                    try:
                        await interaction.followup.send(
                            embed=create_info_embed("Wiping Container", f"Removing `{self2.container_name}`…"),
                            ephemeral=True)
                        try:
                            await routed_execute_docker(
                                self2.node,
                                f"docker stop {shlex.quote(self2.container_name)}",
                                timeout=30,
                            )
                        except Exception:
                            pass
                        await remove_docker_container(self2.node, self2.container_name)
                        await interaction.followup.send(
                            embed=create_info_embed("Rebuilding", "Deploying fresh container…"), ephemeral=True)
                        ram_mb = int(self2.vps["ram"].replace("GB", "")) * 1024
                        disk = int(self2.vps.get("storage", "30GB").replace("GB", ""))
                        new_pw = generate_password()
                        old_port = int(self2.vps.get("ssh_port", 0) or 0)
                        new_port = await create_docker_container(
                            self2.container_name, ram_mb, int(self2.vps["cpu"]), old_port,
                            new_pw, disk_gb=disk, node=self2.node
                        )
                        self2.vps["ssh_port"] = new_port
                        self2.vps["status"] = "running"
                        self2.vps["ssh_password"] = new_pw
                        self2.vps["created_at"] = datetime.now().isoformat()
                        await async_save_data()
                        await interaction.followup.send(
                            embed=create_success_embed("Reinstall Complete",
                                f"VPS `{self2.container_name}` is back online with a fresh install!\n"
                                f"**New Password:** `{new_pw}`\n**SSH Port:** `{new_port}`"),
                            ephemeral=True)
                        if not self2.parent_view.is_shared:
                            await interaction.message.edit(
                                embed=self2.parent_view.create_vps_embed(self2.parent_view.selected_index),
                                view=self2.parent_view)
                    except Exception as e:
                        await interaction.followup.send(
                            embed=create_error_embed("Reinstall Failed", str(e)), ephemeral=True)

                @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
                async def cancel(self2, interaction: discord.Interaction, item: discord.ui.Button):
                    await interaction.response.edit_message(
                        embed=self2.parent_view.create_vps_embed(self2.parent_view.selected_index),
                        view=self2.parent_view)

            await interaction.response.send_message(
                embed=confirm_embed,
                view=ConfirmView(self, container_name, vps, self.owner_id, self.selected_index, node),
                ephemeral=True)

        elif action == 'start':
            await interaction.response.defer(ephemeral=True)
            try:
                await routed_execute_docker(node, f"docker start {container_name}")
                await asyncio.sleep(5)
                await routed_docker_exec(node, container_name, "systemctl start ssh || /usr/sbin/sshd || true", timeout=15)
                vps["status"] = "running"
                await async_save_data()
                await interaction.followup.send(
                    embed=create_success_embed("VPS Started", f"`{container_name}` is now running!"), ephemeral=True)
                await interaction.message.edit(embed=self.create_vps_embed(self.selected_index), view=self)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Start Failed", str(e)), ephemeral=True)

        elif action == 'stop':
            await interaction.response.defer(ephemeral=True)
            try:
                await routed_execute_docker(node, f"docker stop {container_name}", timeout=120)
                vps["status"] = "stopped"
                await async_save_data()
                await interaction.followup.send(
                    embed=create_success_embed("VPS Stopped", f"`{container_name}` has been stopped."), ephemeral=True)
                await interaction.message.edit(embed=self.create_vps_embed(self.selected_index), view=self)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Stop Failed", str(e)), ephemeral=True)

        elif action == 'ssh':
            await interaction.response.defer(ephemeral=True)
            try:
                ssh_password = vps.get("ssh_password")
                if not ssh_password:
                    await interaction.followup.send(
                        embed=create_error_embed("SSH Error", "No credentials found. Reinstall the VPS."),
                        ephemeral=True)
                    return
                await interaction.followup.send(
                    embed=create_info_embed("Generating tmate Session", "Connecting to tmate relay…"), ephemeral=True)
                tmate_cmd = await get_tmate_session(container_name, node=node)
                ssh_embed = create_embed(
                    "SSH Access Ready",
                    f"Secure tunnel created for `{container_name}`",
                    C_SUCCESS
                )
                ssh_embed.add_field(name="🔗 SSH Command",  value=f"```bash\n{tmate_cmd}\n```", inline=False)
                ssh_embed.add_field(name="🔑 Root Password", value=f"```\n{ssh_password}\n```",  inline=True)
                ssh_embed.add_field(name="🌐 Node",          value=node_badge(node),             inline=True)
                ssh_embed.add_field(
                    name="📌 Quick Start",
                    value="**1.** Copy the SSH command\n**2.** Paste it in any terminal (CMD/Termux/PuTTY)\n**3.** Enter your password when prompted",
                    inline=False
                )
                ssh_embed.add_field(
                    name="⚠️ Important",
                    value="• Session expires on VPS restart — click SSH again for a new link\n• Change your password after first login for security",
                    inline=False
                )
                try:
                    await interaction.user.send(embed=ssh_embed)
                    await interaction.followup.send(
                        embed=create_success_embed("SSH Credentials Sent", "Check your **DMs** for your SSH command!"),
                        ephemeral=True)
                except discord.Forbidden:
                    await interaction.followup.send(
                        embed=create_error_embed("DM Failed", "Enable server DMs so I can send your credentials."),
                        ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("SSH Error", str(e)), ephemeral=True)

        elif action == 'sshx':
            await interaction.response.defer(ephemeral=True)
            try:
                await interaction.followup.send(
                    embed=create_info_embed("Generating Browser Session", "Starting sshx relay…"), ephemeral=True)
                sshx_url = await get_sshx_session(container_name, node=node)
                sshx_embed = create_embed(
                    "Browser Terminal Ready",
                    f"Web-based terminal for `{container_name}`",
                    C_PURPLE
                )
                sshx_embed.add_field(name="🔗 Browser Link", value=f"```\n{sshx_url}\n```", inline=False)
                sshx_embed.add_field(name="🌐 Node", value=node_badge(node), inline=True)
                sshx_embed.add_field(
                    name="📌 How to Use",
                    value="**1.** Copy the link above\n**2.** Open it in any browser\n**3.** Full terminal — no SSH client needed!",
                    inline=False
                )
                try:
                    await interaction.user.send(embed=sshx_embed)
                    await interaction.followup.send(
                        embed=create_success_embed("Browser Link Sent", "Check your **DMs** for the terminal link!"),
                        ephemeral=True)
                except discord.Forbidden:
                    await interaction.followup.send(
                        embed=create_error_embed("DM Failed", "Enable server DMs so I can send your link."),
                        ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("SSHX Error", str(e)), ephemeral=True)

        elif action == 'delete':
            await interaction.response.defer(ephemeral=True)
            try:
                await remove_docker_container(node, container_name)
                uid = self.owner_id
                owner_vps = vps_data.get(uid, [])
                vps_data[uid] = [
                    item for item in owner_vps
                    if item.get("container_name") != container_name
                ]
                if not vps_data[uid]:
                    del vps_data[uid]
                await async_save_data()
                await interaction.followup.send(
                    embed=create_success_embed(
                        "VPS Deleted", f"`{container_name}` has been removed."
                    ),
                    ephemeral=True,
                )
                self.stop()
                await interaction.message.edit(
                    embed=create_info_embed("VPS Deleted", f"`{container_name}` was removed."),
                    view=None,
                )
            except Exception as e:
                await interaction.followup.send(
                    embed=create_error_embed("Delete Failed", str(e)), ephemeral=True
                )

# ─── LXC ManageView ───────────────────────────────────────────────────────────

class LXCManageView(discord.ui.View):
    def __init__(self, user_id: str, lxc_list: list, is_admin: bool = False, owner_id: str = None):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.lxc_list = lxc_list
        self.is_admin = is_admin
        self.owner_id = owner_id or user_id
        self.selected_index = 0

        if len(lxc_list) > 1:
            options = [
                discord.SelectOption(
                    label=f"LXC #{i+1} — {c.get('ram','?')} RAM",
                    description=f"{'Running' if c.get('status')=='running' else 'Stopped'} · {c.get('container_name','')}",
                    value=str(i),
                    emoji="🟢" if c.get('status') == 'running' else "🔴"
                ) for i, c in enumerate(lxc_list)
            ]
            sel = discord.ui.Select(placeholder="📦 Select an LXC container…", options=options)
            sel.callback = self.select_lxc
            self.add_item(sel)
        self.initial_embed = self.create_embed(0)
        self.add_buttons()

    def create_embed(self, index: int) -> discord.Embed:
        c = self.lxc_list[index]
        is_running = c.get('status') == 'running'
        node = c.get('node', 'local')

        try:
            ram_gb  = int(str(c.get('ram', '1GB')).replace('GB', ''))
            cpu_c   = int(c.get('cpu', 1))
            disk_gb = int(str(c.get('storage', '30GB')).replace('GB', ''))
            res_bar = resource_bar_field(ram_gb, cpu_c, disk_gb)
        except Exception:
            res_bar = f"`{c.get('ram','?')}` RAM  ·  `{c.get('cpu','?')}` CPU"

        embed = create_lxc_embed(
            f"LXC Container  ·  #{index + 1}  ·  `{c['container_name']}`",
            f"{status_badge(c.get('status','unknown'))}  ·  Node: {node_badge(node)}"
        )
        embed.add_field(name="📊 Resources",  value=res_bar,                               inline=False)
        embed.add_field(name="📅 Created",    value=f"`{c.get('created_at','?')[:10]}`",   inline=True)
        embed.add_field(name="⏳ Expires",    value=c.get('expires', '♾️ Never'),           inline=True)
        embed.add_field(
            name="📌 Connect",
            value="Use **SSH** for a terminal command or **SSHX** for a browser terminal.\n"
                  "The bot creates a temporary relay; no container IP or root password is exposed.",
            inline=False
        )
        return embed

    def add_buttons(self):
        start_btn  = discord.ui.Button(label="▶  Start",         style=discord.ButtonStyle.success,   row=1)
        stop_btn   = discord.ui.Button(label="⏹  Stop",          style=discord.ButtonStyle.secondary,  row=1)
        ssh_btn    = discord.ui.Button(label="🔑  SSH",          style=discord.ButtonStyle.primary,    row=1)
        sshx_btn   = discord.ui.Button(label="🌐  SSHX",         style=discord.ButtonStyle.secondary,  row=1)
        del_btn    = discord.ui.Button(label="🗑️  Delete",        style=discord.ButtonStyle.danger,     row=1)

        start_btn.callback = lambda i: self.action(i, 'start')
        stop_btn.callback  = lambda i: self.action(i, 'stop')
        ssh_btn.callback   = lambda i: self.action(i, 'ssh')
        sshx_btn.callback  = lambda i: self.action(i, 'sshx')
        del_btn.callback   = lambda i: self.action(i, 'delete')

        self.add_item(start_btn)
        self.add_item(stop_btn)
        self.add_item(ssh_btn)
        self.add_item(sshx_btn)
        # Owners can remove their own container; admins can remove containers
        # opened through an admin management panel as well.
        self.add_item(del_btn)

    async def select_lxc(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.user_id and not self.is_admin:
            await interaction.response.send_message(
                embed=create_error_embed("Access Denied", "This is not your panel."), ephemeral=True)
            return
        self.selected_index = int(interaction.data["values"][0])
        await interaction.response.edit_message(embed=self.create_embed(self.selected_index), view=self)

    async def action(self, interaction: discord.Interaction, action: str):
        if str(interaction.user.id) != self.user_id and not self.is_admin:
            await interaction.response.send_message(
                embed=create_error_embed("Access Denied", "This is not your panel."), ephemeral=True)
            return
        c = self.lxc_list[self.selected_index]
        container_name = c['container_name']
        node = c.get('node', 'local')

        if action == 'start':
            await interaction.response.defer(ephemeral=True)
            try:
                await routed_lxc_host_command(
                    node, f"lxc-start -n {shlex.quote(container_name)}", timeout=30
                )
                c['status'] = 'running'
                await async_save_data()
                await interaction.followup.send(
                    embed=create_success_embed("LXC Started", f"`{container_name}` is now running!"), ephemeral=True)
                await interaction.message.edit(embed=self.create_embed(self.selected_index), view=self)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Start Failed", str(e)), ephemeral=True)

        elif action == 'stop':
            await interaction.response.defer(ephemeral=True)
            try:
                await routed_lxc_host_command(
                    node, f"lxc-stop -n {shlex.quote(container_name)}", timeout=30
                )
                c['status'] = 'stopped'
                await async_save_data()
                await interaction.followup.send(
                    embed=create_success_embed("LXC Stopped", f"`{container_name}` has been stopped."), ephemeral=True)
                await interaction.message.edit(embed=self.create_embed(self.selected_index), view=self)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Stop Failed", str(e)), ephemeral=True)

        elif action == 'ssh':
            await interaction.response.defer(ephemeral=True)
            try:
                await interaction.followup.send(
                    embed=create_info_embed(
                        "Generating SSH Session",
                        "Creating a temporary secure terminal relay…"
                    ),
                    ephemeral=True
                )
                ssh_command = await get_lxc_tmate_session(container_name, node)
                ssh_embed = create_lxc_embed(
                    "SSH Access Ready",
                    f"Temporary terminal relay created for `{container_name}`."
                )
                ssh_embed.add_field(
                    name="🔗 SSH Command",
                    value=f"```bash\n{ssh_command}\n```",
                    inline=False
                )
                ssh_embed.add_field(
                    name="📌 How to Use",
                    value="Copy the command into CMD, Termux, PuTTY, or any SSH terminal. "
                          "This relay avoids exposing the LXC IP and password.",
                    inline=False
                )
                try:
                    await interaction.user.send(embed=ssh_embed)
                    await interaction.followup.send(
                        embed=create_success_embed(
                            "SSH Command Sent",
                            "Check your DMs for the temporary SSH command."
                        ),
                        ephemeral=True
                    )
                except discord.Forbidden:
                    await interaction.followup.send(
                        embed=create_error_embed(
                            "DM Failed",
                            "Enable server DMs so the bot can send your SSH command."
                        ),
                        ephemeral=True
                    )
            except Exception as e:
                await interaction.followup.send(
                    embed=create_error_embed("SSH Error", str(e)), ephemeral=True
                )

        elif action == 'sshx':
            await interaction.response.defer(ephemeral=True)
            try:
                await interaction.followup.send(
                    embed=create_info_embed(
                        "Generating Browser Session",
                        "Starting a temporary sshx browser terminal…"
                    ),
                    ephemeral=True
                )
                sshx_url = await get_lxc_sshx_session(container_name, node)
                sshx_embed = create_lxc_embed(
                    "Browser Terminal Ready",
                    f"Web terminal created for `{container_name}`."
                )
                sshx_embed.add_field(
                    name="🔗 Browser Link",
                    value=f"```\n{sshx_url}\n```",
                    inline=False
                )
                sshx_embed.add_field(
                    name="📌 How to Use",
                    value="Open the link in any browser. No SSH client, container IP, "
                          "or root password is required.",
                    inline=False
                )
                try:
                    await interaction.user.send(embed=sshx_embed)
                    await interaction.followup.send(
                        embed=create_success_embed(
                            "Browser Link Sent",
                            "Check your DMs for the temporary terminal link."
                        ),
                        ephemeral=True
                    )
                except discord.Forbidden:
                    await interaction.followup.send(
                        embed=create_error_embed(
                            "DM Failed",
                            "Enable server DMs so the bot can send your browser link."
                        ),
                        ephemeral=True
                    )
            except Exception as e:
                await interaction.followup.send(
                    embed=create_error_embed("SSHX Error", str(e)), ephemeral=True
                )

        elif action == 'delete':
            await interaction.response.defer(ephemeral=True)
            try:
                await remove_lxc_container(node, container_name)
                uid = self.owner_id
                lxc_data[uid] = [x for x in lxc_data.get(uid, []) if x['container_name'] != container_name]
                if not lxc_data[uid]:
                    del lxc_data[uid]
                await async_save_data()
                await interaction.followup.send(
                    embed=create_success_embed("LXC Deleted", f"`{container_name}` has been destroyed."), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Delete Failed", str(e)), ephemeral=True)

# ─── Node commands ─────────────────────────────────────────────────────────────

@bot.command(name='nodes')
@is_admin()
async def list_nodes(ctx):
    """List all connected nodes (Admin only)"""
    connect_url = f"ws://TAILSCALE_IP:{NODE_WS_PORT}"
    embed = create_embed(
        "Node Network Overview",
        f"**WebSocket port:** `{NODE_WS_PORT}`\n"
        f"**Connected nodes:** `{len(connected_nodes) + 1}` (including local)",
        C_CYAN
    )
    embed.add_field(
        name="📡 Connection URL (for node.py)",
        value=f"```\n{connect_url}\n```\n🔒 Set `MAIN_BOT_WS_URL` in `node.py` to your Tailscale IP",
        inline=False
    )
    embed.add_field(
        name="🖥️ local  ·  This Server (AlmaLinux / iDRAC 8)",
        value="```ansi\n\u001b[32mONLINE\u001b[0m  ─  Always available, no setup needed```",
        inline=False
    )
    if connected_nodes:
        for name, info in connected_nodes.items():
            ts = info.get("connected_at", "?")[:19].replace("T", " ")
            embed.add_field(
                name=f"🌐  {name}",
                value=f"**IP:** `{info['ip']}`\n**Since:** `{ts} UTC`",
                inline=True
            )
    else:
        embed.add_field(
            name="🔌 No Remote Nodes Connected",
            value=(
                "**To add a remote node, on each extra VPS run:**\n"
                "```bash\napt install docker.io python3-pip -y\npip install websockets\n```\n"
                "Edit `node.py`: set `NODE_NAME`, `MAIN_BOT_WS_URL`, and load "
                "`NODE_TOKEN` from the node's environment (never paste it into Discord).\n"
                "Then run: `python3 node.py`"
            ),
            inline=False
        )
    embed.add_field(
        name="📋 Usage",
        value="`!create @user <ram> <cpu> <disk> [node_name]` — deploy to a specific node",
        inline=False
    )
    await ctx.send(embed=embed)

@bot.command(name='node-kick')
@is_main_admin()
async def kick_node(ctx, node_name: str):
    was_connected = node_name in connected_nodes
    try:
        if was_connected:
            await connected_nodes[node_name]["ws"].close()
    except Exception:
        pass
    connected_nodes.pop(node_name, None)
    status = "disconnected and removed" if was_connected else "cleared from registry (was already offline)"
    await ctx.send(embed=create_success_embed("Node Kicked", f"Node `{node_name}` has been {status}."))

@bot.command(name='node-purge')
@is_main_admin()
async def purge_node(ctx, node_name: str):
    was_connected = node_name in connected_nodes
    try:
        if was_connected:
            await connected_nodes[node_name]["ws"].close()
    except Exception:
        pass
    connected_nodes.pop(node_name, None)
    purged = []
    for uid, vps_list in list(vps_data.items()):
        removed = [v["container_name"] for v in vps_list if v.get("node", "local") == node_name]
        kept    = [v for v in vps_list if v.get("node", "local") != node_name]
        purged.extend(removed)
        if kept:
            vps_data[uid] = kept
        else:
            del vps_data[uid]
    await async_save_data()
    embed = create_success_embed("Node Purged")
    embed.add_field(name="Node",          value=f"`{node_name}`",                                           inline=True)
    embed.add_field(name="Was Connected", value="Yes" if was_connected else "No",                          inline=True)
    embed.add_field(name="VPS Records Removed",
                    value="\n".join(f"`{c}`" for c in purged) if purged else "None", inline=False)
    embed.add_field(name="Note", value="Actual Docker containers on the node were NOT touched.", inline=False)
    await ctx.send(embed=embed)

# ─── Docker / LXC Unified Create Command ──────────────────────────────────────

@bot.command(name='set-mode')
@is_admin()
async def set_create_mode(ctx, mode: str = None):
    """Switch !create between Docker and LXC — !set-mode <docker|lxc>
    Run !set-mode with no argument to see the current active mode."""
    if mode is None:
        current = get_create_mode()
        badge = "🐳 **Docker**" if current == "docker" else "📦 **LXC**"
        embed = create_info_embed(
            "Current Create Mode",
            f"The `!create` command is currently set to {badge}.\n\n"
            "Use `!set-mode docker` or `!set-mode lxc` to switch."
        )
        await ctx.send(embed=embed)
        return

    mode = mode.lower().strip()
    if mode not in ("docker", "lxc"):
        await ctx.send(embed=create_error_embed(
            "Invalid Mode",
            "Choose one of: `docker` or `lxc`\n"
            "Example: `!set-mode lxc`"
        ))
        return

    # Validate availability before switching
    if mode == "docker" and not shutil.which("docker"):
        await ctx.send(embed=create_error_embed(
            "Docker Not Available",
            "The `docker` command was not found on this server.\n"
            "Install Docker first or keep using LXC mode."
        ))
        return
    if mode == "lxc" and not shutil.which("lxc-create"):
        await ctx.send(embed=create_error_embed(
            "LXC Not Available",
            "The `lxc-create` command was not found on this server.\n"
            "Install LXC: `dnf install -y lxc lxc-templates lxc-extra`"
        ))
        return

    settings_data["create_mode"] = mode
    await async_save_data()

    badge = "🐳 **Docker**" if mode == "docker" else "📦 **LXC**"
    tip   = "Use `!create @user <ram_GB> <cpu> <disk_GB> [node]`"
    embed = create_success_embed(
        "Create Mode Updated",
        f"The `!create` command will now deploy {badge} containers.\n\n{tip}"
    )
    embed.add_field(name="🔧 Active Mode", value=badge, inline=True)
    embed.add_field(name="Changed By",     value=ctx.author.mention, inline=True)
    await ctx.send(embed=embed)

@bot.command(name='create')
@is_admin()
async def create_vps(ctx, user: discord.Member, ram: int, cpu: int, disk: int = 30, node: str = "local"):
    """Create a VPS/container for a user (uses active mode: docker or lxc) — !create @user <ram_GB> <cpu_cores> <disk_GB> [node]
    Switch the active mode with !set-mode <docker|lxc>"""
    if ram <= 0 or cpu <= 0 or disk <= 0:
        await ctx.send(embed=create_error_embed("Invalid Specs",
            "RAM, CPU and Disk must be positive integers.\n"
            "Usage: `!create @user <ram_GB> <cpu_cores> <disk_GB> [node]`"))
        return
    if node != "local" and node not in connected_nodes:
        await ctx.send(embed=create_error_embed("Node Not Found",
            f"Node `{node}` is not connected. Use `!nodes` to see available nodes."))
        return

    mode = get_create_mode()

    # ── LXC path ──────────────────────────────────────────────────────────────
    if mode == "lxc":
        if not shutil.which("lxc-create"):
            await ctx.send(embed=create_error_embed(
                "LXC Not Available",
                "The `lxc-create` command was not found on this AlmaLinux host.\n"
                "Install LXC: `dnf install -y lxc lxc-templates lxc-extra`\n"
                "Or switch back to Docker mode: `!set-mode docker`"
            ))
            return

        user_id = str(user.id)
        if user_id not in lxc_data:
            lxc_data[user_id] = []
        lxc_count      = len(lxc_data[user_id]) + 1
        container_name = f"lxc-{user_id[-8:]}-{lxc_count}"
        ram_mb         = ram * 1024
        password       = generate_password()

        deploy_embed = create_lxc_embed(
            "Deploying LXC Container",
            f"Creating Ubuntu 22.04 LXC container for {user.mention}…\n"
            f"⚙️ Setting up cgroups, networking, and SSH"
        )
        deploy_embed.add_field(name="🧠 RAM",  value=f"`{ram}GB`",        inline=True)
        deploy_embed.add_field(name="⚙️ CPU",  value=f"`{cpu} Core(s)`",  inline=True)
        deploy_embed.add_field(name="💾 Disk", value=f"`{disk}GB`",       inline=True)
        deploy_embed.add_field(name="🌐 Node", value=node_badge(node),    inline=True)
        deploy_embed.add_field(
            name="⏱️ Estimated Time",
            value="3–8 minutes (template download + setup)",
            inline=False
        )
        await ctx.send(embed=deploy_embed)

        try:
            await create_lxc_container(container_name, ram_mb, cpu, disk, password, node=node)
            ip = await lxc_get_container_ip(container_name, node)

            lxc_info = {
                "container_name": container_name,
                "ram": f"{ram}GB",
                "cpu": str(cpu),
                "storage": f"{disk}GB",
                "node": node,
                "status": "running",
                "created_at": datetime.now().isoformat(),
                "expires": "Never",
                "ssh_password": password,
                "ip": ip
            }
            lxc_data[user_id].append(lxc_info)
            await async_save_data()

            if ctx.guild:
                vps_role = await get_or_create_vps_role(ctx.guild)
                if vps_role:
                    try:
                        await user.add_roles(vps_role, reason="LXC container granted")
                    except discord.Forbidden:
                        pass

            embed = create_lxc_embed(
                "LXC Container Deployed",
                f"{user.mention} — your LXC container is ready! Check your **DMs** for access details."
            )
            embed.add_field(name="👤 Owner",     value=user.mention,          inline=True)
            embed.add_field(name="🆔 LXC ID",   value=f"`#{lxc_count}`",     inline=True)
            embed.add_field(name="📦 Container", value=f"`{container_name}`", inline=True)
            embed.add_field(name="🧠 RAM",       value=f"`{ram}GB`",          inline=True)
            embed.add_field(name="⚙️ CPU",       value=f"`{cpu} Core(s)`",    inline=True)
            embed.add_field(name="💾 Disk",      value=f"`{disk}GB`",         inline=True)
            embed.add_field(name="🌐 Node",      value=node_badge(node),      inline=True)
            embed.add_field(
                name="🔐 Secure Access",
                value="Use `!lxc-manage` → **SSH** or **SSHX**. "
                      "The container IP and root password stay private.",
                inline=False
            )
            await ctx.send(embed=embed)

            try:
                dm_embed = create_lxc_embed(
                    "Your LXC Container is Ready",
                    "Connect to your new Linux container below."
                )
                dm_embed.add_field(name="🆔 LXC ID",      value=f"`#{lxc_count}`",     inline=True)
                dm_embed.add_field(name="🧠 RAM",          value=f"`{ram}GB`",          inline=True)
                dm_embed.add_field(name="⚙️ CPU",          value=f"`{cpu} Core(s)`",    inline=True)
                dm_embed.add_field(
                    name="🔐 Secure Access",
                    value="Use `!lxc-manage` in the server and click **SSH** or **SSHX**. "
                          "The bot creates a temporary relay, so the LXC IP and root password "
                          "are never exposed.",
                    inline=False
                )
                dm_embed.add_field(
                    name="📌 Getting Started",
                    value="**1.** Run `!lxc-manage` in the server\n"
                          "**2.** Choose **SSH** or **SSHX**\n"
                          "**3.** Use the temporary terminal session",
                    inline=False
                )
                await user.send(embed=dm_embed)
            except discord.Forbidden:
                pass

        except Exception as e:
            await ctx.send(embed=create_error_embed("LXC Deployment Failed", f"Error: {str(e)}"))
        return

    # ── Docker path ───────────────────────────────────────────────────────────
    if not shutil.which("docker"):
        await ctx.send(embed=create_error_embed(
            "Docker Not Available",
            "The `docker` command was not found on this server.\n"
            "Switch to LXC mode with `!set-mode lxc` or install Docker first."
        ))
        return

    user_id = str(user.id)
    if user_id not in vps_data:
        vps_data[user_id] = []
    vps_count = len(vps_data[user_id]) + 1
    container_name = f"vps-{user_id}-{vps_count}"
    ram_mb = ram * 1024
    password = generate_password()

    deploy_embed = create_info_embed(
        "Deploying Docker VPS",
        f"Spinning up container for {user.mention}…"
    )
    deploy_embed.add_field(name="🧠 RAM",   value=f"`{ram}GB`",        inline=True)
    deploy_embed.add_field(name="⚙️ CPU",   value=f"`{cpu} Core(s)`", inline=True)
    deploy_embed.add_field(name="💾 Disk",  value=f"`{disk}GB`",       inline=True)
    deploy_embed.add_field(name="🌐 Node",  value=node_badge(node),    inline=True)
    await ctx.send(embed=deploy_embed)

    try:
        ssh_port = await create_docker_container(container_name, ram_mb, cpu, 0, password, disk_gb=disk, node=node)
        vps_info = {
            "container_name": container_name,
            "ram": f"{ram}GB",
            "cpu": str(cpu),
            "storage": f"{disk}GB",
            "node": node,
            "status": "running",
            "created_at": datetime.now().isoformat(),
            "expires": "Never",
            "ssh_password": password,
            "ssh_port": ssh_port,
            "shared_with": []
        }
        vps_data[user_id].append(vps_info)
        await async_save_data()

        if ctx.guild:
            vps_role = await get_or_create_vps_role(ctx.guild)
            if vps_role:
                try:
                    await user.add_roles(vps_role, reason="VPS ownership granted")
                except discord.Forbidden:
                    pass

        embed = create_embed(
            "VPS Deployed Successfully",
            f"{user.mention} — your VPS is live! Check your **DMs** for SSH access.",
            C_SUCCESS
        )
        embed.add_field(name="👤 Owner",    value=user.mention,          inline=True)
        embed.add_field(name="🆔 VPS ID",   value=f"`#{vps_count}`",     inline=True)
        embed.add_field(name="📦 Container",value=f"`{container_name}`", inline=True)
        embed.add_field(name="🧠 RAM",      value=f"`{ram}GB`",          inline=True)
        embed.add_field(name="⚙️ CPU",      value=f"`{cpu} Core(s)`",    inline=True)
        embed.add_field(name="💾 Disk",     value=f"`{disk}GB`",         inline=True)
        embed.add_field(name="🌐 Node",     value=node_badge(node),      inline=True)
        embed.add_field(name="📋 Manage",   value="Use `!manage` → Start · Stop · SSH · Reinstall", inline=False)
        await ctx.send(embed=embed)

        try:
            tmate_cmd = await get_tmate_session(container_name, node=node)
            dm_embed = create_embed(
                "Your VPS is Ready",
                "Connect now using the command below.",
                C_PRIMARY
            )
            dm_embed.add_field(name="🆔 VPS ID",     value=f"`#{vps_count}`",     inline=True)
            dm_embed.add_field(name="🧠 RAM",         value=f"`{ram}GB`",          inline=True)
            dm_embed.add_field(name="⚙️ CPU",         value=f"`{cpu} Core(s)`",    inline=True)
            dm_embed.add_field(name="🌐 Node",        value=node_badge(node),      inline=True)
            dm_embed.add_field(name="🔑 Root Password",value=f"```\n{password}\n```",  inline=False)
            dm_embed.add_field(name="🔗 SSH Command", value=f"```bash\n{tmate_cmd}\n```", inline=False)
            dm_embed.add_field(name="📌 Quick Start", value=(
                "**1.** Copy the SSH command above\n"
                "**2.** Paste it in CMD / Termux / PuTTY\n"
                "**3.** Enter your password and you're in!"
            ), inline=False)
            dm_embed.add_field(name="🎮 Manage", value="Use `!manage` in the server to Start · Stop · Reinstall · SSH · SSHX", inline=False)
            await user.send(embed=dm_embed)
        except discord.Forbidden:
            pass

        await ctx.send(
            embed=create_info_embed("Set Expiry?",
                f"VPS `{container_name}` deployed for {user.mention}.\n"
                "Would you like to set an expiry date?"),
            view=ExpirePromptView(str(ctx.author.id), vps_info, container_name, user)
        )
    except Exception as e:
        await ctx.send(embed=create_error_embed("Deployment Failed", f"Error: {str(e)}"))

@bot.command(name='manage')
async def manage_vps(ctx, user: discord.Member = None):
    """Manage your VPS — admins can pass @user to manage others"""
    if user:
        if not (str(ctx.author.id) == str(MAIN_ADMIN_ID) or str(ctx.author.id) in admin_data.get("admins", [])):
            await ctx.send(embed=create_error_embed("Access Denied", "Only admins can manage other users' VPS."))
            return
        uid = str(user.id)
        vl = vps_data.get(uid, [])
        if not vl:
            await ctx.send(embed=create_error_embed("No VPS Found", f"{user.mention} has no VPS deployments."))
            return
        view = ManageView(str(ctx.author.id), vl, is_admin=True, owner_id=uid)
        await ctx.send(embed=view.initial_embed, view=view)
    else:
        uid = str(ctx.author.id)
        vl  = vps_data.get(uid, [])
        if not vl:
            await ctx.send(embed=create_embed(
                "No VPS Found",
                "You don't have any Docker VPS yet.\nContact an admin to get one, or use `!redeem <code>` if you have a code.",
                C_ERROR
            ))
            return
        view = ManageView(uid, vl)
        await ctx.send(embed=view.initial_embed, view=view)

@bot.command(name='delete-vps')
@is_admin()
async def delete_vps(ctx, user: discord.Member, vps_number: int, *, reason: str = "No reason provided"):
    user_id = str(user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number or user has no VPS."))
        return
    vps = vps_data[user_id][vps_number - 1]
    container_name = vps["container_name"]
    node = vps.get("node", "local")
    await ctx.send(embed=create_info_embed("Deleting VPS", f"Removing VPS #{vps_number} from node `{node}`…"))
    try:
        await remove_docker_container(node, container_name)
        del vps_data[user_id][vps_number - 1]
        if not vps_data[user_id]:
            del vps_data[user_id]
            if ctx.guild:
                vps_role = await get_or_create_vps_role(ctx.guild)
                if vps_role and vps_role in user.roles:
                    try:
                        await user.remove_roles(vps_role)
                    except discord.Forbidden:
                        pass
        await async_save_data()
        embed = create_success_embed("VPS Deleted")
        embed.add_field(name="👤 Owner",    value=user.mention,          inline=True)
        embed.add_field(name="🆔 VPS ID",   value=f"`#{vps_number}`",    inline=True)
        embed.add_field(name="📦 Container",value=f"`{container_name}`", inline=True)
        embed.add_field(name="🌐 Node",     value=node_badge(node),      inline=True)
        embed.add_field(name="📝 Reason",   value=reason,                inline=False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Deletion Failed", str(e)))

@bot.command(name='purge-vps')
@is_admin()
async def purge_vps(ctx, user: discord.Member, vps_number: int):
    """Force-remove a VPS record without contacting the node"""
    user_id = str(user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number or user has no VPS."))
        return
    vps = vps_data[user_id][vps_number - 1]
    container_name = vps["container_name"]
    node = vps.get("node", "local")
    del vps_data[user_id][vps_number - 1]
    if not vps_data[user_id]:
        del vps_data[user_id]
        if ctx.guild:
            vps_role = await get_or_create_vps_role(ctx.guild)
            if vps_role and vps_role in user.roles:
                try:
                    await user.remove_roles(vps_role)
                except discord.Forbidden:
                    pass
    await async_save_data()
    embed = create_success_embed("VPS Record Purged")
    embed.add_field(name="👤 Owner",    value=user.mention,          inline=True)
    embed.add_field(name="🆔 VPS #",    value=f"`#{vps_number}`",    inline=True)
    embed.add_field(name="📦 Container",value=f"`{container_name}`", inline=True)
    embed.add_field(name="🌐 Node",     value=node_badge(node),      inline=True)
    embed.add_field(name="⚠️ Note", value="Record removed from bot data. The actual Docker container (if any) was **NOT** touched.", inline=False)
    await ctx.send(embed=embed)

class ListVpsView(discord.ui.View):
    PAGE_SIZE = 5

    def __init__(self, ctx, entries):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.entries = entries
        self.page = 0
        self.total_pages = max(1, (len(entries) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.pending_delete = None
        self._rebuild_components()

    def _page_entries(self):
        start = self.page * self.PAGE_SIZE
        return self.entries[start:start + self.PAGE_SIZE]

    def build_embed(self):
        page_entries = self._page_entries()
        total   = len(self.entries)
        running = sum(1 for e in self.entries if e['vps'].get('status') == 'running')
        embed = create_embed(
            "Admin  ·  All VPS Overview",
            f"**Total:** `{total}` containers across all nodes",
            C_PURPLE
        )
        embed.add_field(
            name="📊 Summary",
            value=(
                f"🟢 Running: **{running}**  ·  🔴 Stopped: **{total - running}**\n"
                f"🌐 Nodes online: **{len(connected_nodes) + 1}** (incl. local)"
            ),
            inline=False
        )
        for i, entry in enumerate(page_entries):
            global_idx = self.page * self.PAGE_SIZE + i + 1
            vps = entry['vps']
            node = vps.get('node', 'local')
            expires = vps.get('expires', 'Never')
            if expires and expires != 'Never':
                try:
                    days_left = (datetime.fromisoformat(expires) - datetime.utcnow()).days
                    exp_str = f"Exp. in {days_left}d" if days_left >= 0 else "⚠️ EXPIRED"
                except Exception:
                    exp_str = expires[:10]
            else:
                exp_str = "Never"
            nick = f" *(_{vps.get('nickname','')}_)*" if vps.get('nickname') else ""
            embed.add_field(
                name=f"{'🟢' if vps.get('status')=='running' else '🔴'}  #{global_idx}  `{vps['container_name']}`{nick}",
                value=(
                    f"👤 **{entry['user_name']}**  ·  🌐 {node_badge(node)}\n"
                    f"🧠 `{vps.get('ram','?')}`  ⚙️ `{vps.get('cpu','?')}c`  💾 `{vps.get('storage','?')}`\n"
                    f"Status: {status_badge(vps.get('status','unknown'))}  ·  ⏳ {exp_str}"
                ),
                inline=False
            )
        embed.set_footer(text=f"GunpointNodes  ›  Page {self.page + 1}/{self.total_pages}  —  use the dropdown to delete a VPS")
        return embed

    def _rebuild_components(self):
        self.clear_items()
        page_entries = self._page_entries()
        prev_btn = discord.ui.Button(label="◀ Prev", style=discord.ButtonStyle.secondary, disabled=(self.page == 0))
        prev_btn.callback = self._prev
        self.add_item(prev_btn)
        next_btn = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary, disabled=(self.page >= self.total_pages - 1))
        next_btn.callback = self._next
        self.add_item(next_btn)
        refresh_btn = discord.ui.Button(label="🔄 Refresh", style=discord.ButtonStyle.primary)
        refresh_btn.callback = self._refresh
        self.add_item(refresh_btn)
        if not page_entries:
            return
        options = []
        for i, entry in enumerate(page_entries):
            vps = entry['vps']
            global_num = self.page * self.PAGE_SIZE + i + 1
            options.append(discord.SelectOption(
                label=f"#{global_num} {vps['container_name']}"[:100],
                description=f"Owner: {entry['user_name']} | Node: {vps.get('node','local')}"[:100],
                value=str(i)
            ))
        select = discord.ui.Select(placeholder="🗑️ Select a VPS to delete…", options=options)
        select.callback = self._select_delete
        self.add_item(select)
        if self.pending_delete is not None:
            confirm_btn = discord.ui.Button(label="✅ Confirm Delete", style=discord.ButtonStyle.danger)
            confirm_btn.callback = self._confirm_delete
            self.add_item(confirm_btn)
            cancel_btn = discord.ui.Button(label="✖ Cancel", style=discord.ButtonStyle.secondary)
            cancel_btn.callback = self._cancel_delete
            self.add_item(cancel_btn)

    async def _only_admin(self, interaction):
        if str(interaction.user.id) != str(self.ctx.author.id):
            await interaction.response.send_message(
                embed=create_error_embed("Access Denied", "Only the admin who ran this command can use these controls."),
                ephemeral=True)
            return False
        return True

    async def _prev(self, interaction):
        if not await self._only_admin(interaction): return
        self.page = max(0, self.page - 1)
        self.pending_delete = None
        self._rebuild_components()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _next(self, interaction):
        if not await self._only_admin(interaction): return
        self.page = min(self.total_pages - 1, self.page + 1)
        self.pending_delete = None
        self._rebuild_components()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _refresh(self, interaction):
        if not await self._only_admin(interaction): return
        self.pending_delete = None
        self._rebuild_components()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _select_delete(self, interaction):
        if not await self._only_admin(interaction): return
        local_idx = int(interaction.data["values"][0])
        entry = self._page_entries()[local_idx]
        self.pending_delete = entry
        vps = entry['vps']
        node = vps.get('node', 'local')
        warn_embed = create_warning_embed(
            "Confirm Deletion",
            f"You are about to **permanently delete** the VPS below.\n\n"
            f"📦 **Container:** `{vps['container_name']}`\n"
            f"👤 **Owner:** {entry['user_name']}\n"
            f"🌐 **Node:** {node_badge(node)}\n"
            f"🧠 `{vps.get('ram','?')}`  ⚙️ `{vps.get('cpu','?')}c`  💾 `{vps.get('storage','?')}`\n\n"
            "Press **✅ Confirm Delete** to proceed or **✖ Cancel** to abort."
        )
        self._rebuild_components()
        await interaction.response.edit_message(embed=warn_embed, view=self)

    async def _cancel_delete(self, interaction):
        if not await self._only_admin(interaction): return
        self.pending_delete = None
        self._rebuild_components()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _confirm_delete(self, interaction):
        if not await self._only_admin(interaction): return
        if self.pending_delete is None:
            await interaction.response.send_message(
                embed=create_error_embed("Nothing Staged", "Select a VPS from the dropdown first."), ephemeral=True)
            return
        entry = self.pending_delete
        vps = entry['vps']
        container_name = vps['container_name']
        node = vps.get('node', 'local')
        user_id = entry['user_id']
        await interaction.response.defer()
        try:
            await remove_docker_container(node, container_name)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed("Delete Failed", f"Could not remove container:\n`{e}`"), ephemeral=True)
            return
        user_vps_list = vps_data.get(user_id, [])
        for idx, v in enumerate(user_vps_list):
            if v['container_name'] == container_name:
                del user_vps_list[idx]
                break
        if not user_vps_list:
            del vps_data[user_id]
            if self.ctx.guild:
                try:
                    member = self.ctx.guild.get_member(int(user_id)) or await self.ctx.guild.fetch_member(int(user_id))
                    vps_role = await get_or_create_vps_role(self.ctx.guild)
                    if vps_role and vps_role in member.roles:
                        await member.remove_roles(vps_role)
                except Exception:
                    pass
        await async_save_data()
        new_entries = []
        for uid, vlist in vps_data.items():
            try:
                u = await bot.fetch_user(int(uid))
                uname = u.name
                uobj  = u
            except Exception:
                uname = f"Unknown ({uid})"
                uobj  = None
            for vi, v in enumerate(vlist):
                new_entries.append({'user_id': uid, 'user_name': uname, 'user_obj': uobj, 'vps_index': vi, 'vps': v})
        self.entries = new_entries
        self.total_pages = max(1, (len(new_entries) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = min(self.page, self.total_pages - 1)
        self.pending_delete = None
        self._rebuild_components()
        await interaction.followup.send(
            embed=create_success_embed("VPS Deleted",
                f"Container `{container_name}` removed from node `{node}`.\nOwner: **{entry['user_name']}**"))
        await interaction.message.edit(embed=self.build_embed(), view=self)

@bot.command(name='list-vps')
@is_admin()
async def list_vps_panel(ctx):
    """Interactive paginated VPS list (Admin only)"""
    if not vps_data:
        await ctx.send(embed=create_info_embed("No VPS Found", "No VPS deployments exist yet."))
        return
    status_msg = await ctx.send(embed=create_info_embed("Loading…", "Fetching VPS list across all nodes…"))
    entries = []
    for uid, vlist in vps_data.items():
        try:
            u = await bot.fetch_user(int(uid))
            uname = u.name
            uobj  = u
        except Exception:
            uname = f"Unknown ({uid})"
            uobj  = None
        for vi, v in enumerate(vlist):
            entries.append({'user_id': uid, 'user_name': uname, 'user_obj': uobj, 'vps_index': vi, 'vps': v})
    if not entries:
        await status_msg.edit(embed=create_info_embed("No VPS Found", "No VPS records."))
        return
    view = ListVpsView(ctx, entries)
    await status_msg.edit(embed=view.build_embed(), view=view)

@bot.command(name='list-all')
@is_admin()
async def list_all_vps(ctx):
    """List all VPS overview (Admin only)"""
    embed = create_embed("All VPS — Plain Overview", "Complete deployment overview", C_DARK)
    total_vps = running_vps = stopped_vps = 0
    user_summary = []
    vps_lines = []
    for user_id, vps_list in vps_data.items():
        try:
            user = await bot.fetch_user(int(user_id))
            uc = len(vps_list)
            ur = sum(1 for v in vps_list if v.get('status') == 'running')
            total_vps += uc; running_vps += ur; stopped_vps += uc - ur
            user_summary.append(f"**{user.name}** — {uc} VPS ({ur} running)")
            for i, vps in enumerate(vps_list):
                icon = "🟢" if vps.get('status') == 'running' else "🔴"
                vps_lines.append(f"{icon} **{user.name}** — VPS {i+1}: `{vps['container_name']}` | {vps.get('status','?').upper()} | {node_badge(vps.get('node','local'))}")
        except discord.NotFound:
            vps_lines.append(f"❓ Unknown ({user_id}) — {len(vps_list)} VPS")
    embed.add_field(
        name="📊 System Totals",
        value=(
            f"Users: **{len(vps_data)}**  ·  VPS: **{total_vps}**\n"
            f"🟢 Running: **{running_vps}**  ·  🔴 Stopped: **{stopped_vps}**\n"
            f"🌐 Nodes: **{len(connected_nodes) + 1}** (incl. local)"
        ),
        inline=False
    )
    if user_summary:
        embed.add_field(name="👥 User Summary", value="\n".join(user_summary[:10]), inline=False)
    for i in range(0, min(len(vps_lines), 30), 15):
        embed.add_field(name=f"VPS List ({i+1}–{min(i+15, len(vps_lines))})",
                        value="\n".join(vps_lines[i:i+15]), inline=False)
    await ctx.send(embed=embed)

@bot.command(name='manage-shared')
async def manage_shared_vps(ctx, owner: discord.Member, vps_number: int):
    """Manage a shared VPS"""
    owner_id = str(owner.id)
    user_id  = str(ctx.author.id)
    if owner_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[owner_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number."))
        return
    vps = vps_data[owner_id][vps_number - 1]
    if user_id not in vps.get("shared_with", []):
        await ctx.send(embed=create_error_embed("Access Denied", "You don't have access to this VPS."))
        return
    view = ManageView(user_id, [vps], is_shared=True, owner_id=owner_id)
    await ctx.send(embed=view.initial_embed, view=view)

@bot.command(name='share-user')
async def share_user(ctx, shared_user: discord.Member, vps_number: int):
    """Share your VPS with another user"""
    user_id = str(ctx.author.id)
    sid     = str(shared_user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number."))
        return
    vps = vps_data[user_id][vps_number - 1]
    if "shared_with" not in vps:
        vps["shared_with"] = []
    if sid in vps["shared_with"]:
        await ctx.send(embed=create_error_embed("Already Shared", f"{shared_user.mention} already has access."))
        return
    vps["shared_with"].append(sid)
    await async_save_data()
    await ctx.send(embed=create_success_embed("VPS Shared", f"VPS #{vps_number} shared with {shared_user.mention}!"))
    try:
        await shared_user.send(embed=create_embed(
            "VPS Access Granted",
            f"You have been granted access to VPS #{vps_number} from {ctx.author.mention}.\n"
            f"Use `!manage-shared {ctx.author.mention} {vps_number}` to manage it.",
            C_SUCCESS
        ))
    except discord.Forbidden:
        pass

@bot.command(name='share-ruser')
async def revoke_share(ctx, shared_user: discord.Member, vps_number: int):
    """Revoke shared VPS access"""
    user_id = str(ctx.author.id)
    sid     = str(shared_user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number."))
        return
    vps = vps_data[user_id][vps_number - 1]
    if "shared_with" not in vps or sid not in vps["shared_with"]:
        await ctx.send(embed=create_error_embed("Not Shared", f"{shared_user.mention} doesn't have access."))
        return
    vps["shared_with"].remove(sid)
    await async_save_data()
    await ctx.send(embed=create_success_embed("Access Revoked", f"Revoked {shared_user.mention}'s access to VPS #{vps_number}."))

@bot.command(name='userinfo')
@is_admin()
async def user_info(ctx, user: discord.Member):
    user_id = str(user.id)
    embed = create_embed(f"User Profile  ·  {user.name}", "", C_PRIMARY, thumbnail=str(user.display_avatar.url))
    embed.add_field(name="👤 User",   value=f"{user.mention}\n**ID:** `{user.id}`", inline=True)
    embed.add_field(name="📅 Joined", value=f"`{user.created_at.strftime('%Y-%m-%d')}`", inline=True)
    is_adm = str(user_id) == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])
    embed.add_field(name="🛡️ Admin", value="✅ Yes" if is_adm else "❌ No", inline=True)
    vps_list = vps_data.get(user_id, [])
    if vps_list:
        vps_text = "\n".join([
            f"{'🟢' if v.get('status')=='running' else '🔴'} VPS {i+1}: `{v['container_name']}` · {node_badge(v.get('node','local'))}"
            for i, v in enumerate(vps_list)
        ])
        embed.add_field(name="🖥️ Docker VPS", value=vps_text, inline=False)
    else:
        embed.add_field(name="🖥️ Docker VPS", value="None", inline=False)
    lxc_list = lxc_data.get(user_id, [])
    if lxc_list:
        lxc_text = "\n".join([
            f"{'🟢' if c.get('status')=='running' else '🔴'} LXC {i+1}: `{c['container_name']}` · IP: `{c.get('ip','?')}`"
            for i, c in enumerate(lxc_list)
        ])
        embed.add_field(name="📦 LXC Containers", value=lxc_text, inline=False)
    await ctx.send(embed=embed)

@bot.command(name='serverstats')
@is_admin()
async def server_stats(ctx):
    total_vps   = sum(len(v) for v in vps_data.values())
    running_vps = sum(1 for vl in vps_data.values() for v in vl if v.get('status') == 'running')
    total_lxc   = sum(len(v) for v in lxc_data.values())
    running_lxc = sum(1 for vl in lxc_data.values() for v in vl if v.get('status') == 'running')
    total_ram   = sum(int(v['ram'].replace('GB','')) for vl in vps_data.values() for v in vl)
    lxc_ram     = sum(int(str(c.get('ram','0GB')).replace('GB','')) for vl in lxc_data.values() for c in vl)
    node_counts = {}
    for vl in vps_data.values():
        for v in vl:
            n = v.get('node', 'local')
            node_counts[n] = node_counts.get(n, 0) + 1
    embed = create_embed("Infrastructure Stats", "Full overview of all deployments", C_GOLD)
    embed.add_field(
        name="🖥️ Docker VPS",
        value=(
            f"Total: **{total_vps}**\n"
            f"🟢 Running: **{running_vps}**\n"
            f"🔴 Stopped: **{total_vps - running_vps}**\n"
            f"RAM Allocated: **{total_ram}GB**"
        ),
        inline=True
    )
    embed.add_field(
        name="📦 LXC Containers",
        value=(
            f"Total: **{total_lxc}**\n"
            f"🟢 Running: **{running_lxc}**\n"
            f"🔴 Stopped: **{total_lxc - running_lxc}**\n"
            f"RAM Allocated: **{lxc_ram}GB**"
        ),
        inline=True
    )
    embed.add_field(
        name="🌐 Nodes",
        value=f"Connected: **{len(connected_nodes) + 1}** (incl. local)\n" +
              "\n".join(f"`{n}`: {c} VPS" for n, c in node_counts.items()),
        inline=True
    )
    embed.add_field(name="👥 Users", value=f"**{len(set(list(vps_data.keys()) + list(lxc_data.keys())))}** total", inline=True)
    await ctx.send(embed=embed)

@bot.command(name='vpsinfo')
@is_admin()
async def vps_info(ctx, container_name: str = None):
    if not container_name:
        all_vps = []
        for uid, vl in vps_data.items():
            try:
                u = await bot.fetch_user(int(uid))
                for i, v in enumerate(vl):
                    all_vps.append(f"**{u.name}** — VPS {i+1}: `{v['container_name']}` | {v.get('status','?').upper()} | {node_badge(v.get('node','local'))}")
            except Exception:
                pass
        embed = create_embed("All VPS Info", f"Total: {len(all_vps)}", C_PRIMARY)
        for i in range(0, len(all_vps), 20):
            embed.add_field(name=f"VPS ({i+1}–{i+20})", value="\n".join(all_vps[i:i+20]) or "—", inline=False)
        await ctx.send(embed=embed)
    else:
        found_vps = None
        found_user = None
        for uid, vl in vps_data.items():
            for v in vl:
                if v['container_name'] == container_name:
                    found_vps = v
                    try:
                        found_user = await bot.fetch_user(int(uid))
                    except Exception:
                        pass
                    break
            if found_vps:
                break
        if not found_vps:
            await ctx.send(embed=create_error_embed("Not Found", f"No VPS named `{container_name}`"))
            return
        embed = create_embed(f"VPS  ·  `{container_name}`", f"Owned by {found_user.mention if found_user else 'Unknown'}", C_PRIMARY)
        embed.add_field(name="Specs", value=f"RAM: `{found_vps['ram']}`\nCPU: `{found_vps['cpu']}` cores", inline=True)
        embed.add_field(name="Status", value=status_badge(found_vps.get('status','?')), inline=True)
        embed.add_field(name="Node", value=node_badge(found_vps.get('node','local')), inline=True)
        embed.add_field(name="Created", value=f"`{found_vps.get('created_at','Unknown')[:10]}`", inline=True)
        await ctx.send(embed=embed)

@bot.command(name='restart-vps')
@is_admin()
async def restart_vps(ctx, container_name: str):
    node = "local"
    for vl in vps_data.values():
        for v in vl:
            if v['container_name'] == container_name:
                node = v.get('node', 'local')
                break
    await ctx.send(embed=create_info_embed("Restarting VPS", f"Restarting `{container_name}` on node `{node}`…"))
    try:
        await routed_execute_docker(node, f"docker restart {container_name}")
        await asyncio.sleep(5)
        await routed_docker_exec(node, container_name, "systemctl start ssh || /usr/sbin/sshd || true", timeout=15)
        for vl in vps_data.values():
            for v in vl:
                if v['container_name'] == container_name:
                    v['status'] = 'running'
                    await async_save_data()
                    break
        await ctx.send(embed=create_success_embed("VPS Restarted", f"`{container_name}` restarted successfully!"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Restart Failed", str(e)))

@bot.command(name='backup-vps')
@is_admin()
async def backup_vps(ctx, container_name: str):
    node = "local"
    for vl in vps_data.values():
        for v in vl:
            if v['container_name'] == container_name:
                node = v.get('node', 'local')
                break
    snapshot = f"{container_name}-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    await ctx.send(embed=create_info_embed("Creating Backup", f"Committing snapshot of `{container_name}` on node `{node}`…"))
    try:
        await routed_execute_docker(node, f"docker commit {container_name} {snapshot}")
        await ctx.send(embed=create_success_embed("Backup Created", f"Snapshot `{snapshot}` saved on node `{node}`!"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Backup Failed", str(e)))

@bot.command(name='restore-vps')
@is_admin()
async def restore_vps(ctx, container_name: str, snapshot_name: str):
    found_vps = None
    for vl in vps_data.values():
        for v in vl:
            if v['container_name'] == container_name:
                found_vps = v
                break
    if not found_vps:
        await ctx.send(embed=create_error_embed("Not Found", f"No VPS data for `{container_name}`"))
        return
    node    = found_vps.get('node', 'local')
    ram_mb  = int(str(found_vps.get("ram", "1GB")).replace("GB", "")) * 1024
    cpu_c   = int(found_vps.get("cpu", 1))
    cpuset  = build_cpuset(cpu_c)
    ssh_port = int(found_vps.get("ssh_port", 0) or 0)
    if not ssh_port:
        ssh_port = await allocate_ssh_port(node)
    await ctx.send(embed=create_info_embed("Restoring VPS", f"Restoring `{container_name}` from `{snapshot_name}`…"))
    try:
        try:
            await routed_execute_docker(
                node, f"docker stop {shlex.quote(container_name)}", timeout=30
            )
        except Exception:
            pass
        await remove_docker_container(node, container_name)
        run_cmd = (
            f"docker run -d --name {shlex.quote(container_name)} "
            f"--memory={ram_mb}m --cpus={cpu_c} --cpuset-cpus={cpuset} "
            f"-p {ssh_port}:22/tcp --restart=unless-stopped --privileged --cgroupns=host "
            f"-v /sys/fs/cgroup:/sys/fs/cgroup:rw {snapshot_name} /sbin/init"
        )
        await routed_execute_docker(node, run_cmd)
        await asyncio.sleep(5)
        await routed_docker_exec(node, container_name, "systemctl start ssh || /usr/sbin/sshd || true", timeout=15)
        await apply_fake_hardware(node, container_name, ram_mb, cpu_c)
        found_vps["ssh_port"] = ssh_port
        found_vps["status"] = "running"
        await async_save_data()
        await ctx.send(embed=create_success_embed("VPS Restored", f"`{container_name}` restored from `{snapshot_name}`!"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Restore Failed", str(e)))

@bot.command(name='list-snapshots')
@is_admin()
async def list_snapshots(ctx, container_name: str):
    node = "local"
    for vl in vps_data.values():
        for v in vl:
            if v['container_name'] == container_name:
                node = v.get('node', 'local')
                break
    try:
        output = await routed_execute_docker(
            node,
            f"docker images --format '{{{{.Repository}}}}:{{{{.Tag}}}} ({{{{.Size}}}})'",
            timeout=30
        )
        all_images = (output if isinstance(output, str) else "").strip().split('\n')
        snapshots = [img for img in all_images if img.startswith(container_name + "-backup-")]
        if snapshots:
            embed = create_embed(f"Snapshots  ·  `{container_name}`", f"Found **{len(snapshots)}** snapshot(s) on `{node}`", C_INFO)
            embed.add_field(name="📸 Snapshots", value="\n".join(f"• `{s}`" for s in snapshots), inline=False)
        else:
            embed = create_info_embed("No Snapshots", f"No snapshots found for `{container_name}`")
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Error", str(e)))

@bot.command(name='exec')
@is_admin()
async def execute_command(ctx, container_name: str, *, command: str):
    node = "local"
    for vl in vps_data.values():
        for v in vl:
            if v['container_name'] == container_name:
                node = v.get('node', 'local')
                break
    await ctx.send(embed=create_info_embed("Executing", f"Running in `{container_name}` on `{node}`…"))
    try:
        stdout, stderr, rc = await routed_docker_exec(node, container_name, command, timeout=30)
        embed = create_embed(f"Exec Output  ·  `{container_name}`", f"```bash\n{command}\n```", C_DARK)
        if stdout:
            out = stdout[:1000] + "\n…(truncated)" if len(stdout) > 1000 else stdout
            embed.add_field(name="📤 stdout", value=f"```\n{out}\n```", inline=False)
        if stderr:
            err = stderr[:1000] + "\n…(truncated)" if len(stderr) > 1000 else stderr
            embed.add_field(name="⚠️ stderr", value=f"```\n{err}\n```", inline=False)
        embed.add_field(name="🔄 Exit Code", value=f"`{rc}`", inline=True)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Execution Failed", str(e)))

@bot.command(name='stop-vps-all')
@is_admin()
async def stop_all_vps(ctx):
    await ctx.send(embed=create_warning_embed("Stop All VPS",
        "⚠️ This will stop **ALL** running Docker VPS across all nodes. Continue?"))

    class ConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)

        @discord.ui.button(label="Stop All", style=discord.ButtonStyle.danger)
        async def confirm(self, interaction: discord.Interaction, item: discord.ui.Button):
            await interaction.response.defer()
            stopped = 0
            errors  = []
            for vl in vps_data.values():
                for v in vl:
                    if v.get('status') == 'running':
                        node = v.get('node', 'local')
                        try:
                            await routed_execute_docker(node, f"docker stop {v['container_name']}")
                            v['status'] = 'stopped'
                            stopped += 1
                        except Exception as e:
                            errors.append(f"`{v['container_name']}`: {e}")
            await async_save_data()
            embed = create_success_embed("All VPS Stopped", f"Stopped **{stopped}** containers.")
            if errors:
                embed.add_field(name="Errors", value="\n".join(errors[:5]), inline=False)
            await interaction.followup.send(embed=embed)

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, item: discord.ui.Button):
            await interaction.response.edit_message(embed=create_info_embed("Cancelled", "Operation cancelled."))

    await ctx.send(view=ConfirmView())

@bot.command(name='update-all')
@is_admin()
async def update_all_vps(ctx):
    """Push the latest hardware presentation to all running Docker and LXC instances."""
    running_docker = [
        (uid, v) for uid, vl in vps_data.items()
        for v in vl if v.get('status') == 'running'
    ]
    running_lxc = [
        (uid, v) for uid, vl in lxc_data.items()
        for v in vl if v.get('status') == 'running'
    ]
    running = [
        ("docker", uid, v) for uid, v in running_docker
    ] + [
        ("lxc", uid, v) for uid, v in running_lxc
    ]
    if not running:
        await ctx.send(embed=create_info_embed("Nothing to Update", "No running containers found."))
        return
    status_msg = await ctx.send(embed=create_info_embed(
        "Updating All VPS",
        f"Pushing hardware spoof to **{len(running)}** running container(s)…"
    ))
    success = 0
    errors  = []
    semaphore = asyncio.Semaphore(3)

    async def update_one(kind, uid, vps):
        async with semaphore:
            ram_mb  = int(str(vps.get('ram','1GB')).replace('GB','')) * 1024
            cpu_cnt = int(vps.get('cpu', 1))
            disk_gb = int(str(vps.get('storage','30GB')).replace('GB',''))
            node = vps.get('node', 'local')
            if kind == "lxc":
                await apply_fake_hardware_lxc(
                    node, vps['container_name'], ram_mb, cpu_cnt, disk_gb
                )
            else:
                await apply_fake_hardware(
                    node, vps['container_name'], ram_mb, cpu_cnt, disk_gb
                )

    results = await asyncio.gather(
        *(update_one(kind, uid, vps) for kind, uid, vps in running),
        return_exceptions=True,
    )
    for (kind, uid, vps), result in zip(running, results):
        try:
            if isinstance(result, Exception):
                raise result
            success += 1
        except Exception as e:
            errors.append(f"`{vps['container_name']}` ({kind}): {e}")
    embed = create_success_embed("Update Complete", f"Updated **{success}/{len(running)}** containers.")
    embed.add_field(
        name="What was applied",
        value="Docker + LXC · Disk spoof · CPU/RAM presentation · Systemd boot service",
        inline=False
    )
    if errors:
        embed.add_field(name=f"❌ Failed ({len(errors)})", value="\n".join(errors[:10]), inline=False)
    await status_msg.edit(embed=embed)

async def get_node_resource_snapshot(node: str) -> str:
    """Collect a small on-demand snapshot without a resident polling process."""
    script = (
        "printf 'LOAD=%s\\n' \"$(awk '{print $1}' /proc/loadavg 2>/dev/null || echo '?')\"; "
        "printf 'MEM=%sMB/%sMB\\n' "
        "\"$(awk '/MemTotal:/ {print int($2/1024)}' /proc/meminfo)\" "
        "\"$(awk '/MemAvailable:/ {print int($2/1024)}' /proc/meminfo)\"; "
        "printf 'DISK=%s\\n' \"$(df -P -h / 2>/dev/null | awk 'NR==2 {print $3 \"/\" $2 \" (\" $5 \")\"}')\"; "
        "docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}' "
        "2>/dev/null || true"
    )
    return str(await routed_execute_docker(
        node, f"bash -lc {shlex.quote(script)}", timeout=20
    ) or "")

@bot.command(name='resource-monitor', aliases=['resources'])
@is_admin()
async def resource_monitor(ctx, node: str = None):
    """Admin-only, on-demand host and instance resource overview."""
    target_nodes = [node] if node else ["local"] + sorted(connected_nodes)
    invalid = [n for n in target_nodes if n != "local" and n not in connected_nodes]
    if invalid:
        await ctx.send(embed=create_error_embed(
            "Node Not Found", f"Unknown or offline node(s): {', '.join(invalid)}"
        ))
        return

    status_msg = await ctx.send(embed=create_info_embed(
        "Resource Monitor", "Collecting a low-overhead snapshot…"
    ))
    embed = create_embed("Resource Monitor", "Admin-only on-demand infrastructure view", C_INFO)
    records = [
        ("Docker", v) for values in vps_data.values() for v in values
        if v.get("node", "local") in target_nodes
    ] + [
        ("LXC", c) for values in lxc_data.values() for c in values
        if c.get("node", "local") in target_nodes
    ]

    for target in target_nodes:
        try:
            raw = await get_node_resource_snapshot(target)
            lines = raw.strip().splitlines()
            host_lines = [line for line in lines if line.startswith(("LOAD=", "MEM=", "DISK="))]
            stats = [line for line in lines if "|" in line]
            value = "\n".join(host_lines) or "Host metrics unavailable"
            if stats:
                value += "\n\n**Docker live usage**\n" + "\n".join(
                    f"`{line.replace('|', ' · ')}`" for line in stats[:8]
                )
            embed.add_field(name=f"Node · {target}", value=value[:1024], inline=False)
        except Exception as exc:
            embed.add_field(name=f"Node · {target}", value=f"Unavailable: `{exc}`", inline=False)

    if records:
        allocation_lines = []
        for kind, record in records[:30]:
            allocation_lines.append(
                f"`{record.get('container_name','?')}` · {kind} · "
                f"dedicated `{record.get('ram','?')} RAM / {record.get('cpu','?')} CPU / "
                f"{record.get('storage','?')} disk` · {record.get('status','unknown')}"
            )
        embed.add_field(
            name="Dedicated instance allocations",
            value="\n".join(allocation_lines)[:1024],
            inline=False,
        )
    else:
        embed.add_field(name="Dedicated instance allocations", value="No instances found.", inline=False)
    embed.set_footer(text=f"{BRAND_NAME}  ›  Snapshot only; no automatic container stops")
    await status_msg.edit(embed=embed)

@bot.command(name='cpu-monitor')
@is_admin()
async def cpu_monitor_control(ctx, action: str = "status"):
    global cpu_monitor_active
    if action.lower() == "status":
        embed = create_embed("CPU Monitor", "", C_INFO if cpu_monitor_active else C_WARNING)
        embed.add_field(name="Status",    value="🟢 **Active**" if cpu_monitor_active else "🔴 **Inactive**", inline=True)
        embed.add_field(name="Threshold", value=f"`{CPU_THRESHOLD}%`",   inline=True)
        embed.add_field(name="Interval",  value=f"`{CHECK_INTERVAL}s`",  inline=True)
        embed.add_field(name="Scope",     value="Passive host load sampling", inline=True)
        embed.add_field(
            name="Safety",
            value="Alerts only — running containers are never stopped automatically.",
            inline=False,
        )
        await ctx.send(embed=embed)
    elif action.lower() == "enable":
        cpu_monitor_active = True
        await ctx.send(embed=create_success_embed(
            "CPU Monitor Enabled",
            "Passive high-load logging is active; containers will not be stopped.",
        ))
    elif action.lower() == "disable":
        cpu_monitor_active = False
        await ctx.send(embed=create_warning_embed(
            "CPU Monitor Disabled", "Passive high-load logging is OFF."
        ))
    else:
        await ctx.send(embed=create_error_embed("Invalid Action", "Use: `!cpu-monitor <status|enable|disable>`"))

# ─── LXC Commands ─────────────────────────────────────────────────────────────

@bot.command(name='lxc-create')
@is_admin()
async def lxc_create(ctx, user: discord.Member, ram: int, cpu: int, disk: int = 30, node: str = "local"):
    """Create an LXC container for a user — !lxc-create @user <ram_GB> <cpu> <disk_GB> [node]"""
    if ram <= 0 or cpu <= 0 or disk <= 0:
        await ctx.send(embed=create_error_embed("Invalid Specs",
            "RAM, CPU and Disk must be positive integers.\n"
            "Usage: `!lxc-create @user <ram_GB> <cpu> <disk_GB> [node]`"))
        return

    # Check lxc-create is available
    if not shutil.which("lxc-create"):
        await ctx.send(embed=create_error_embed("LXC Not Available",
            "The `lxc-create` command was not found on this node.\n"
            "Install LXC: `dnf install -y lxc lxc-templates lxc-extra`"))
        return

    user_id = str(user.id)
    if user_id not in lxc_data:
        lxc_data[user_id] = []
    lxc_count      = len(lxc_data[user_id]) + 1
    container_name = f"lxc-{user_id[-8:]}-{lxc_count}"
    ram_mb         = ram * 1024
    password       = generate_password()

    deploy_embed = create_lxc_embed(
        "Deploying LXC Container",
        f"Creating Ubuntu 22.04 LXC container for {user.mention}…\n"
        f"⚙️ Setting up cgroups, networking, and SSH"
    )
    deploy_embed.add_field(name="🧠 RAM",  value=f"`{ram}GB`",        inline=True)
    deploy_embed.add_field(name="⚙️ CPU",  value=f"`{cpu} Core(s)`",  inline=True)
    deploy_embed.add_field(name="💾 Disk", value=f"`{disk}GB`",       inline=True)
    deploy_embed.add_field(name="🌐 Node", value=node_badge(node),    inline=True)
    deploy_embed.add_field(
        name="⏱️ Estimated Time",
        value="3–8 minutes (template download + setup)",
        inline=False
    )
    await ctx.send(embed=deploy_embed)

    try:
        await create_lxc_container(container_name, ram_mb, cpu, disk, password, node=node)

        # Try to get the IP
        ip = await lxc_get_container_ip(container_name, node)

        lxc_info = {
            "container_name": container_name,
            "ram": f"{ram}GB",
            "cpu": str(cpu),
            "storage": f"{disk}GB",
            "node": node,
            "status": "running",
            "created_at": datetime.now().isoformat(),
            "expires": "Never",
            "ssh_password": password,
            "ip": ip
        }
        lxc_data[user_id].append(lxc_info)
        await async_save_data()

        if ctx.guild:
            vps_role = await get_or_create_vps_role(ctx.guild)
            if vps_role:
                try:
                    await user.add_roles(vps_role, reason="LXC container granted")
                except discord.Forbidden:
                    pass

        embed = create_lxc_embed(
            "LXC Container Deployed",
            f"{user.mention} — your LXC container is ready! Check your **DMs** for access details."
        )
        embed.add_field(name="👤 Owner",     value=user.mention,          inline=True)
        embed.add_field(name="🆔 LXC ID",   value=f"`#{lxc_count}`",     inline=True)
        embed.add_field(name="📦 Container", value=f"`{container_name}`", inline=True)
        embed.add_field(name="🧠 RAM",       value=f"`{ram}GB`",          inline=True)
        embed.add_field(name="⚙️ CPU",       value=f"`{cpu} Core(s)`",    inline=True)
        embed.add_field(name="💾 Disk",      value=f"`{disk}GB`",         inline=True)
        embed.add_field(name="🌐 Node",      value=node_badge(node),      inline=True)
        embed.add_field(
            name="🔐 Secure Access",
            value="Use `!lxc-manage` → **SSH** or **SSHX**. "
                  "The container IP and root password stay private.",
            inline=False
        )
        await ctx.send(embed=embed)

        # DM the user
        try:
            dm_embed = create_lxc_embed(
                "Your LXC Container is Ready",
                "Connect to your new Linux container below."
            )
            dm_embed.add_field(name="🆔 LXC ID",      value=f"`#{lxc_count}`",     inline=True)
            dm_embed.add_field(name="🧠 RAM",          value=f"`{ram}GB`",          inline=True)
            dm_embed.add_field(name="⚙️ CPU",          value=f"`{cpu} Core(s)`",    inline=True)
            dm_embed.add_field(
                name="🔐 Secure Access",
                value="Use `!lxc-manage` in the server and click **SSH** or **SSHX**. "
                      "The bot creates a temporary relay, so the LXC IP and root password "
                      "are never exposed.",
                inline=False
            )
            dm_embed.add_field(
                name="📌 Getting Started",
                value="**1.** Run `!lxc-manage` in the server\n"
                      "**2.** Choose **SSH** or **SSHX**\n"
                      "**3.** Use the temporary terminal session",
                inline=False
            )
            await user.send(embed=dm_embed)
        except discord.Forbidden:
            pass

    except Exception as e:
        await ctx.send(embed=create_error_embed("LXC Deployment Failed", f"Error: {str(e)}"))

@bot.command(name='lxc-manage')
async def lxc_manage(ctx, user: discord.Member = None):
    """Manage your LXC containers — admins can pass @user"""
    if user:
        if not (str(ctx.author.id) == str(MAIN_ADMIN_ID) or str(ctx.author.id) in admin_data.get("admins", [])):
            await ctx.send(embed=create_error_embed("Access Denied", "Only admins can manage others' LXC containers."))
            return
        uid = str(user.id)
        cl  = lxc_data.get(uid, [])
        if not cl:
            await ctx.send(embed=create_error_embed("No LXC Found", f"{user.mention} has no LXC containers."))
            return
        view = LXCManageView(str(ctx.author.id), cl, is_admin=True, owner_id=uid)
        await ctx.send(embed=view.initial_embed, view=view)
    else:
        uid = str(ctx.author.id)
        cl  = lxc_data.get(uid, [])
        if not cl:
            await ctx.send(embed=create_lxc_embed(
                "No LXC Containers",
                "You don't have any LXC containers yet.\nContact an admin to get one!"
            ))
            return
        view = LXCManageView(uid, cl)
        await ctx.send(embed=view.initial_embed, view=view)

@bot.command(name='lxc-delete')
@is_admin()
async def lxc_delete(ctx, user: discord.Member, lxc_number: int, *, reason: str = "No reason provided"):
    """Delete a user's LXC container (Admin only)"""
    user_id = str(user.id)
    if user_id not in lxc_data or lxc_number < 1 or lxc_number > len(lxc_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid LXC", "Invalid LXC number or user has no containers."))
        return
    c = lxc_data[user_id][lxc_number - 1]
    container_name = c["container_name"]
    node = c.get("node", "local")
    await ctx.send(embed=create_info_embed("Deleting LXC", f"Destroying `{container_name}`…"))
    try:
        await remove_lxc_container(node, container_name)
        del lxc_data[user_id][lxc_number - 1]
        if not lxc_data[user_id]:
            del lxc_data[user_id]
        await async_save_data()
        embed = create_success_embed("LXC Container Deleted")
        embed.add_field(name="👤 Owner",    value=user.mention,          inline=True)
        embed.add_field(name="🆔 LXC ID",   value=f"`#{lxc_number}`",    inline=True)
        embed.add_field(name="📦 Container",value=f"`{container_name}`", inline=True)
        embed.add_field(name="📝 Reason",   value=reason,                inline=False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Delete Failed", str(e)))

@bot.command(name='lxc-list')
@is_admin()
async def lxc_list(ctx):
    """List all LXC containers (Admin only)"""
    if not lxc_data:
        await ctx.send(embed=create_lxc_embed("No LXC Containers", "No LXC deployments exist yet."))
        return
    embed = create_lxc_embed("All LXC Containers", "Full overview of all LXC deployments")
    total = running = 0
    for uid, cl in lxc_data.items():
        try:
            u = await bot.fetch_user(int(uid))
            uname = u.name
        except Exception:
            uname = f"Unknown ({uid})"
        for i, c in enumerate(cl):
            total += 1
            if c.get('status') == 'running':
                running += 1
            embed.add_field(
                name=f"{'🟢' if c.get('status')=='running' else '🔴'}  {uname}  ·  LXC #{i+1}",
                value=(
                    f"📦 `{c['container_name']}`\n"
                    f"🌐 IP: `{c.get('ip','?')}`  ·  {node_badge(c.get('node','local'))}\n"
                    f"🧠 `{c.get('ram','?')}`  ⚙️ `{c.get('cpu','?')}c`  💾 `{c.get('storage','?')}`"
                ),
                inline=True
            )
    embed.description = f"**Total:** `{total}` containers  ·  🟢 Running: `{running}`  ·  🔴 Stopped: `{total - running}`"
    await ctx.send(embed=embed)

@bot.command(name='lxc-exec')
@is_admin()
async def lxc_exec_cmd(ctx, container_name: str, *, command: str):
    """Execute a command inside an LXC container (Admin only)"""
    node = "local"
    for containers in lxc_data.values():
        for container in containers:
            if container.get("container_name") == container_name:
                node = container.get("node", "local")
                break
    await ctx.send(embed=create_info_embed("Executing in LXC", f"Running in `{container_name}`…"))
    try:
        stdout, stderr, rc = await routed_lxc_exec(
            node, container_name, command, timeout=60
        )
        embed = create_lxc_embed(f"LXC Exec  ·  `{container_name}`", f"```bash\n{command}\n```")
        if stdout:
            out = stdout[:1000] + "\n…(truncated)" if len(stdout) > 1000 else stdout
            embed.add_field(name="📤 stdout", value=f"```\n{out}\n```", inline=False)
        if stderr:
            err = stderr[:1000] + "\n…(truncated)" if len(stderr) > 1000 else stderr
            embed.add_field(name="⚠️ stderr", value=f"```\n{err}\n```", inline=False)
        embed.add_field(name="🔄 Exit Code", value=f"`{rc}`", inline=True)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Exec Failed", str(e)))

@bot.command(name='lxc-restart')
@is_admin()
async def lxc_restart_cmd(ctx, container_name: str):
    """Restart an LXC container (Admin only)"""
    node = "local"
    for containers in lxc_data.values():
        for container in containers:
            if container.get("container_name") == container_name:
                node = container.get("node", "local")
                break
    await ctx.send(embed=create_info_embed("Restarting LXC", f"Restarting `{container_name}`…"))
    try:
        await routed_lxc_host_command(
            node, f"lxc-stop -n {shlex.quote(container_name)}", timeout=20
        )
        await asyncio.sleep(2)
        await routed_lxc_host_command(
            node, f"lxc-start -n {shlex.quote(container_name)}", timeout=30
        )
        # Update status
        for uid, cl in lxc_data.items():
            for c in cl:
                if c['container_name'] == container_name:
                    c['status'] = 'running'
                    await async_save_data()
                    break
        await ctx.send(embed=create_success_embed("LXC Restarted", f"`{container_name}` is back online!"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Restart Failed", str(e)))

@bot.command(name='lxc-info')
@is_admin()
async def lxc_info_cmd(ctx, container_name: str):
    """Get detailed info on an LXC container (Admin only)"""
    found = None
    found_user = None
    for uid, cl in lxc_data.items():
        for c in cl:
            if c['container_name'] == container_name:
                found = c
                try:
                    found_user = await bot.fetch_user(int(uid))
                except Exception:
                    pass
                break
        if found:
            break
    if not found:
        await ctx.send(embed=create_error_embed("Not Found", f"No LXC container named `{container_name}`"))
        return

    # Get live lxc-info
    live_info = ""
    try:
        result = await routed_lxc_host_command(
            found.get("node", "local"),
            f"lxc-info -n {shlex.quote(container_name)}",
            timeout=10
        )
        live_info = str(result)[:500]
    except Exception as e:
        live_info = f"Could not fetch live info: {e}"

    embed = create_lxc_embed(
        f"LXC Info  ·  `{container_name}`",
        f"Owned by: {found_user.mention if found_user else 'Unknown'}"
    )
    embed.add_field(name="🌐 IP",      value=f"`{found.get('ip','?')}`",                 inline=True)
    embed.add_field(name="📊 Status",  value=status_badge(found.get('status','unknown')), inline=True)
    embed.add_field(name="🌐 Node",    value=node_badge(found.get('node','local')),       inline=True)
    embed.add_field(name="🧠 RAM",     value=f"`{found.get('ram','?')}`",                 inline=True)
    embed.add_field(name="⚙️ CPU",     value=f"`{found.get('cpu','?')}` Core(s)",         inline=True)
    embed.add_field(name="💾 Disk",    value=f"`{found.get('storage','?')}`",             inline=True)
    embed.add_field(name="📅 Created", value=f"`{found.get('created_at','?')[:10]}`",     inline=True)
    if live_info:
        embed.add_field(name="📋 Live Info", value=f"```\n{live_info}\n```", inline=False)
    await ctx.send(embed=embed)

# ─── Admin commands ────────────────────────────────────────────────────────────

@bot.command(name='admin-add')
@is_main_admin()
async def admin_add(ctx, user: discord.Member):
    uid = str(user.id)
    if uid not in admin_data["admins"]:
        admin_data["admins"].append(uid)
        await async_save_data()
    await ctx.send(embed=create_success_embed("Admin Added", f"{user.mention} has been promoted to admin."))

@bot.command(name='admin-remove')
@is_main_admin()
async def admin_remove(ctx, user: discord.Member):
    uid = str(user.id)
    if uid in admin_data["admins"]:
        admin_data["admins"].remove(uid)
        await async_save_data()
    await ctx.send(embed=create_success_embed("Admin Removed", f"{user.mention} is no longer an admin."))

@bot.command(name='admin-list')
@is_main_admin()
async def admin_list(ctx):
    admins = []
    for aid in admin_data.get("admins", []):
        try:
            u = await bot.fetch_user(int(aid))
            admins.append(f"• {u.mention} (`{u.name}`)")
        except Exception:
            admins.append(f"• Unknown (`{aid}`)")
    embed = create_info_embed("Admin List", "\n".join(admins) if admins else "No admins configured.")
    await ctx.send(embed=embed)

# ─── User commands ─────────────────────────────────────────────────────────────

@bot.command(name='rename-vps')
async def rename_vps(ctx, vps_number: int, *, new_name: str):
    uid = str(ctx.author.id)
    vl  = vps_data.get(uid, [])
    if not vl or vps_number < 1 or vps_number > len(vl):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    if len(new_name) > 30:
        await ctx.send(embed=create_error_embed("Name Too Long", "Nickname must be 30 characters or less."))
        return
    vl[vps_number - 1]["nickname"] = new_name
    await async_save_data()
    await ctx.send(embed=create_success_embed("VPS Renamed", f"VPS #{vps_number} is now called **{new_name}**!"))

@bot.command(name='vps-note')
async def vps_note(ctx, vps_number: int, *, note: str):
    uid = str(ctx.author.id)
    vl  = vps_data.get(uid, [])
    if not vl or vps_number < 1 or vps_number > len(vl):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    vl[vps_number - 1]["note"] = note[:200]
    await async_save_data()
    await ctx.send(embed=create_success_embed("Note Saved", f"Note added to VPS #{vps_number}:\n> {note[:200]}"))

@bot.command(name='ping-vps')
async def ping_vps(ctx, vps_number: int):
    uid = str(ctx.author.id)
    vl  = vps_data.get(uid, [])
    if not vl or vps_number < 1 or vps_number > len(vl):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    vps       = vl[vps_number - 1]
    container = vps["container_name"]
    node      = vps.get("node", "local")
    nickname  = vps.get("nickname", f"VPS #{vps_number}")
    msg = await ctx.send(embed=create_info_embed("Pinging…", f"Checking `{container}` on node `{node}`…"))
    start = time.time()
    try:
        output  = await routed_execute_docker(node, f"docker inspect --format={{{{.State.Running}}}} {container}", timeout=10)
        elapsed = int((time.time() - start) * 1000)
        is_run  = str(output).strip() == "true"
        if is_run:
            embed = create_success_embed("Pong!", f"**{nickname}** is alive!\n⚡ Response time: `{elapsed}ms`  ·  Node: {node_badge(node)}")
        else:
            embed = create_error_embed("No Response", f"**{nickname}** container is not running.")
        await msg.edit(embed=embed)
    except Exception as e:
        await msg.edit(embed=create_error_embed("Ping Failed", str(e)))

@bot.command(name='uptime-vps')
async def uptime_vps(ctx, vps_number: int):
    uid = str(ctx.author.id)
    vl  = vps_data.get(uid, [])
    if not vl or vps_number < 1 or vps_number > len(vl):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    vps       = vl[vps_number - 1]
    container = vps["container_name"]
    node      = vps.get("node", "local")
    nickname  = vps.get("nickname", f"VPS #{vps_number}")
    try:
        output     = await routed_execute_docker(node, f"docker inspect --format={{{{.State.StartedAt}}}} {container}", timeout=10)
        started_at = datetime.fromisoformat(str(output).strip()[:19])
        delta      = datetime.utcnow() - started_at
        d, rem     = delta.days, delta.seconds
        h, rem     = divmod(rem, 3600)
        m, s       = divmod(rem, 60)
        uptime_str = f"{d}d {h}h {m}m {s}s"
        embed = create_success_embed(f"Uptime  ·  {nickname}", f"Container has been running for:\n```\n{uptime_str}\n```")
        embed.add_field(name="🕐 Started At", value=f"`{started_at.isoformat()[:19]} UTC`", inline=True)
        embed.add_field(name="🌐 Node",       value=node_badge(node),                        inline=True)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Uptime Error", str(e)))

@bot.command(name='myinfo')
async def my_info(ctx):
    uid = str(ctx.author.id)
    vl  = vps_data.get(uid, [])
    cl  = lxc_data.get(uid, [])
    embed = create_embed(
        f"Dashboard  ·  {ctx.author.display_name}",
        "",
        C_PRIMARY,
        thumbnail=str(ctx.author.display_avatar.url)
    )
    embed.add_field(name="👤 Account",   value=f"Joined Discord: `{ctx.author.created_at.strftime('%Y-%m-%d')}`", inline=True)
    embed.add_field(name="🖥️ Docker VPS",value=f"**{len(vl)}** deployment(s)",                                    inline=True)
    embed.add_field(name="📦 LXC",       value=f"**{len(cl)}** container(s)",                                     inline=True)
    if vl:
        vps_text = ""
        for i, v in enumerate(vl):
            icon = "🟢" if v.get("status") == "running" else "🔴"
            name = v.get("nickname", f"VPS {i+1}")
            note = f"  _{v['note']}_" if v.get("note") else ""
            vps_text += f"{icon} **{name}** (`{v['container_name']}`)  ·  {node_badge(v.get('node','local'))}{note}\n"
        embed.add_field(name="🖥️ Your Docker VPS", value=vps_text, inline=False)
    if cl:
        lxc_text = ""
        for i, c in enumerate(cl):
            icon = "🟢" if c.get("status") == "running" else "🔴"
            lxc_text += f"{icon} LXC {i+1} (`{c['container_name']}`)  ·  IP: `{c.get('ip','?')}`\n"
        embed.add_field(name="📦 Your LXC Containers", value=lxc_text, inline=False)
    if not vl and not cl:
        embed.add_field(name="🤷 No deployments yet", value="Contact an admin to get your VPS or LXC container!", inline=False)
    await ctx.send(embed=embed)

@bot.command(name='botstatus')
async def bot_status(ctx):
    delta   = datetime.now() - BOT_START_TIME
    d, rem  = delta.days, delta.seconds
    h, rem  = divmod(rem, 3600)
    m, _    = divmod(rem, 60)
    t_vps   = sum(len(v) for v in vps_data.values())
    r_vps   = sum(1 for vl in vps_data.values() for v in vl if v.get("status") == "running")
    t_lxc   = sum(len(v) for v in lxc_data.values())
    r_lxc   = sum(1 for vl in lxc_data.values() for v in vl if v.get("status") == "running")
    embed = create_embed("Bot Status  ·  GunpointNodes", "Real-time infrastructure overview", C_SUCCESS)
    embed.add_field(name="⏱️ Uptime",     value=f"`{d}d {h}h {m}m`",                              inline=True)
    embed.add_field(name="📡 Latency",    value=f"`{round(bot.latency * 1000)}ms`",               inline=True)
    embed.add_field(name="🔧 Maintenance",value="🔴 ON" if maintenance_mode else "🟢 OFF",         inline=True)
    embed.add_field(name="🖥️ Docker VPS", value=f"**{t_vps}** total  ·  🟢 {r_vps} running",     inline=True)
    embed.add_field(name="📦 LXC",        value=f"**{t_lxc}** total  ·  🟢 {r_lxc} running",     inline=True)
    embed.add_field(name="🌐 Nodes",      value=f"**{len(connected_nodes) + 1}** online",          inline=True)
    embed.add_field(name="👥 Users",      value=f"**{len(set(list(vps_data.keys()) + list(lxc_data.keys())))}** with deployments", inline=True)
    embed.add_field(name="🖥️ Host",       value="iDRAC 8  ·  AlmaLinux",                           inline=True)
    await ctx.send(embed=embed)

@bot.command(name='announce')
@is_admin()
async def announce(ctx, *, message: str):
    all_users = set(list(vps_data.keys()) + list(lxc_data.keys()))
    sent = failed = 0
    ann_embed = create_embed(
        f"Announcement from {BRAND_NAME}",
        message,
        C_WARNING
    )
    ann_embed.add_field(name="📣 From", value=f"**{BRAND_NAME} Team**  ({ctx.author.mention})", inline=False)
    status_msg = await ctx.send(embed=create_info_embed("Sending Announcement", f"Broadcasting to {len(all_users)} users…"))
    for uid in all_users:
        try:
            u = await bot.fetch_user(int(uid))
            await u.send(embed=ann_embed)
            sent += 1
            await asyncio.sleep(0.5)
        except Exception:
            failed += 1
    await status_msg.edit(embed=create_success_embed("Announcement Sent",
        f"✅ Delivered to **{sent}** users\n❌ Failed: **{failed}** (DMs closed)"))

@bot.command(name='maintenance')
@is_admin()
async def maintenance_toggle(ctx, mode: str):
    global maintenance_mode
    if mode.lower() == "on":
        maintenance_mode = True
        await bot.change_presence(status=discord.Status.idle,
            activity=discord.Activity(type=discord.ActivityType.watching, name="🔴 Under Maintenance"))
        await ctx.send(embed=create_warning_embed("Maintenance Mode ON",
            "• All commands blocked for non-admins\n• Bot status set to Idle"))
    elif mode.lower() == "off":
        maintenance_mode = False
        await bot.change_presence(status=discord.Status.online,
            activity=discord.Activity(type=discord.ActivityType.watching, name="GunpointNodes | Infrastructure"))
        await ctx.send(embed=create_success_embed("Maintenance Mode OFF", "Bot is back to normal operation."))
    else:
        await ctx.send(embed=create_error_embed("Invalid", "Use: `!maintenance on` or `!maintenance off`"))

@bot.check
async def maintenance_check(ctx):
    if not maintenance_mode:
        return True
    uid = str(ctx.author.id)
    is_adm = uid == str(MAIN_ADMIN_ID) or uid in admin_data.get("admins", [])
    if is_adm and ctx.command and ctx.command.name == 'maintenance':
        return True
    if not is_adm:
        await ctx.send(embed=create_warning_embed(
            "Under Maintenance",
            "The bot is currently under maintenance. All commands are temporarily disabled."
        ))
        return False
    return True

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if isinstance(message.channel, discord.DMChannel):
        if message.content.startswith(bot.command_prefix):
            await message.channel.send(embed=create_warning_embed(
                "DM Commands Disabled",
                "Bot commands are **not available in DMs**.\n\n"
                "Please use commands in the **server channel** instead.\n"
                "👉 Go to the server and run your commands there!"
            ))
            return
    if maintenance_mode and isinstance(message.channel, discord.TextChannel):
        uid = str(message.author.id)
        is_adm = uid == str(MAIN_ADMIN_ID) or uid in admin_data.get("admins", [])
        if not is_adm and message.content.startswith(bot.command_prefix):
            await message.channel.send(embed=create_warning_embed(
                "Under Maintenance",
                "The bot is under maintenance. Commands are disabled for users."
            ))
            return
    await bot.process_commands(message)

@bot.command(name='stats')
async def stats_alias(ctx):
    uid = str(ctx.author.id)
    if uid == str(MAIN_ADMIN_ID) or uid in admin_data.get("admins", []):
        await server_stats(ctx)
    else:
        await ctx.send(embed=create_error_embed("Access Denied", "Admin only."))

@bot.command(name='mangage')
async def manage_typo(ctx):
    await ctx.send(embed=create_info_embed("Did you mean…", "Try `!manage` to manage your VPS."))

# ─── Help System ──────────────────────────────────────────────────────────────

def build_help_pages(is_user_admin, is_user_main_admin):
    pages = {}

    user_embed = create_embed("User Commands", "All commands available to VPS and LXC owners:", C_SUCCESS)
    user_embed.add_field(name="🖥️ Docker VPS", value=(
        "`!manage` — Manage your VPS (Start/Stop/SSH/Reinstall)\n"
        "`!manage [@user]` — Admin: manage another user's VPS\n"
        "`!share-user @user <#>` — Share VPS access\n"
        "`!share-ruser @user <#>` — Revoke shared access\n"
        "`!manage-shared @owner <#>` — Access a shared VPS"
    ), inline=False)
    user_embed.add_field(name="📦 LXC Containers", value=(
        "`!lxc-manage` — Manage your LXC container (Start/Stop/SSH/SSHX/Delete)\n"
        "`!lxc-manage [@user]` — Admin: manage another user's LXC\n"
        "`!lxc-list` — Admin overview of all LXC containers"
    ), inline=False)
    user_embed.add_field(name="🔧 Tools", value=(
        "`!rename-vps <#> <name>` — Give your VPS a nickname\n"
        "`!vps-note <#> <note>` — Add a note to your VPS\n"
        "`!ping-vps <#>` — Check if VPS is alive\n"
        "`!uptime-vps <#>` — Check VPS uptime\n"
        "`!myinfo` — Your personal dashboard\n"
        "`!botstatus` — Bot stats and uptime"
    ), inline=False)
    pages["user"] = user_embed

    extras_embed = create_embed("Expiry & Codes", "Expiry management and redeem codes:", 0xff6b9d)
    extras_embed.add_field(name="⏳ Expiry", value=(
        "`!checkexpire` — Check your VPS expiry dates\n"
        "`!checkexpire [@user]` — Admin: check another user's expiry"
    ), inline=False)
    extras_embed.add_field(name="🎟️ Redeem Codes", value=(
        "`!redeem <code>` — Redeem a code to get a VPS instantly"
    ), inline=False)
    pages["extras"] = extras_embed

    if is_user_admin:
        admin_embed = create_embed("Admin Panel", "Admin-only commands:", C_ERROR)
        admin_embed.add_field(name="🔀 Create Mode", value=(
            "`!set-mode lxc` — Switch `!create` to deploy LXC containers (AlmaLinux)\n"
            "`!set-mode docker` — Switch `!create` to deploy Docker containers\n"
            "`!set-mode` — Show the currently active mode"
        ), inline=False)
        admin_embed.add_field(name="🖥️ Docker / LXC Unified", value=(
            "`!create @user <ram> <cpu> <disk> [node]` — Deploy VPS using active mode\n"
            "`!delete-vps @user <#> [reason]` — Delete a user's VPS\n"
            "`!restart-vps <container>` — Restart a VPS\n"
            "`!stop-vps-all` — Emergency stop ALL VPS\n"
            "`!exec <container> <cmd>` — Run command in VPS\n"
            "`!update-all` — Push spoof update to all running VPS\n"
            "`!backup-vps <container>` — Create Docker snapshot\n"
            "`!restore-vps <container> <snapshot>` — Restore from snapshot\n"
            "`!list-snapshots <container>` — List snapshots"
        ), inline=False)
        admin_embed.add_field(name="📦 LXC Control", value=(
            "`!lxc-create @user <ram> <cpu> <disk> [node]` — Deploy LXC container (explicit)\n"
            "`!lxc-delete @user <#> [reason]` — Destroy an LXC container\n"
            "`!lxc-restart <container>` — Restart LXC container\n"
            "`!lxc-exec <container> <cmd>` — Run command in LXC\n"
            "`!lxc-info <container>` — Detailed LXC info\n"
            "`!lxc-list` — All LXC containers overview\n"
            "`!resource-monitor [node]` — On-demand host and dedicated-instance resources"
        ), inline=False)
        admin_embed.add_field(name="🌐 Node Management", value=(
            "`!nodes` — List all connected nodes\n"
            "`!node-kick <name>` — Disconnect a node (Main Admin)\n"
            "`!node-purge <name>` — Remove node + all its VPS records"
        ), inline=False)
        admin_embed.add_field(name="📊 Info & Tools", value=(
            "`!list-vps` — Interactive VPS panel (paginated)\n"
            "`!list-all` — Plain text overview of all VPS\n"
            "`!lxc-list` — All LXC containers overview\n"
            "`!userinfo @user` — Full user info\n"
            "`!serverstats` — Infrastructure statistics\n"
            "`!vpsinfo [container]` — VPS details\n"
            "`!announce <msg>` — DM all VPS owners\n"
            "`!cpu-monitor <status|enable|disable>` — CPU monitor\n"
            "`!maintenance <on|off>` — Toggle maintenance mode"
        ), inline=False)
        admin_embed.add_field(name="⏳ Expiry Management", value=(
            "`!setexpire @user <#> <days>` — Set VPS expiry\n"
            "`!extendexpire @user <#> <days>` — Extend expiry\n"
            "`!removeexpire @user <#>` — Remove expiry\n"
            "`!checkexpire [@user]` — Check expiry status"
        ), inline=False)
        admin_embed.add_field(name="🎟️ Redeem Codes", value=(
            "`!createcode` — Create a VPS redeem code (modal)\n"
            "`!listcodes` — View all active codes\n"
            "`!deletecode <code>` — Delete a code"
        ), inline=False)
        pages["admin"] = admin_embed

    if is_user_main_admin:
        mainadmin_embed = create_embed("Main Admin", "Exclusive main admin commands:", C_GOLD)
        mainadmin_embed.add_field(name="👥 Admin Management", value=(
            "`!admin-add @user` — Promote to admin\n"
            "`!admin-remove @user` — Remove admin\n"
            "`!admin-list` — View all admins"
        ), inline=False)
        mainadmin_embed.add_field(name="🌐 Node Management", value=(
            "`!node-kick <name>` — Forcefully disconnect a node\n"
            "`!node-purge <name>` — Remove node + wipe all its VPS records"
        ), inline=False)
        pages["mainadmin"] = mainadmin_embed

    return pages

@bot.command(name='help')
async def show_help(ctx):
    uid = str(ctx.author.id)
    is_adm      = uid == str(MAIN_ADMIN_ID) or uid in admin_data.get("admins", [])
    is_main_adm = uid == str(MAIN_ADMIN_ID)
    pages = build_help_pages(is_adm, is_main_adm)

    options = [
        discord.SelectOption(label="User Commands",     description="VPS manage, LXC, share, tools",   value="user",      emoji="👤"),
        discord.SelectOption(label="Expiry & Codes",    description="Expiry check, redeem codes",       value="extras",    emoji="⏳"),
    ]
    if is_adm:
        options.append(discord.SelectOption(label="Admin Panel",  description="Docker + LXC + node commands", value="admin",     emoji="🛡️"))
    if is_main_adm:
        options.append(discord.SelectOption(label="Main Admin",   description="Admin management",              value="mainadmin", emoji="👑"))

    class HelpSelect(discord.ui.Select):
        def __init__(self):
            super().__init__(placeholder="📂 Browse categories…", options=options, min_values=1, max_values=1)
        async def callback(self, interaction: discord.Interaction):
            embed = pages.get(self.values[0], pages["user"])
            await interaction.response.edit_message(embed=embed, view=self.view)

    class HelpView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=180)
            self.add_item(HelpSelect())

    await ctx.send(embed=pages["user"], view=HelpView())

# ─── Redeem Code System ────────────────────────────────────────────────────────

class CreateCodeModal(discord.ui.Modal, title="Create Redeem Code"):
    code_input    = discord.ui.TextInput(label="Code (blank = auto-generate)", placeholder="e.g. GUNPOINT2025", required=False, max_length=32)
    max_uses_input= discord.ui.TextInput(label="Max Uses (0 = unlimited)", placeholder="e.g. 10", default="1", max_length=5)
    specs_input   = discord.ui.TextInput(label="RAM (GB), CPU cores, Disk (GB) — comma-sep", placeholder="e.g. 2, 1, 20", max_length=20)
    node_input    = discord.ui.TextInput(label="Node name (blank = local)", placeholder="local", required=False, max_length=32)
    expiry_input  = discord.ui.TextInput(label="VPS Expiry Days (0 = never)", placeholder="e.g. 30", default="0", max_length=5)

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx

    async def on_submit(self, interaction: discord.Interaction):
        try:
            parts = [p.strip() for p in self.specs_input.value.split(',')]
            if len(parts) != 3:
                raise ValueError
            ram, cpu, disk = int(parts[0]), int(parts[1]), int(parts[2])
            if ram <= 0 or cpu <= 0 or disk <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                embed=create_error_embed("Invalid Specs", "Example: `2, 1, 20`"), ephemeral=True)
            return
        try:
            max_uses = int(self.max_uses_input.value.strip())
            if max_uses < 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                embed=create_error_embed("Invalid Max Uses", "Enter a whole number ≥ 0."), ephemeral=True)
            return
        try:
            expiry_days = int(self.expiry_input.value.strip())
            if expiry_days < 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                embed=create_error_embed("Invalid Expiry", "Enter a whole number ≥ 0."), ephemeral=True)
            return
        node = self.node_input.value.strip() or "local"
        if node != "local" and node not in connected_nodes:
            await interaction.response.send_message(
                embed=create_error_embed("Node Not Found", f"Node `{node}` is not connected."), ephemeral=True)
            return
        raw_code = self.code_input.value.strip() or ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
        code_key = raw_code.upper()
        if code_key in codes_data:
            await interaction.response.send_message(
                embed=create_error_embed("Code Exists", f"Code `{code_key}` already exists."), ephemeral=True)
            return
        codes_data[code_key] = {
            "ram": ram, "cpu": cpu, "disk": disk, "node": node,
            "max_uses": max_uses, "uses": 0,
            "expires_days": expiry_days,
            "created_by": str(self.ctx.author.id),
            "created_at": datetime.utcnow().isoformat(),
            "redeemed_by": []
        }
        await asyncio.get_event_loop().run_in_executor(None, save_data)
        embed = create_success_embed("Redeem Code Created", f"Code `{code_key}` is ready to share!")
        embed.add_field(name="🔑 Code",     value=f"`{code_key}`",                                         inline=True)
        embed.add_field(name="🔢 Max Uses", value="Unlimited" if max_uses == 0 else str(max_uses),         inline=True)
        embed.add_field(name="🧠 RAM",      value=f"`{ram}GB`",                                             inline=True)
        embed.add_field(name="⚙️ CPU",      value=f"`{cpu} Core(s)`",                                      inline=True)
        embed.add_field(name="💾 Disk",     value=f"`{disk}GB`",                                            inline=True)
        embed.add_field(name="🌐 Node",     value=node_badge(node),                                         inline=True)
        embed.add_field(name="⏳ VPS Expiry",value=f"**{expiry_days}d** from redemption" if expiry_days > 0 else "♾️ Never", inline=True)
        await interaction.response.send_message(embed=embed)

class CreateCodeView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=120)
        self.ctx = ctx

    @discord.ui.button(label="Open Code Setup", style=discord.ButtonStyle.primary)
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != str(self.ctx.author.id):
            await interaction.response.send_message("This is not your panel.", ephemeral=True)
            return
        await interaction.response.send_modal(CreateCodeModal(self.ctx))
        self.stop()

class CreateExpireModal(discord.ui.Modal, title="Set VPS Expiry"):
    days_input = discord.ui.TextInput(label="Days until expiry (0 = never)", placeholder="e.g. 30", default="30", min_length=1, max_length=4)

    def __init__(self, vps_info, container_name, owner: discord.Member):
        super().__init__()
        self.vps_info = vps_info
        self.container_name = container_name
        self.owner = owner

    async def on_submit(self, interaction: discord.Interaction):
        try:
            d = int(self.days_input.value.strip())
        except ValueError:
            await interaction.response.send_message(
                embed=create_error_embed("Invalid", "Enter a whole number of days."), ephemeral=True)
            return
        if d <= 0:
            await interaction.response.send_message(
                embed=create_info_embed("No Expiry", f"VPS `{self.container_name}` has no expiry date."), ephemeral=True)
            return
        import datetime as dt
        exp_date = (datetime.utcnow() + dt.timedelta(days=d)).isoformat()
        self.vps_info['expires'] = exp_date
        await asyncio.get_event_loop().run_in_executor(None, save_data)
        await interaction.response.send_message(
            embed=create_success_embed("Expiry Set",
                f"`{self.container_name}` expires on `{exp_date[:10]}` ({d}d from now)."),
            ephemeral=True)
        try:
            await self.owner.send(embed=create_warning_embed("VPS Expiry Set",
                f"Your VPS `{self.container_name}` expires on **{exp_date[:10]}** ({d} days).\n"
                "Contact an admin before then to extend it!"))
        except discord.Forbidden:
            pass

class ExpirePromptView(discord.ui.View):
    def __init__(self, admin_id: str, vps_info: dict, container_name: str, owner: discord.Member):
        super().__init__(timeout=120)
        self.admin_id = admin_id
        self.vps_info = vps_info
        self.container_name = container_name
        self.owner = owner

    @discord.ui.button(label="Set Expiry", style=discord.ButtonStyle.primary)
    async def set_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.admin_id:
            await interaction.response.send_message("This is not your action.", ephemeral=True)
            return
        await interaction.response.send_modal(CreateExpireModal(self.vps_info, self.container_name, self.owner))
        self.stop()

    @discord.ui.button(label="Skip (No Expiry)", style=discord.ButtonStyle.secondary)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.admin_id:
            await interaction.response.send_message("This is not your action.", ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=create_info_embed("Skipped", f"VPS `{self.container_name}` has no expiry date."), view=None)
        self.stop()

@bot.command(name='createcode')
@is_admin()
async def create_code(ctx):
    embed = create_info_embed(
        "Create Redeem Code",
        "Click the button below to open the code setup panel.\n\n"
        "**Configure:**\n"
        "• Custom code name (or auto-generate)\n"
        "• Max redemptions (0 = unlimited)\n"
        "• VPS specs: RAM, CPU, Disk\n"
        "• Target node\n"
        "• VPS expiry days (0 = never)"
    )
    await ctx.send(embed=embed, view=CreateCodeView(ctx))

@bot.command(name='redeem')
async def redeem_code(ctx, code: str = None):
    if not code:
        await ctx.send(embed=create_error_embed("Missing Code", "Usage: `!redeem <code>`"))
        return
    code_key = code.upper()
    if code_key not in codes_data:
        await ctx.send(embed=create_error_embed("Invalid Code", f"Code `{code_key}` doesn't exist or has been revoked."))
        return
    entry = codes_data[code_key]
    uid = str(ctx.author.id)
    if uid in entry.get("redeemed_by", []):
        await ctx.send(embed=create_error_embed("Already Redeemed", "You've already redeemed this code."))
        return
    if entry["max_uses"] > 0 and entry["uses"] >= entry["max_uses"]:
        await ctx.send(embed=create_error_embed("Code Exhausted", f"Code `{code_key}` has reached its maximum uses."))
        return
    ram, cpu, disk, node = entry["ram"], entry["cpu"], entry["disk"], entry.get("node", "local")
    if node != "local" and node not in connected_nodes:
        await ctx.send(embed=create_error_embed("Node Offline", f"Node `{node}` assigned to this code is offline."))
        return
    if uid not in vps_data:
        vps_data[uid] = []
    vps_count = len(vps_data[uid]) + 1
    container_name = f"vps-{uid}-{vps_count}"
    ram_mb = ram * 1024
    password = generate_password()

    entry["uses"] += 1
    entry.setdefault("redeemed_by", []).append(uid)
    await async_save_data()

    await ctx.send(embed=create_info_embed(
        "Redeeming Code…",
        f"Deploying your VPS — this may take a moment.\n"
        f"🧠 `{ram}GB RAM`  ⚙️ `{cpu} Core(s)`  💾 `{disk}GB Disk`  🌐 Node: {node_badge(node)}"
    ))
    try:
        ssh_port = await create_docker_container(container_name, ram_mb, cpu, 0, password, disk_gb=disk, node=node)
        import datetime as dt
        expiry_days = entry.get("expires_days", 0)
        exp_date = (datetime.utcnow() + dt.timedelta(days=expiry_days)).isoformat() if expiry_days > 0 else "Never"
        vps_info = {
            "container_name": container_name,
            "ram": f"{ram}GB", "cpu": str(cpu), "storage": f"{disk}GB",
            "node": node, "status": "running",
            "ssh_port": ssh_port,
            "created_at": datetime.now().isoformat(),
            "expires": exp_date, "ssh_password": password,
            "shared_with": [], "redeemed_with": code_key
        }
        vps_data[uid].append(vps_info)
        await async_save_data()

        if ctx.guild:
            vps_role = await get_or_create_vps_role(ctx.guild)
            if vps_role:
                try:
                    await ctx.author.add_roles(vps_role)
                except discord.Forbidden:
                    pass

        embed = create_embed("Code Redeemed!", f"{ctx.author.mention} — your VPS is live! Check your **DMs**.", C_SUCCESS)
        embed.add_field(name="🔑 Code",     value=f"`{code_key}`",                 inline=True)
        embed.add_field(name="🆔 VPS ID",   value=f"`#{vps_count}`",              inline=True)
        embed.add_field(name="📦 Container",value=f"`{container_name}`",           inline=True)
        embed.add_field(name="🧠 RAM",      value=f"`{ram}GB`",                   inline=True)
        embed.add_field(name="⚙️ CPU",      value=f"`{cpu} Core(s)`",             inline=True)
        embed.add_field(name="💾 Disk",     value=f"`{disk}GB`",                  inline=True)
        embed.add_field(name="⏳ Expiry",   value=f"`{exp_date[:10]}`" if exp_date != "Never" else "♾️ Never", inline=True)
        await ctx.send(embed=embed)

        try:
            tmate_cmd = await get_tmate_session(container_name, node=node)
            dm_embed = create_embed("Your VPS is Ready!", "Connect now using the command below.", C_PRIMARY)
            dm_embed.add_field(name="🔑 Root Password", value=f"```\n{password}\n```", inline=False)
            dm_embed.add_field(name="🔗 SSH Command",   value=f"```bash\n{tmate_cmd}\n```", inline=False)
            dm_embed.add_field(name="📌 Quick Start", value="**1.** Copy the SSH command\n**2.** Paste in terminal\n**3.** Enter your password!", inline=False)
            await ctx.author.send(embed=dm_embed)
        except discord.Forbidden:
            pass

    except Exception as e:
        entry["uses"] = max(0, entry["uses"] - 1)
        if uid in entry.get("redeemed_by", []):
            entry["redeemed_by"].remove(uid)
        await async_save_data()
        await ctx.send(embed=create_error_embed("Redemption Failed",
            f"VPS creation failed — your slot has been released, you can try again.\nError: {str(e)}"))

@bot.command(name='listcodes')
@is_admin()
async def list_codes(ctx):
    if not codes_data:
        await ctx.send(embed=create_info_embed("No Codes", "No redeem codes exist. Use `!createcode` to make one."))
        return
    embed = create_info_embed("Redeem Codes", f"**{len(codes_data)}** code(s) active:")
    for code_key, entry in codes_data.items():
        uses_str   = f"{entry['uses']}/{entry['max_uses']}" if entry['max_uses'] > 0 else f"{entry['uses']}/∞"
        expiry_str = f"{entry.get('expires_days',0)}d" if entry.get('expires_days',0) > 0 else "Never"
        embed.add_field(
            name=f"🔑 `{code_key}`",
            value=(
                f"**Specs:** `{entry['ram']}GB RAM`  ·  `{entry['cpu']} CPU`  ·  `{entry['disk']}GB`\n"
                f"**Node:** {node_badge(entry.get('node','local'))}  ·  **Uses:** `{uses_str}`  ·  **Expiry:** `{expiry_str}`"
            ),
            inline=False
        )
    await ctx.send(embed=embed)

@bot.command(name='deletecode')
@is_admin()
async def delete_code(ctx, code: str = None):
    if not code:
        await ctx.send(embed=create_error_embed("Missing Code", "Usage: `!deletecode <code>`"))
        return
    code_key = code.upper()
    if code_key not in codes_data:
        await ctx.send(embed=create_error_embed("Not Found", f"Code `{code_key}` doesn't exist."))
        return
    entry = codes_data.pop(code_key)
    await async_save_data()
    await ctx.send(embed=create_success_embed("Code Deleted",
        f"Code `{code_key}` deleted. It had **{entry['uses']}** use(s)."))

# ─── Expire System ─────────────────────────────────────────────────────────────

@bot.command(name='setexpire')
@is_admin()
async def set_expire(ctx, user: discord.Member, vps_number: int, days: int):
    uid = str(user.id)
    vl  = vps_data.get(uid, [])
    if not vl or vps_number < 1 or vps_number > len(vl):
        await ctx.send(embed=create_error_embed("Not Found", f"{user.mention} has no VPS #{vps_number}."))
        return
    import datetime as dt
    exp_date = (datetime.utcnow() + dt.timedelta(days=days)).isoformat()
    vl[vps_number - 1]['expires'] = exp_date
    await async_save_data()
    vps_name = vl[vps_number - 1]['container_name']
    await ctx.send(embed=create_success_embed("Expiry Set",
        f"{user.mention}'s **VPS #{vps_number}** (`{vps_name}`) expires on `{exp_date[:10]}` ({days}d from now)."))
    try:
        await user.send(embed=create_warning_embed("VPS Expiry Set",
            f"Your **VPS #{vps_number}** (`{vps_name}`) expires on **{exp_date[:10]}** ({days} days).\n"
            "Contact an admin to extend it!"))
    except discord.Forbidden:
        pass

@bot.command(name='extendexpire')
@is_admin()
async def extend_expire(ctx, user: discord.Member, vps_number: int, days: int):
    uid = str(user.id)
    vl  = vps_data.get(uid, [])
    if not vl or vps_number < 1 or vps_number > len(vl):
        await ctx.send(embed=create_error_embed("Not Found", f"{user.mention} has no VPS #{vps_number}."))
        return
    import datetime as dt
    vps  = vl[vps_number - 1]
    cur  = vps.get('expires', 'Never')
    base = datetime.utcnow()
    if cur and cur != 'Never':
        try:
            b = datetime.fromisoformat(cur)
            if b > base:
                base = b
        except Exception:
            pass
    new_exp = (base + dt.timedelta(days=days)).isoformat()
    vps['expires'] = new_exp
    await async_save_data()
    vps_name = vps['container_name']
    await ctx.send(embed=create_success_embed("Expiry Extended",
        f"{user.mention}'s **VPS #{vps_number}** (`{vps_name}`) extended by **{days}d**. New expiry: `{new_exp[:10]}`"))
    try:
        await user.send(embed=create_success_embed("VPS Extended",
            f"Your **VPS #{vps_number}** extended by **{days} days**! New expiry: **{new_exp[:10]}**"))
    except discord.Forbidden:
        pass

@bot.command(name='checkexpire')
async def check_expire(ctx, user: discord.Member = None):
    is_adm = str(ctx.author.id) == str(MAIN_ADMIN_ID) or str(ctx.author.id) in admin_data.get("admins", [])
    target = user if (user and is_adm) else ctx.author
    uid    = str(target.id)
    if uid not in vps_data or not vps_data[uid]:
        await ctx.send(embed=create_error_embed("Not Found", f"{target.mention} has no VPS."))
        return
    embed = create_info_embed(f"VPS Expiry  ·  {target.display_name}", "")
    for i, vps in enumerate(vps_data[uid]):
        expires = vps.get('expires', 'Never')
        if expires and expires != 'Never':
            try:
                days_left = (datetime.fromisoformat(expires) - datetime.utcnow()).days
                if days_left < 0:
                    status = f"❌ **EXPIRED** {abs(days_left)}d ago"
                elif days_left <= 3:
                    status = f"⚠️ Expires in **{days_left}d** — `{expires[:10]}`"
                else:
                    status = f"✅ Expires on `{expires[:10]}` ({days_left}d left)"
            except Exception:
                status = expires
        else:
            status = "♾️ Never (no expiry set)"
        embed.add_field(
            name=f"VPS #{i+1}  ·  `{vps['container_name']}`  ·  {node_badge(vps.get('node','local'))}",
            value=status, inline=False
        )
    await ctx.send(embed=embed)

@bot.command(name='removeexpire')
@is_admin()
async def remove_expire(ctx, user: discord.Member, vps_number: int):
    uid = str(user.id)
    vl  = vps_data.get(uid, [])
    if not vl or vps_number < 1 or vps_number > len(vl):
        await ctx.send(embed=create_error_embed("Not Found", f"{user.mention} has no VPS #{vps_number}."))
        return
    vl[vps_number - 1]['expires'] = 'Never'
    await async_save_data()
    vps_name = vl[vps_number - 1]['container_name']
    await ctx.send(embed=create_success_embed("Expiry Removed",
        f"{user.mention}'s **VPS #{vps_number}** (`{vps_name}`) expiry set to **Never**."))

# ─── Auto expire checker ───────────────────────────────────────────────────────

@tasks.loop(hours=1)
async def auto_expire_check():
    now = datetime.utcnow()
    for user_id, vps_list in list(vps_data.items()):
        for vps in vps_list:
            expires = vps.get('expires', 'Never')
            if not expires or expires == 'Never':
                continue
            try:
                exp_dt    = datetime.fromisoformat(expires)
                days_left = (exp_dt - now).days
                node      = vps.get('node', 'local')
                if days_left == 3:
                    try:
                        u = await bot.fetch_user(int(user_id))
                        await u.send(embed=create_warning_embed(
                            "VPS Expiring Soon",
                            f"Your VPS `{vps['container_name']}` expires in **3 days** on `{expires[:10]}`!\n"
                            "Contact an admin to extend it."
                        ))
                    except Exception:
                        pass
                elif days_left == 1:
                    try:
                        u = await bot.fetch_user(int(user_id))
                        await u.send(embed=create_error_embed(
                            "VPS Expiring Tomorrow",
                            f"Your VPS `{vps['container_name']}` expires **tomorrow** (`{expires[:10]}`)!\n"
                            "Contact an admin **immediately** to avoid losing access."
                        ))
                    except Exception:
                        pass
                elif days_left < 0:
                    try:
                        await routed_execute_docker(node, f"docker stop {vps['container_name']}")
                        vps['status'] = 'stopped'
                    except Exception:
                        pass
                    try:
                        u = await bot.fetch_user(int(user_id))
                        await u.send(embed=create_error_embed(
                            "VPS Expired",
                            f"Your VPS `{vps['container_name']}` has expired and been **stopped**.\n"
                            "Contact an admin to renew it."
                        ))
                    except Exception:
                        pass
            except Exception:
                continue
    await async_save_data()

# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set.")
    bot.run(BOT_TOKEN)

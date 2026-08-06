#!/usr/bin/env python3
"""
UGREEN DX4600 Pro Hardware Monitor & Control Panel
Temperature monitoring, fan control, and LED management.
Zero external dependencies — uses Python stdlib only.
"""

import json
import os
import gzip
import glob
import time
import threading
import subprocess
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HOST = "0.0.0.0"
PORT = 9092
REFRESH_INTERVAL = 3  # seconds between temp checks in auto mode

# ─── Cache ───
_cache = {}
_cache_ttl = {
    "temps": 2,
    "fans": 2,
    "leds": 5,
    "services": 10,
    "health": 15,
    "dkms": 60,
    "disk_serials": 60,
}

# Cap POST body to a reasonable size (largest legit endpoint is /api/config/leds).
MAX_POST_BODY = 65536

def cached(key, fn, *args):
    """Return cached result if within TTL, else recalculate."""
    now = time.time()
    entry = _cache.get(key)
    if entry and (now - entry["ts"]) < _cache_ttl.get(key, 5):
        return entry["data"]
    data = fn(*args)
    _cache[key] = {"ts": now, "data": data}
    return data

# ─── Hardware discovery (robust to kernel/sysfs renumbering) ───
def _discover_hwmon(name):
    """Find the first hwmon device whose chip name matches `name`."""
    try:
        for hwmon in os.listdir("/sys/class/hwmon"):
            try:
                with open(f"/sys/class/hwmon/{hwmon}/name") as f:
                    if f.read().strip() == name:
                        return f"/sys/class/hwmon/{hwmon}"
            except OSError:
                continue
    except OSError:
        pass
    return None


def _discover_fan_cdevs():
    """Discover all cooling devices that are actual fans (type=Fan) AND have
    a writable cur_state. Robust to kernel renumbering while skipping CPU
    processor cooling zones and driver-managed zones like intel_powerclamp."""
    cdevs = []
    try:
        for d in sorted(os.listdir("/sys/class/thermal")):
            if not d.startswith("cooling_device"):
                continue
            path = f"/sys/class/thermal/{d}"
            if not os.path.exists(f"{path}/cur_state"):
                continue
            try:
                with open(f"{path}/type") as f:
                    t = f.read().strip()
            except OSError:
                continue
            if t == "Fan":
                cdevs.append(path)
    except OSError:
        pass
    if not cdevs:
        # Fallback to historical DX4600 Pro layout if discovery finds nothing
        cdevs = [f"/sys/class/thermal/cooling_device{i}" for i in range(4, 9)]
    return cdevs


def _discover_primary_netif():
    """Find the primary LAN interface (skip lo, prefer en*/eth*)."""
    try:
        for d in sorted(os.listdir("/sys/class/net")):
            if d == "lo":
                continue
            if d.startswith(("en", "eth")):
                return d
    except OSError:
        pass
    return "enp2s0"


# Cached at startup; resolved lazily on first call to avoid slowing import.
_I2CDETECT_PATH = None


def _get_i2cdetect():
    """Locate the i2cdetect binary, memoizing the result."""
    global _I2CDETECT_PATH
    if _I2CDETECT_PATH is not None:
        return _I2CDETECT_PATH
    for c in ("/usr/local/bin/i2cdetect", "/usr/sbin/i2cdetect", "/usr/bin/i2cdetect"):
        if os.path.exists(c):
            _I2CDETECT_PATH = c
            return _I2CDETECT_PATH
    return None


# Resolved at startup; fall back to historical defaults if discovery misses.
HWMON_CORETEMP = _discover_hwmon("coretemp") or _discover_hwmon("k10temp") or "/sys/class/hwmon/hwmon2"
HWMON_NVME = _discover_hwmon("nvme") or "/sys/class/hwmon/hwmon1"
HWMON_ACPI = _discover_hwmon("acpi") or "/sys/class/hwmon/hwmon0"
FAN_CDEVS = _discover_fan_cdevs()
_PRIMARY_NETIF = _discover_primary_netif()
LED_BASE = "/sys/class/leds"
LED_NAMES = ["power", "netdev", "disk1", "disk2", "disk3", "disk4"]

# ─── Service / config paths ───
LED_SERVICES = [
    ("ugreen-led-init",        "LED 驱动加载"),
    ("ugreen-probe-leds",      "LED 硬件探测"),
    ("ugreen-power-led",       "电源 LED"),
    ("ugreen-diskiomon",       "磁盘 IO 监控"),
    (f"ugreen-netdevmon@{_PRIMARY_NETIF}", "网络 IO 监控"),
]
# Convenience: the netdev service unit (used by led_repair/dkms_rebuild)
NETDEV_SERVICE = f"ugreen-netdevmon@{_PRIMARY_NETIF}"
LEDS_CONF_PATH = "/etc/ugreen-leds.conf"
DKMS_PKG_NAME = "led-ugreen"
DKMS_PKG_VERSION = "0.3"

# ─── Fan auto-curve config ───
# (temp_threshold, fans_to_activate)
FAN_CURVE = [
    (45, 0),   # < 45°C → all off (silent)
    (50, 1),   # 45-50°C → 1 fan
    (55, 2),   # 50-55°C → 2 fans
    (60, 3),   # 55-60°C → 3 fans
    (65, 4),   # 60-65°C → 4 fans
    (70, 5),   # 65-70°C → 5 fans (max)
]

# ─── Global state ───
fan_mode = "auto"  # "auto" or "manual"
fan_manual_count = 0  # how many fans on in manual mode
auto_thread = None
auto_running = False


def read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def write_int(path, val):
    try:
        with open(path, "w") as f:
            f.write(str(val))
        return True
    except OSError:
        return False


def read_str(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def write_str(path, val):
    try:
        with open(path, "w") as f:
            f.write(val)
        return True
    except OSError:
        return False


# ─── Temperature reading ───
def get_temps():
    temps = {}

    # CPU package + cores
    pkg_temp = read_int(f"{HWMON_CORETEMP}/temp1_input")
    if pkg_temp is not None:
        temps["cpu_package"] = pkg_temp / 1000.0

    for i in range(2, 6):
        t = read_int(f"{HWMON_CORETEMP}/temp{i}_input")
        label = read_str(f"{HWMON_CORETEMP}/temp{i}_label") or f"Core {i-2}"
        if t is not None:
            temps[f"cpu_core{i-2}"] = {"temp": t / 1000.0, "label": label}

    # NVMe
    nvme_temp = read_int(f"{HWMON_NVME}/temp1_input")
    if nvme_temp is not None:
        temps["nvme"] = nvme_temp / 1000.0

    # ACPI sensor
    acpi_temp = read_int(f"{HWMON_ACPI}/temp1_input")
    if acpi_temp is not None:
        temps["acpi"] = acpi_temp / 1000.0

    return temps


# ─── Fan control ───
def get_fan_status():
    fans = []
    for i, cdev in enumerate(FAN_CDEVS):
        cur = read_int(f"{cdev}/cur_state")
        fans.append({"id": i, "on": cur == 1 if cur is not None else False})
    return fans


def set_fans(count):
    """Turn on `count` fans (0..len(FAN_CDEVS)). Skip sysfs writes if the state
    hasn't changed — auto_loop calls this every 3s and idle writes are pure noise."""
    global _last_fan_state
    count = max(0, min(len(FAN_CDEVS), count))
    new_state = tuple(1 if i < count else 0 for i in range(len(FAN_CDEVS)))
    if new_state == _last_fan_state:
        return count
    _last_fan_state = new_state
    for i, cdev in enumerate(FAN_CDEVS):
        write_int(f"{cdev}/cur_state", new_state[i])
    _cache.pop("fans", None)  # Invalidate so next poll sees the change
    return count


# Last fan state we wrote to sysfs; used to skip redundant writes
_last_fan_state = None


def get_cpu_package_temp():
    t = read_int(f"{HWMON_CORETEMP}/temp1_input")
    return t / 1000.0 if t is not None else 0


def fan_auto_tick():
    """One iteration of auto fan control."""
    temp = get_cpu_package_temp()
    count = 0
    for threshold, fans in FAN_CURVE:
        if temp >= threshold:
            count = fans
        else:
            break
    set_fans(count)
    return temp, count


def auto_loop():
    global auto_running
    auto_running = True
    while auto_running and fan_mode == "auto":
        fan_auto_tick()
        time.sleep(REFRESH_INTERVAL)
    auto_running = False


def start_auto():
    global auto_thread
    if auto_thread and auto_thread.is_alive():
        return
    auto_thread = threading.Thread(target=auto_loop, daemon=True)
    auto_thread.start()


def stop_auto():
    global auto_running
    auto_running = False


# ─── LED control ───
def get_led_status():
    leds = []
    for name in LED_NAMES:
        base = f"{LED_BASE}/{name}"
        if not os.path.isdir(base):
            continue
        brightness = read_int(f"{base}/brightness") or 0
        max_b = read_int(f"{base}/max_brightness") or 255
        status_line = read_str(f"{base}/status")
        color = read_str(f"{base}/color")
        blink_type = read_str(f"{base}/blink_type")
        parts = status_line.split() if status_line else []
        state = parts[0] if parts else "unknown"
        r, g, b = 255, 255, 255
        if color:
            cp = color.split()
            if len(cp) >= 3:
                r, g, b = int(cp[0]), int(cp[1]), int(cp[2])
        leds.append({
            "name": name,
            "brightness": brightness,
            "max_brightness": max_b,
            "state": state,
            "r": r, "g": g, "b": b,
            "blink_type": blink_type,
        })
    return leds


def set_led(name, **kwargs):
    base = f"{LED_BASE}/{name}"
    if not os.path.isdir(base):
        return False
    if "brightness" in kwargs:
        # Clamp to valid LED sysfs range (0..max_brightness, typically 0..255)
        b = int(kwargs["brightness"])
        max_b = read_int(f"{base}/max_brightness") or 255
        b = max(0, min(b, max_b))
        write_int(f"{base}/brightness", b)
    if "color" in kwargs:
        r, g, b = kwargs["color"]
        # Clamp each channel to 0..255 — prevents garbage in sysfs
        r, g, b = (max(0, min(255, int(v))) for v in (r, g, b))
        write_str(f"{base}/color", f"{r} {g} {b}")
    if "blink_type" in kwargs:
        # Only allow short ASCII strings (no shell metacharacters)
        s = str(kwargs["blink_type"])[:64]
        if all(c.isalnum() or c in " -_" for c in s):
            write_str(f"{base}/blink_type", s)
    # Invalidate the LED cache so the next read reflects the change
    _cache.pop("leds", None)
    return True


def sync_all_leds_to_config():
    """Read current LED states from sysfs and batch-write colors/brightnesses to /etc/ugreen-leds.conf."""
    # Collect unique (color_key, brightness_key, r, g, b, brightness) per LED group
    updates = []  # list of (color_key, brightness_key, r, g, b, brightness)
    seen_keys = set()

    for name in LED_NAMES:
        base = f"{LED_BASE}/{name}"
        if not os.path.isdir(base):
            continue

        color_key = brightness_key = None
        if name == "power":
            color_key, brightness_key = "COLOR_POWER", "BRIGHTNESS_POWER"
        elif name == "netdev":
            color_key, brightness_key = "COLOR_NETDEV_NORMAL", "BRIGHTNESS_NETDEV_LED"
        elif name.startswith("disk"):
            color_key, brightness_key = "COLOR_DISK_HEALTH", "BRIGHTNESS_DISK_LEDS"

        if not color_key or color_key in seen_keys:
            continue
        seen_keys.add(color_key)

        color = read_str(f"{base}/color")
        brightness = read_int(f"{base}/brightness") or 0
        r, g, b = 255, 255, 255
        if color:
            parts = color.split()
            if len(parts) >= 3:
                r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
        updates.append((color_key, brightness_key, r, g, b, brightness))

    if not updates:
        return False, "没有找到 LED 设备"

    try:
        with open(LEDS_CONF_PATH) as f:
            lines = f.read().split("\n")

        for color_key, brightness_key, r, g, b, brightness in updates:
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("#") or not stripped or "=" not in stripped:
                    continue
                key = stripped.split("=", 1)[0].strip()
                if key == color_key:
                    lines[i] = f'{color_key}="{r} {g} {b}"'
                elif key == brightness_key:
                    lines[i] = f'{brightness_key}="{brightness}"'

        with open(LEDS_CONF_PATH, "w") as f:
            f.write("\n".join(lines))
        return True, f"已同步 {len(updates)} 组 LED 配置到配置文件"
    except OSError as e:
        return False, f"写入配置文件失败: {e}"


# ─── LED I2C bus auto-repair ───
# Robust I2C detection script (kept in sync with /usr/bin/ugreen-detect-i2c).
# Embedded here so the web one-click repair can self-heal even before a deploy.
UGREEN_DETECT_I2C_SCRIPT = r'''#!/bin/bash
# Auto-detect I2C bus for UGREEN LED controller (slave 0x3a)
# Robust against fnOS kernel upgrades that renumber I2C buses, and against the
# controller being visible on multiple adapters (DesignWare + SMBus I801).
SLAVE_ADDR=3a

# Fast path: ONLY trust an existing node on the SMBus I801 adapter. That is the
# bus that actually drives the LED class devices. A DesignWare (SoC) bus may
# expose a PHANTOM 0x3a with no LED class devices, so we must not fall back to
# it here — instead drop through to an active scan below.
for d in /sys/bus/i2c/devices/*-003a; do
    [ -e "$d" ] || continue
    b="${d##*/}"; b="${b%-003a}"; b="${b#i2c-}"
    bn=$(cat "/sys/bus/i2c/devices/i2c-${b}/name" 2>/dev/null || echo x)
    if echo "$bn" | grep -qi "SMBus I801"; then
        echo "$b"; exit 0
    fi
done

# Locate i2cdetect (install path has varied across fnOS images)
I2CDETECT=""
for c in /usr/local/bin/i2cdetect /usr/sbin/i2cdetect /usr/bin/i2cdetect; do
    [ -x "$c" ] && I2CDETECT="$c" && break
done
[ -z "$I2CDETECT" ] && exit 1

# Scan every I2C bus. Prefer SMBus I801; fall back to any non-GPU bus.
preferred=""
fallback=""
for p in /sys/bus/i2c/devices/i2c-*; do
    b="${p##*/i2c-}"
    n=$(cat "$p/name" 2>/dev/null || echo x)
    case $n in *gmbus*|*drm*|*DP*|*HDMI*|*DDC*|*designware*|*synopsys*) continue ;; esac
    r=$("$I2CDETECT" -y -r "$b" "$SLAVE_ADDR" "$SLAVE_ADDR" 2>/dev/null) || true
    if echo "$r" | grep -qiE "($SLAVE_ADDR|UU)"; then
        if echo "$n" | grep -qi "SMBus I801"; then
            preferred="$b"
        elif [ -z "$fallback" ]; then
            fallback="$b"
        fi
    fi
done
[ -n "$preferred" ] && { echo "$preferred"; exit 0; }
[ -n "$fallback" ] && { echo "$fallback"; exit 0; }
exit 1
'''

# Robust wrapper that also ensures the DKMS module is built/installed for the
# running kernel. Kept in sync with /usr/bin/ugreen-detect-i2c-and-load.
UGREEN_DETECT_I2C_AND_LOAD_SCRIPT = r'''#!/bin/bash
# Detect UGREEN LED controller bus and ensure driver + device node are loaded.
# Belt-and-suspenders fallback used by the web one-click repair.
K=$(uname -r)

# Ensure the module is available/built for this kernel
if ! /usr/sbin/modprobe -n led-ugreen >/dev/null 2>&1; then
    /usr/sbin/dkms add led-ugreen/0.3 >/dev/null 2>&1 || true
    /usr/sbin/dkms install led-ugreen/0.3 -k "$K" >/dev/null 2>&1 || true
fi

I2C=$(/usr/bin/ugreen-detect-i2c) || exit 1
echo "LED controller on i2c-${I2C}"

if [ -d "/sys/bus/i2c/devices/${I2C}-003a" ]; then
    echo "Device already exists on i2c-${I2C}"
    exit 0
fi

/usr/sbin/modprobe led-ugreen 2>/dev/null || true
sleep 0.5
echo led-ugreen 0x3a > "/sys/bus/i2c/devices/i2c-${I2C}/new_device"
'''

# Hardened led-init service with DKMS auto-install fallback. Kept in sync with
# /etc/systemd/system/ugreen-led-init.service.
UGREEN_LED_INIT_SERVICE = r'''[Unit]
Description=Load UGREEN LED driver and create I2C device
After=local-fs.target systemd-modules-load.service
Wants=systemd-modules-load.service

[Service]
Type=oneshot
RemainAfterExit=yes
# 异常断电冷重启时，SMBus I801 适配器(i2c 总线)尚未被内核/udev 枚举，
# 原逻辑会立即因找不到 0x3a 而退出，导致 LED 设备节点未创建、灯不亮。
# 修复:
#  1) 轮询等待 SMBus I801 总线就绪(最多 ~30s)，解决开机竞态；
#  2) 若 LED 设备节点已存在(热重启/fnOS 已加载)则直接跳过，避免拆掉正常工作的灯；
#  3) Restart=on-failure + TimeoutStartSec=120 兜底极慢的磁盘检查/枚举。
TimeoutStartSec=120
Restart=on-failure
RestartSec=5
ExecStart=/bin/sh -c 'K=$(uname -r); if ! /usr/sbin/modprobe -n led-ugreen >/dev/null 2>&1; then /usr/sbin/dkms add led-ugreen/0.3 >/dev/null 2>&1 || true; /usr/sbin/dkms install led-ugreen/0.3 -k "$K" >/dev/null 2>&1 || true; fi; for d in /sys/bus/i2c/devices/*-003a; do [ -e "$d" ] && exit 0; done; BUS=""; for i in $(seq 1 60); do for p in /sys/bus/i2c/devices/i2c-*; do n=$(cat "$p/name" 2>/dev/null || true); if echo "$n" | grep -qi "SMBus I801"; then BUS="${p##*/i2c-}"; break 2; fi; done; sleep 0.5; done; if [ -z "$BUS" ]; then BUS=$(/usr/bin/ugreen-detect-i2c 2>/dev/null); fi; if [ -z "$BUS" ]; then echo "LED controller not found on any I2C bus" >&2; exit 1; fi; /usr/sbin/modprobe led-ugreen 2>/dev/null || true; sleep 0.5; echo led-ugreen 0x3a > /sys/bus/i2c/devices/i2c-${BUS}/new_device 2>/dev/null || true; true'

[Install]
WantedBy=multi-user.target
'''


def detect_led_bus():
    """Return the I2C bus number hosting the UGREEN LED controller (0x3a), else None.
    Prefers the SMBus I801 adapter — that is the bus that actually drives the LED
    class devices on fnOS. A DesignWare bus may expose a phantom 0x3a with no LEDs."""
    try:
        devs = sorted(os.listdir("/sys/bus/i2c/devices"))
    except OSError:
        return None
    i2cdetect = _get_i2cdetect()
    if i2cdetect is None:
        return None
    preferred = None
    fallback = None
    for dev in devs:
        if not dev.startswith("i2c-"):
            continue
        bus = dev.replace("i2c-", "")
        try:
            name = read_str(f"/sys/bus/i2c/devices/{dev}/name")
        except Exception:
            name = ""
        if any(k in name.lower() for k in ("gmbus", "drm", "dp", "hdmi", "ddc", "designware", "synopsys")):
            continue
        try:
            r = subprocess.run(
                [i2cdetect, "-y", "-r", bus, "0x3a", "0x3a"],
                capture_output=True, text=True, timeout=5,
            )
            if "3a" in r.stdout or "UU" in r.stdout:
                if "smbus i801" in name.lower() and preferred is None:
                    preferred = bus
                elif fallback is None:
                    fallback = bus
        except Exception:
            pass
    return preferred or fallback


def led_repair():
    """Web one-click repair: self-heal the LED driver against fnOS upgrades.

    The full fix logic lives here so a single button click can recover from the
    two recurring failure modes:
      1. fnOS update renumbered the I2C bus   -> robust SMBus-I801-preferred detection
      2. fnOS update reverted our system files -> re-deploy them from embedded copies
    Plus a DKMS auto-install fallback for newly-installed kernels.
    """
    import time
    result = {"ok": False, "message": "", "steps": []}

    # Snapshot state so we can tell "already healthy" from "recovered".
    was_ok = any(os.path.isdir(f"{LED_BASE}/{n}") for n in LED_NAMES)

    # 1. Always re-deploy robust system files first (idempotent self-heal).
    result["steps"].append("① 固化健壮修复文件 (抵抗 fnOS 更新回退)...")
    _deploy_robust_files(result)

    # 2. Make sure the DKMS module is built/installed for this kernel.
    _ensure_dkms(result)

    # 3. Detect the I2C bus hosting the LED controller (prefers SMBus I801).
    bus = _robust_detect_bus(result)
    if bus is None:
        result["message"] = "未找到 LED 控制器 (0x3a)。请确认硬件连接，或重启系统后再点一次修复。"
        return result

    # 4. Overload systemd (service file may have changed) and (re)create the node.
    result["steps"].append("② 重载 systemd 并重建 LED 设备节点...")
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True, timeout=5)

    led_ok = any(os.path.isdir(f"{LED_BASE}/{n}") for n in LED_NAMES)
    if not led_ok:
        _create_device(bus, result)
        time.sleep(1)
        led_ok = any(os.path.isdir(f"{LED_BASE}/{n}") for n in LED_NAMES)

    # 5. Restart LED services (led-init recreates the node via the robust script).
    result["steps"].append("③ 重启 LED 相关服务...")
    _restart_led_services(result)
    time.sleep(1)

    # 6. Verify
    led_ok = any(os.path.isdir(f"{LED_BASE}/{n}") for n in LED_NAMES)
    if led_ok:
        leds = [n for n in LED_NAMES if os.path.isdir(f"{LED_BASE}/{n}")]
        if was_ok:
            result["ok"] = True
            result["message"] = ("系统本就正常，已重新固化健壮修复文件；"
                                  "后续 fnOS 升级后点此按钮即可一键自愈。")
        else:
            result["ok"] = True
            result["message"] = f"修复成功! 已在 i2c-{bus} 恢复 LED 设备: {', '.join(leds)}"
    else:
        result["ok"] = False
        result["message"] = "修复后仍未检测到 LED 设备，请尝试重启系统后再点一次修复。"

    # Drop cached LED/service snapshots so the next UI poll reflects changes
    _cache.pop("leds", None)
    _cache.pop("services", None)
    return result


def _deploy_robust_files(result):
    """Re-write the embedded robust files to their system locations.

    fnOS updates have been observed to revert these; re-deploying from the panel
    makes the web button a true self-heal without needing a manual deploy."""
    files = [
        ("/usr/bin/ugreen-detect-i2c", UGREEN_DETECT_I2C_SCRIPT, 0o755),
        ("/usr/bin/ugreen-detect-i2c-and-load", UGREEN_DETECT_I2C_AND_LOAD_SCRIPT, 0o755),
        ("/etc/systemd/system/ugreen-led-init.service", UGREEN_LED_INIT_SERVICE, 0o644),
    ]
    for path, content, mode in files:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
            os.chmod(path, mode)
            result["steps"].append(f"  ✓ 已固化 {path}")
        except OSError as e:
            result["steps"].append(f"  ✗ 写入 {path} 失败: {e}")


def _ensure_dkms(result):
    """Ensure the led-ugreen DKMS module is loadable for the running kernel."""
    r = subprocess.run(["/usr/sbin/modprobe", "-n", "led-ugreen"],
                       capture_output=True, text=True, timeout=10)
    if r.returncode == 0:
        result["steps"].append("  ✓ DKMS 模块已就绪，无需编译")
        return
    result["steps"].append("  ⚙ DKMS 模块缺失，自动为新内核编译安装...")
    kr = subprocess.run(["uname", "-r"], capture_output=True, text=True).stdout.strip()
    a = subprocess.run(["/usr/sbin/dkms", "add", "led-ugreen/0.3"],
                       capture_output=True, text=True, timeout=30)
    if a.returncode != 0:
        result["steps"].append(f"  (dkms add 返回 {a.returncode}, 可能已添加过)")
    i = subprocess.run(["/usr/sbin/dkms", "install", "led-ugreen/0.3", "-k", kr],
                       capture_output=True, text=True, timeout=180)
    result["steps"].append(f"  dkms install: {'OK' if i.returncode == 0 else 'FAIL'}")


def _robust_detect_bus(result):
    """Detect the LED bus: prefer the freshly-deployed robust script, fall back to Python."""
    try:
        r = subprocess.run(["/usr/bin/ugreen-detect-i2c"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            bus = r.stdout.strip().splitlines()[0].strip()
            result["steps"].append(f"  检测脚本定位到 LED 总线 i2c-{bus}")
            return bus
    except Exception:
        pass
    bus = detect_led_bus()
    if bus:
        result["steps"].append(f"  Python 回退检测定位到 i2c-{bus}")
    else:
        result["steps"].append("  ✗ 未能在任何 I2C 总线上找到 0x3a")
    return bus


def _create_device(bus, result):
    """Load the module and create the I2C device node on the given bus."""
    subprocess.run(["/usr/sbin/modprobe", "led-ugreen"], capture_output=True, timeout=10)
    try:
        with open(f"/sys/bus/i2c/devices/i2c-{bus}/new_device", "w") as f:
            f.write("led-ugreen 0x3a")
        result["steps"].append(f"  ✓ 已在 i2c-{bus} 创建 LED 设备节点")
    except OSError as e:
        result["steps"].append(f"  (设备节点可能已存在: {e})")


def _restart_led_services(result):
    """Restart all LED services; include probe-leds if the unit exists."""
    svcs = ["ugreen-led-init", "ugreen-power-led",
            "ugreen-diskiomon", "ugreen-netdevmon@enp2s0"]
    try:
        p = subprocess.run(["systemctl", "list-unit-files", "ugreen-probe-leds.service"],
                           capture_output=True, text=True, timeout=5)
        if "ugreen-probe-leds.service" in p.stdout:
            svcs.insert(1, "ugreen-probe-leds")
    except Exception:
        pass
    for svc in svcs:
        r = subprocess.run(["systemctl", "restart", svc], capture_output=True, timeout=10)
        result["steps"].append(f"  重启 {svc}: {'OK' if r.returncode == 0 else 'FAIL'}")


# ─── Disk serial detection ───
def get_disk_serials():
    """Scan SATA disk devices and return serial numbers with physical slot mapping."""
    import re
    results = []

    try:
        block_devs = sorted(os.listdir("/sys/block"))
    except OSError:
        return results

    for dev in block_devs:
        if not dev.startswith("sd"):
            continue

        real = os.path.realpath(f"/sys/block/{dev}")
        if "/ata" not in real:
            continue

        # Physical slot from ata{N}
        m = re.search(r'/ata(\d+)/', real)
        slot = int(m.group(1)) if m else 0

        # Serial via udevadm
        serial = ""
        model = ""
        try:
            r = subprocess.run(
                ["udevadm", "info", "--query=property", "--name", f"/dev/{dev}"],
                capture_output=True, text=True, timeout=5
            )
            props = dict(
                line.split("=", 1)
                for line in r.stdout.strip().split("\n")
                if "=" in line
            )
            serial = props.get("ID_SERIAL_SHORT", "")
            if not serial:
                full_id = props.get("ID_SERIAL", "")
                if full_id and "_" in full_id:
                    serial = full_id.split("_")[-1]
        except Exception:
            pass

        # Model from sysfs
        try:
            with open(f"/sys/block/{dev}/device/model") as f:
                model = f.read().strip()
        except OSError:
            pass

        # Size in GB
        size_gb = 0
        try:
            with open(f"/sys/block/{dev}/size") as f:
                size_gb = round(int(f.read().strip()) * 512 / (1000 ** 3))
        except (OSError, ValueError):
            pass

        results.append({
            "dev": dev, "serial": serial, "model": model,
            "slot": slot, "size_gb": size_gb,
        })

    results.sort(key=lambda x: x["slot"])
    return results


# ─── Service status ───
def get_service_statuses():
    """Query systemd for LED-related service statuses — batched into one call.

    Uses Key=Value parsing (without --value) so an empty SubState never
    shifts the line index and breaks the alignment."""
    units = [svc if "." in svc else svc + ".service" for svc, _ in LED_SERVICES]
    results = []
    try:
        r = subprocess.run(
            ["systemctl", "show", *units,
             "--property=Id,ActiveState,SubState", "--no-pager"],
            capture_output=True, text=True, timeout=10
        )
        statuses = {}
        current = {}
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line:
                # Empty line marks unit boundary
                if current.get("Id"):
                    statuses[current["Id"]] = (
                        current.get("ActiveState", "unknown"),
                        current.get("SubState", "unknown"),
                    )
                    current = {}
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                current[k] = v
        if current.get("Id"):
            statuses[current["Id"]] = (
                current.get("ActiveState", "unknown"),
                current.get("SubState", "unknown"),
            )
        for svc_name, label in LED_SERVICES:
            unit = svc_name if "." in svc_name else svc_name + ".service"
            active, sub = statuses.get(unit, ("unknown", "unknown"))
            results.append({
                "name": svc_name, "label": label,
                "status": active, "sub_state": sub,
            })
    except Exception:
        for svc_name, label in LED_SERVICES:
            results.append({
                "name": svc_name, "label": label,
                "status": "unknown", "sub_state": "unknown",
            })
    return results


# ─── Health check ───
def get_health_check():
    """Comprehensive health check for all system components.

    Reuses the already-batched /cached `services` and `dkms` lookups so we
    don't re-shell-out 6+ times per check."""
    issues = []
    ok_count = 0
    warn_count = 0

    # 1. LED devices check
    led_list = [n for n in LED_NAMES if os.path.isdir(f"{LED_BASE}/{n}")]
    if not led_list:
        issues.append({
            "severity": "error",
            "component": "LED 设备",
            "message": "未检测到任何 LED 设备",
            "fix": "点击下方 🔧 修复 按钮自动检测 I2C 总线"
        })
    else:
        ok_count += 1
        missing = [n for n in LED_NAMES if n not in led_list]
        if missing:
            issues.append({
                "severity": "warning",
                "component": "LED 设备",
                "message": f"部分 LED 不可用: {', '.join(missing)}"
            })

    # 2. Service health — reuse the batched cache (one systemctl show call)
    services = cached("services", get_service_statuses)
    kernel = os.uname().release
    for svc in services:
        st = svc["status"]
        if st == "active":
            ok_count += 1
        elif st == "failed":
            issues.append({
                "severity": "error",
                "component": svc["label"],
                "message": f"服务 {svc['name']} 状态: failed",
                "fix": f"systemctl restart {svc['name']}.service"
            })
        elif st == "inactive":
            issues.append({
                "severity": "warning",
                "component": svc["label"],
                "message": f"服务 {svc['name']} 已停止",
                "fix": f"systemctl start {svc['name']}.service"
            })
        else:
            issues.append({
                "severity": "warning",
                "component": svc["label"],
                "message": f"服务 {svc['name']} 状态: {st}"
            })

    # 3. DKMS module check — reuse the cached dkms info
    dkms = cached("dkms", get_dkms_info)
    if dkms.get("module_loaded"):
        ok_count += 1
    else:
        issues.append({
            "severity": "error",
            "component": "内核模块",
            "message": f"led-ugreen 模块未加载 (内核 {kernel})",
            "fix": "点击 🔧 修复 自动重新构建并加载"
        })

    # 4. I2C bus check — only scan when LEDs are missing (otherwise skip)
    if not led_list:
        try:
            i2c_devs = sorted(os.listdir("/sys/bus/i2c/devices"))
            found = False
            for dev in i2c_devs:
                if not dev.startswith("i2c-"):
                    continue
                bus = dev.replace("i2c-", "")
                try:
                    name = open(f"/sys/bus/i2c/devices/{dev}/name").read().strip()
                except Exception:
                    name = ""
                # Skip GPU/SoC buses — never the LED controller
                low = name.lower()
                if any(k in low for k in ("gmbus", "drm", "designware", "synopsys")):
                    continue
                ic = _get_i2cdetect()
                if not ic:
                    break
                r = subprocess.run(
                    [ic, "-y", "-r", bus, "0x3a", "0x3a"],
                    capture_output=True, text=True, timeout=5
                )
                if "3a" in r.stdout:
                    found = True
                    issues.append({
                        "severity": "error",
                        "component": "I2C 总线",
                        "message": f"LED 控制器在 i2c-{bus} 但未初始化",
                        "fix": "点击 🔧 修复 自动配置"
                    })
                    break
            if not found:
                issues.append({
                    "severity": "error",
                    "component": "I2C 总线",
                    "message": "未找到 LED 控制器 (0x3a)"
                })
        except Exception as e:
            issues.append({
                "severity": "warning",
                "component": "I2C 扫描",
                "message": f"I2C 扫描异常: {e}"
            })

    # 5. Disk detection
    try:
        r = subprocess.run(["lsblk", "-S", "-o", "name,tran", "-n"],
                           capture_output=True, text=True, timeout=5)
        sata_disks = [l.split()[0] for l in r.stdout.strip().split("\n") if "sata" in l]
        if not sata_disks:
            issues.append({
                "severity": "warning",
                "component": "磁盘检测",
                "message": "未检测到 SATA 磁盘"
            })
        else:
            ok_count += 1
    except Exception:
        pass

    all_ok = len([i for i in issues if i["severity"] == "error"]) == 0
    return {
        "ok": all_ok,
        "ok_count": ok_count,
        "warn_count": warn_count,
        "issues": issues,
        "kernel": kernel,
        "led_devices": led_list,
        "dkms_ok": dkms.get("module_loaded", False),
    }


def restart_service(svc_name):
    """Restart a single systemd service, return (ok, message)."""
    unit = svc_name if "." in svc_name else svc_name + ".service"
    try:
        r = subprocess.run(
            ["systemctl", "restart", unit],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode == 0:
            return True, f"{svc_name} 已重启"
        return False, r.stderr.strip() or f"重启 {svc_name} 失败 (exit {r.returncode})"
    except Exception as e:
        return False, str(e)


# ─── Config file ───
def get_leds_config():
    """Read /etc/ugreen-leds.conf."""
    try:
        with open(LEDS_CONF_PATH) as f:
            return f.read()
    except OSError as e:
        return ""


def save_leds_config(content):
    """Write /etc/ugreen-leds.conf atomically: .tmp then os.replace()."""
    try:
        backup = LEDS_CONF_PATH + ".bak"
        if os.path.exists(LEDS_CONF_PATH):
            with open(LEDS_CONF_PATH) as src, open(backup, "w") as dst:
                dst.write(src.read())
        tmp = LEDS_CONF_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())  # ensure data is on disk before swap
        os.replace(tmp, LEDS_CONF_PATH)  # atomic on POSIX
        return True
    except OSError:
        # Best-effort cleanup of leftover .tmp
        try:
            os.remove(LEDS_CONF_PATH + ".tmp")
        except OSError:
            pass
        return False


# ─── DKMS rebuild ───
def get_dkms_info():
    kernel = subprocess.run(["uname", "-r"], capture_output=True, text=True).stdout.strip()
    try:
        r = subprocess.run(["dkms", "status"], capture_output=True, text=True, timeout=10)
        dkms_status = r.stdout.strip()
    except Exception:
        dkms_status = "dkms not available"
    # Check if module is loaded
    lsmod = subprocess.run(["lsmod"], capture_output=True, text=True).stdout
    module_loaded = "led_ugreen" in lsmod
    # Check kernel headers
    headers_ok = os.path.isfile(f"/lib/modules/{kernel}/build/Makefile")
    return {
        "kernel": kernel,
        "module": f"{DKMS_PKG_NAME}/{DKMS_PKG_VERSION}",
        "dkms_status": dkms_status,
        "module_loaded": module_loaded,
        "headers_installed": headers_ok,
    }


def dkms_rebuild():
    """Full DKMS rebuild + reload sequence. Returns (success, output_log)."""
    import os as _os
    kernel = subprocess.run(["uname", "-r"], capture_output=True, text=True).stdout.strip()

    # Pre-check: kernel headers
    headers_path = f"/lib/modules/{kernel}/build"
    if not _os.path.isfile(f"{headers_path}/Makefile"):
        return False, (
            f"错误: 内核头文件未安装 ({kernel})\n"
            f"路径 {headers_path} 不存在\n\n"
            f"请先安装:\n"
            f"  apt install linux-headers-{kernel}\n"
        )

    # Detect the I2C bus hosting the LED controller (survives bus renumbering)
    bus = detect_led_bus() or "0"

    # (description, command, critical)
    # critical=True means failure aborts the whole sequence
    steps = [
        ("停止 LED 监控服务",
         f"systemctl stop ugreen-diskiomon {NETDEV_SERVICE} ugreen-power-led 2>&1",
         False),
        ("卸载 I2C 设备",
         f"sh -c 'echo 0x3a > /sys/bus/i2c/devices/i2c-{bus}/delete_device 2>/dev/null; true'",
         False),
        ("卸载内核模块",
         "sh -c 'rmmod led-ugreen 2>/dev/null; true'",
         False),
        ("DKMS 移除旧模块",
         f"dkms remove {DKMS_PKG_NAME}/{DKMS_PKG_VERSION} --all 2>&1 || true",
         False),
        ("DKMS 注册源码",
         f"dkms add {DKMS_PKG_NAME}/{DKMS_PKG_VERSION} 2>&1 || true",
         False),
        (f"DKMS 编译 ({kernel})",
         f"dkms build {DKMS_PKG_NAME}/{DKMS_PKG_VERSION} -k {kernel} 2>&1",
         True),
        ("DKMS 安装",
         f"dkms install {DKMS_PKG_NAME}/{DKMS_PKG_VERSION} -k {kernel} --force 2>&1",
         True),
        ("加载内核模块",
         "modprobe led-ugreen 2>&1",
         True),
        ("创建 I2C 设备",
         f"sh -c 'sleep 0.5 && echo led-ugreen 0x3a > /sys/bus/i2c/devices/i2c-{bus}/new_device 2>&1'",
         True),
        ("重启 LED 服务",
         f"systemctl restart ugreen-led-init ugreen-probe-leds ugreen-power-led ugreen-diskiomon {NETDEV_SERVICE} 2>&1",
         False),
    ]
    log_lines = []
    success = True
    for desc, cmd, critical in steps:
        log_lines.append(f">>> {desc}")
        log_lines.append(f"    $ {cmd}")
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
            out = (r.stdout + r.stderr).strip()
            if out:
                log_lines.append(out)
            if r.returncode != 0:
                # Commands with || true or ;true always return 0, so non-zero = real failure
                if critical:
                    log_lines.append(f"    [FAILED — exit {r.returncode}]")
                    success = False
                    break  # stop on critical failure
                else:
                    log_lines.append(f"    [exit {r.returncode}, non-critical, continuing]")
        except subprocess.TimeoutExpired:
            log_lines.append("    [TIMEOUT — 命令超时 (>120s)]")
            if critical:
                success = False
                break
        except Exception as e:
            log_lines.append(f"    [ERROR] {e}")
            if critical:
                success = False
                break
        log_lines.append("")

    # Final status check
    log_lines.append("")
    try:
        r = subprocess.run(["dkms", "status"], capture_output=True, text=True, timeout=10)
        log_lines.append("=== 最终 DKMS 状态 ===")
        log_lines.append(r.stdout.strip() or "(empty)")
    except Exception:
        log_lines.append("=== 无法获取 DKMS 状态 ===")

    try:
        lsmod = subprocess.run(["lsmod"], capture_output=True, text=True).stdout
        loaded = "led_ugreen" in lsmod
        log_lines.append(f"\n模块已加载: {'是' if loaded else '否'}")
    except Exception:
        pass

    return success, "\n".join(log_lines)


def install_kernel_headers():
    """Install kernel headers for the running kernel. Returns (success, output)."""
    kernel = subprocess.run(["uname", "-r"], capture_output=True, text=True).stdout.strip()
    headers_path = f"/lib/modules/{kernel}/build/Makefile"
    if os.path.isfile(headers_path):
        return True, f"内核头文件已安装 ({kernel})，无需重复安装。"

    cmd = f"apt-get install -y linux-headers-{kernel} 2>&1"
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
        out = r.stdout.strip()
        if r.stderr.strip():
            out += "\n" + r.stderr.strip()
        ok = r.returncode == 0
        # Verify installation
        if ok and os.path.isfile(headers_path):
            out += f"\n\n内核头文件 {kernel} 安装成功。"
        elif ok:
            out += f"\n\n警告: apt 返回成功但头文件路径不存在，请检查包名是否正确。"
            ok = False
        return ok, out
    except subprocess.TimeoutExpired:
        return False, "安装超时 (>300s)，请通过 SSH 手动执行。"
    except Exception as e:
        return False, str(e)


# ─── HTTP Handler ───
class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # suppress default logging

    def _gzip_ok(self):
        return "gzip" in self.headers.get("Accept-Encoding", "").lower()

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        if self._gzip_ok() and len(body) > 512:
            body = gzip.compress(body)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

    def do_GET(self):
        try:
            self._do_GET()
        except Exception as e:
            self.send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/":
            self.send_html()
        elif path == "/api/temps":
            self.send_json(cached("temps", get_temps))
        elif path == "/api/fans":
            self.send_json({
                "mode": fan_mode,
                "manual_count": fan_manual_count,
                "fans": cached("fans", get_fan_status),
                "cpu_temp": get_cpu_package_temp(),
            })
        elif path == "/api/leds":
            self.send_json(cached("leds", get_led_status))
        elif path == "/api/health":
            self.send_json(cached("health", get_health_check))

        elif path == "/api/services":
            self.send_json(cached("services", get_service_statuses))
        elif path == "/api/config/leds":
            self.send_json({"content": get_leds_config()})
        elif path == "/api/dkms":
            self.send_json(cached("dkms", get_dkms_info))
        elif path == "/api/disk-serials":
            self.send_json(cached("disk_serials", get_disk_serials))
        elif path == "/api/realtime":
            # temps + fans + LEDs in one shot so the 3s refresh keeps the
            # LED card in sync with state changes
            self.send_json({
                "temps": cached("temps", get_temps),
                "fans": {
                    "mode": fan_mode,
                    "manual_count": fan_manual_count,
                    "fans": cached("fans", get_fan_status),
                },
                "leds": cached("leds", get_led_status),
            })
        elif path == "/api/status":
            self.send_json({
                "leds": cached("leds", get_led_status),
                "services": cached("services", get_service_statuses),
                "dkms": cached("dkms", get_dkms_info),
                "health": cached("health", get_health_check),
            })
        elif path == "/api/all":
            self.send_json({
                "temps": cached("temps", get_temps),
                "fans": {
                    "mode": fan_mode,
                    "manual_count": fan_manual_count,
                    "fans": cached("fans", get_fan_status),
                },
                "leds": cached("leds", get_led_status),
                "services": cached("services", get_service_statuses),
                "health": cached("health", get_health_check),
                "dkms": cached("dkms", get_dkms_info),
            })
        else:
            self.send_error(404)

    def do_POST(self):
        try:
            self._do_POST()
        except Exception as e:
            self.send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _do_POST(self):
        global fan_mode, fan_manual_count
        parsed = urlparse(self.path)
        path = parsed.path

        # Cap body size to prevent a malicious client from exhausting memory
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        if length < 0 or length > MAX_POST_BODY:
            self.send_json({"ok": False, "error": "body too large"}, 413)
            return
        body = json.loads(self.rfile.read(length)) if length else {}

        if path == "/api/fan/mode":
            mode = body.get("mode", "auto")
            if mode == "auto":
                fan_mode = "auto"
                start_auto()
                self.send_json({"ok": True, "mode": "auto"})
            elif mode == "manual":
                fan_mode = "manual"
                stop_auto()
                count = max(0, min(len(FAN_CDEVS), int(body.get("count", 0))))
                fan_manual_count = set_fans(count)
                self.send_json({"ok": True, "mode": "manual", "count": fan_manual_count})
            else:
                self.send_json({"ok": False, "error": "invalid mode"}, 400)

        elif path == "/api/fan/set":
            count = max(0, min(len(FAN_CDEVS), int(body.get("count", 0))))
            if fan_mode == "manual":
                fan_manual_count = set_fans(count)
            else:
                set_fans(count)
            self.send_json({"ok": True, "count": count})

        elif path == "/api/led/set":
            name = body.get("name")
            if not name:
                self.send_json({"ok": False, "error": "name required"}, 400)
                return
            kwargs = {}
            if "brightness" in body:
                kwargs["brightness"] = int(body["brightness"])
            if "color" in body:
                c = body["color"]
                kwargs["color"] = (int(c.get("r", 255)), int(c.get("g", 255)), int(c.get("b", 255)))
            if "blink_type" in body:
                kwargs["blink_type"] = body["blink_type"]
            ok = set_led(name, **kwargs)
            self.send_json({"ok": ok})

        elif path == "/api/led/all":
            color = body.get("color")
            brightness = body.get("brightness")
            for name in LED_NAMES:
                kwargs = {}
                if color:
                    kwargs["color"] = (int(color.get("r", 255)), int(color.get("g", 255)), int(color.get("b", 255)))
                if brightness is not None:
                    kwargs["brightness"] = int(brightness)
                if kwargs:
                    set_led(name, **kwargs)
            self.send_json({"ok": True})

        elif path == "/api/config/leds":
            content = body.get("content", "")
            ok = save_leds_config(content)
            self.send_json({"ok": ok})

        elif path == "/api/config/sync-leds":
            ok, msg = sync_all_leds_to_config()
            self.send_json({"ok": ok, "message": msg})

        elif path == "/api/service/restart":
            svc = body.get("name", "")
            if not svc:
                self.send_json({"ok": False, "error": "name required"}, 400)
                return
            ok, msg = restart_service(svc)
            self.send_json({"ok": ok, "message": msg})

        elif path == "/api/led/repair":
            result = led_repair()
            self.send_json(result)

        elif path == "/api/dkms/rebuild":
            success, output = dkms_rebuild()
            self.send_json({"ok": success, "output": output})

        elif path == "/api/dkms/headers":
            ok, output = install_kernel_headers()
            self.send_json({"ok": ok, "output": output})

        else:
            self.send_error(404)

    def send_html(self):
        html = INDEX_HTML.encode()
        if self._gzip_ok():
            html = gzip.compress(html)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", len(html))
            self.send_header("Cache-Control", "public, max-age=60")
            self.end_headers()
            self.wfile.write(html)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(html))
            self.send_header("Cache-Control", "public, max-age=60")
            self.end_headers()
            self.wfile.write(html)


# ─── HTML Page ───
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DX4600 Pro 硬件监控</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='6' fill='%230d1117'/><path d='M8 12c0-2.2 1.8-4 4-4h8c2.2 0 4 1.8 4 4v2c0 1.1-.4 2.1-1.2 2.8L21 18.5V22c0 1.1-.9 2-2 2h-1c-1.1 0-2-.9-2-2v-1h-1v1c0 1.1-.9 2-2 2h-1c-1.1 0-2-.9-2-2v-3.5l-1.8-1.7A3.9 3.9 0 0 1 8 14v-2z' fill='%23161b22' stroke='%2330363d' stroke-width='.7'/><rect x='11' y='11.5' width='4' height='1.2' rx='.6' fill='%2358a6ff'/><rect x='12.4' y='10' width='1.2' height='4' rx='.6' fill='%2358a6ff'/><circle cx='20' cy='10.5' r='1' fill='%233fb950'/><circle cx='22' cy='12.5' r='1' fill='%23f85149'/><circle cx='18' cy='12.5' r='1' fill='%23d29922'/><circle cx='20' cy='14.5' r='1' fill='%2358a6ff'/></svg>">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--text2:#8b949e;--accent:#58a6ff;--green:#3fb950;--orange:#d29922;--red:#f85149;--blue:#58a6ff}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:linear-gradient(rgba(13,17,23,0.82),rgba(13,17,23,0.88)),radial-gradient(ellipse at 30% 20%,#1a2540 0%,transparent 50%),radial-gradient(ellipse at 70% 80%,#1a2040 0%,transparent 50%),url(http://192.168.31.251:9999/random.php) center/cover no-repeat fixed;color:var(--text);min-height:100vh;padding:16px;padding-bottom:48px}
.container{max-width:960px;margin:0 auto}
.app-layout{display:flex;gap:20px;max-width:1200px;margin:0 auto;padding:16px 0;align-items:flex-start}
.sidebar{width:220px;flex-shrink:0;display:flex;flex-direction:column;gap:16px;padding-top:74px}
.main{flex:1;min-width:0}
/* Clock */
.clock{width:200px;height:200px;background:rgba(22,27,34,0.5);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border-radius:50%;border:1px solid rgba(255,255,255,0.06);position:relative;overflow:hidden;margin:0 auto 16px;box-shadow:0 8px 24px rgba(0,0,0,0.3),inset 0 1px 0 rgba(255,255,255,0.04)}
.clock .numbers{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:100%;height:100%;border-radius:50%;background:transparent}
.clock .number{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);display:inline-block;width:1px;height:10px;background:rgba(255,255,255,0.15)}
.clock .number.big{width:2px;height:18px;background:rgba(255,255,255,0.3)}
.clock .needles{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%)}
.clock .h,.clock .m,.clock .s,.clock .center{position:absolute;top:50%;left:50%}
.clock .h{width:5px;height:40px;background:#e0e0e0;border-radius:3px 3px 0 0;z-index:1;transform:translate(-50%,-100%) rotateZ(0deg);transform-origin:50% 100%;transition:transform .5s cubic-bezier(.4,2,.6,1)}
.clock .m{width:3px;height:60px;background:rgba(255,255,255,0.55);border-radius:2px 2px 0 0;z-index:2;transform:translate(-50%,-100%) rotateZ(0deg);transform-origin:50% 100%;transition:transform .3s cubic-bezier(.4,2,.6,1)}
.clock .s{width:1.5px;height:72px;background:#ff3b30;border-radius:1px 1px 0 0;z-index:3;transform:translate(-50%,-80%) rotateZ(0deg);transform-origin:50% 80%}
.clock .center{width:12px;height:12px;background:#fff;border:3px solid #ff3b30;z-index:4;border-radius:50%;transform:translate(-50%,-50%)}
/* Weather card */
.weather-card{display:flex;flex-direction:column;align-items:center;position:relative;width:220px;height:340px;border-radius:20px;overflow:hidden;box-shadow:0 12px 24px rgba(0,0,0,0.3);margin:0 auto}
.weather-card .landscape-section{position:relative;width:100%;height:65%;overflow:hidden}
.weather-card .landscape-section *:not(.weather-info,.weather-info *){position:absolute}
.weather-card .sky{width:100%;height:100%;background:linear-gradient(0deg,rgba(247,225,87,1) 0%,rgba(233,101,148,1) 100%)}
.weather-card .sun{display:flex;align-items:center;justify-content:center;width:45px;height:45px;border-radius:50%;background:#fff;bottom:40%;left:23%;filter:drop-shadow(0 0 10px #fff)}
.weather-card .sun::after{position:absolute;content:"";width:118%;height:118%;border-radius:50%;background:#fff;opacity:.5}
.weather-card .sun::before{position:absolute;content:"";width:134%;height:134%;border-radius:50%;background:#fff;opacity:.1}
.weather-card .ocean{overflow:hidden;bottom:0;width:100%;height:28%;background:linear-gradient(0deg,rgba(241,192,125,1) 0%,rgba(247,218,150,1) 100%)}
.weather-card .reflection{position:absolute;background:#fff;opacity:.5;z-index:1}
.weather-card .reflection:nth-child(1){width:40px;height:10px;clip-path:polygon(0% 0%,100% 0%,50% 20%);top:5%;left:32%}
.weather-card .reflection:nth-child(2){width:80px;height:15px;clip-path:polygon(0% 0%,100% 0%,60% 20%,40% 20%);top:15%;left:39%}
.weather-card .reflection:nth-child(3){width:60px;height:2px;clip-path:polygon(0% 50%,40% 0%,60% 0%,100% 50%,60% 100%,40% 100%);top:27%;right:15%}
.weather-card .reflection:nth-child(4){width:70px;height:2px;clip-path:polygon(0% 50%,40% 0%,60% 0%,100% 50%,60% 100%,40% 100%);top:37%;right:28%}
.weather-card .reflection:nth-child(5){width:70px;height:3px;clip-path:polygon(0% 50%,40% 0%,60% 0%,100% 50%,60% 100%,40% 100%);top:46%;right:8%}
.weather-card .hill-1{right:-25%;bottom:20%;width:150px;height:40px;border-radius:50%;background:#e6b29d}
.weather-card .shadow-hill-1{right:-25%;top:-30%;width:150px;height:40px;border-radius:50%;background:#f1c7a0}
.weather-card .hill-2{right:-36%;bottom:10%;width:150px;height:80px;border-radius:50%;background:#c29182}
.weather-card .shadow-hill-2{right:-36%;top:-65%;width:150px;height:80px;border-radius:50%;background:#e5bb96}
.weather-card .hill-3{left:-100%;bottom:-28%;width:350px;height:150px;border-radius:50%;background:#b77873;z-index:3}
.weather-card .hill-4{right:-100%;bottom:-40%;width:350px;height:150px;border-radius:50%;background:#a16773;z-index:3}
.weather-card .tree-1{bottom:20%;left:3%;width:50px;height:70px;z-index:3}
.weather-card .tree-2{bottom:14%;left:25%;width:50px;height:70px;z-index:3}
.weather-card .tree-3{bottom:10%;right:1%;width:65px;height:80px;z-index:3}
.weather-card .filter-overlay{position:absolute;top:0;left:0;height:100%;width:100%;background:linear-gradient(0deg,rgba(255,255,255,1) 0%,rgba(255,255,255,0) 40%);z-index:5;opacity:.2}
.weather-card .content-section{width:100%;height:35%;display:flex;flex-direction:column;align-items:center;background:#1a1a2e}
.weather-card .weather-info{position:absolute;top:0;right:0;display:flex;align-items:center;justify-content:space-around;width:100%;padding:10px 14px;color:#fff;z-index:10;text-align:center}
.weather-card .weather-info .left-side{width:30%;font-size:11pt;font-weight:600;align-self:baseline}
.weather-card .weather-info .left-side p{margin:2px 0}
.weather-card .weather-icon{font-size:28px;line-height:1}
.weather-card .weather-icon svg{width:32px;height:32px;fill:#fff}
.weather-card .weather-info .right-side{display:flex;flex-direction:column;align-items:flex-end;gap:2px}
.weather-card .weather-info .right-side .city-name{font-size:11pt;font-weight:700}
.weather-card .weather-info .right-side .date-text{font-size:9pt;opacity:.8}
.weather-card .weather-info .right-side .w-temp{font-size:20pt;font-weight:700;line-height:1}
.weather-card .forecast{display:flex;flex-direction:column;align-items:center;justify-content:space-evenly;height:100%;width:100%;padding:6px 20px;color:#aab}
.weather-card .forecast>div{width:100%;display:flex;align-items:center;justify-content:space-between;font-size:9pt}
.weather-card .forecast-sep{width:100%;height:1px;background:rgba(255,255,255,0.08);border-radius:1px}
/* Location modal */
.loc-modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.7);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);display:none;align-items:center;justify-content:center;z-index:1000}
.loc-modal-overlay.show{display:flex}
.loc-modal{background:linear-gradient(135deg,rgba(30,38,50,0.95),rgba(20,26,35,0.92));backdrop-filter:blur(16px);border:1px solid rgba(255,255,255,0.08);border-radius:16px;padding:24px;width:320px;box-shadow:0 20px 40px rgba(0,0,0,0.5)}
.loc-modal h3{font-size:1em;color:var(--text);margin-bottom:16px;font-weight:500}
.loc-modal input{width:100%;padding:10px 14px;background:rgba(0,0,0,0.3);border:1px solid rgba(255,255,255,0.1);border-radius:10px;color:var(--text);font-size:.9em;outline:none;margin-bottom:14px}
.loc-modal input:focus{border-color:var(--accent)}
.loc-modal .loc-btns{display:flex;gap:8px;justify-content:flex-end}
.loc-modal .loc-hint{font-size:.75em;color:var(--text2);margin-bottom:14px}
/* Intro 3D card */
.intro-3d{width:220px;height:280px;perspective:1000px;flex-shrink:0}
.intro-3d .card3d{height:100%;border-radius:40px;background:linear-gradient(135deg,rgb(0,255,214) 0%,rgb(8,226,96) 100%);transition:all .5s ease-in-out;transform-style:preserve-3d;box-shadow:rgba(5,71,17,0) 40px 50px 25px -40px,rgba(5,71,17,.2) 0 25px 25px -5px}
.intro-3d:hover .card3d{transform:rotate3d(1,1,0,30deg);box-shadow:rgba(5,71,17,.3) 30px 50px 25px -40px,rgba(5,71,17,.1) 0 25px 30px 0}
.intro-3d .glass3d{transform-style:preserve-3d;position:absolute;inset:8px;border-radius:45px;border-top-right-radius:100%;background:linear-gradient(0deg,rgba(255,255,255,.35) 0%,rgba(255,255,255,.82) 100%);transform:translate3d(0,0,25px);border-left:1px solid #fff;border-bottom:1px solid #fff;transition:all .5s ease-in-out}
.intro-3d .content3d{padding:80px 30px 0 30px;transform:translate3d(0,0,26px)}
.intro-3d .content3d .title3d{display:block;color:#00894d;font-weight:900;font-size:18px}
.intro-3d .content3d .text3d{display:block;color:rgba(0,137,78,.76);font-size:12px;margin-top:8px;line-height:1.5}
.intro-3d .logo3d{position:absolute;right:0;top:0;transform-style:preserve-3d}
.intro-3d .logo3d .c3d{display:block;position:absolute;aspect-ratio:1;border-radius:50%;top:0;right:0;box-shadow:rgba(100,100,111,.2) -10px 10px 20px 0;-webkit-backdrop-filter:blur(5px);backdrop-filter:blur(5px);background:rgba(0,249,203,.2);transition:all .5s ease-in-out}
.intro-3d .logo3d .c3d1{width:160px;transform:translate3d(0,0,20px);top:6px;right:6px}
.intro-3d .logo3d .c3d2{width:130px;transform:translate3d(0,0,40px);top:8px;right:8px;transition-delay:.4s}
.intro-3d .logo3d .c3d3{width:100px;transform:translate3d(0,0,60px);top:14px;right:14px;transition-delay:.8s}
.intro-3d .logo3d .c3d4{width:70px;transform:translate3d(0,0,80px);top:18px;right:18px;transition-delay:1.2s}
.intro-3d .logo3d .c3d5{width:44px;transform:translate3d(0,0,100px);top:24px;right:24px;display:grid;place-content:center;transition-delay:1.6s}
.intro-3d .logo3d .c3d5 svg{width:18px;fill:#fff}
.intro-3d:hover .logo3d .c3d2{transform:translate3d(0,0,60px)}
.intro-3d:hover .logo3d .c3d3{transform:translate3d(0,0,80px)}
.intro-3d:hover .logo3d .c3d4{transform:translate3d(0,0,100px)}
.intro-3d:hover .logo3d .c3d5{transform:translate3d(0,0,120px)}
.intro-3d .bottom3d{padding:10px 12px;transform-style:preserve-3d;position:absolute;bottom:14px;left:14px;right:14px;display:flex;align-items:center;justify-content:space-between;transform:translate3d(0,0,26px)}
.intro-3d .bottom3d .specs3d{display:flex;gap:8px}
.intro-3d .bottom3d .chip3d{background:#fff;padding:3px 8px;border-radius:10px;font-size:9px;color:#00894d;font-weight:600;box-shadow:rgba(5,71,17,.5) 0 7px 5px -5px}
.intro-3d .bottom3d .link3d{background:none;border:none;color:#00c37b;font-weight:700;font-size:11px;cursor:default}
.intro-3d:hover .bottom3d .chip3d{transform:translate3d(0,0,50px);box-shadow:rgba(5,71,17,.2) -5px 20px 10px 0}
/* Title button */
.title-btn{display:flex;flex-direction:column;justify-content:center;align-items:center;width:16rem;overflow:hidden;height:3.6rem;background-size:300% 300%;backdrop-filter:blur(1rem);border-radius:5rem;transition:.5s;animation:gradient_301 5s ease infinite;border:double 4px transparent;background-image:linear-gradient(#161b22,#161b22),linear-gradient(137.48deg,#ffdb3b 10%,#fe53bb 45%,#8f51ea 67%,#0044ff 87%);background-origin:border-box;background-clip:content-box,border-box;position:relative;padding:0}
.title-btn strong{z-index:2;font-size:14px;letter-spacing:5px;color:#fff;text-shadow:0 0 4px #fff;font-weight:700}
.title-btn .title-sub{z-index:2;font-size:10px;color:rgba(255,255,255,.6);letter-spacing:2px;margin-top:2px}
#container-stars{position:absolute;z-index:-1;width:100%;height:100%;overflow:hidden;transition:.5s;backdrop-filter:blur(1rem);border-radius:5rem}
#glow{position:absolute;display:flex;width:12rem}
#glow .circle{width:100%;height:30px;filter:blur(2rem);animation:pulse_3011 4s infinite;z-index:-1}
#glow .circle:nth-of-type(1){background:rgba(254,83,186,.64)}
#glow .circle:nth-of-type(2){background:rgba(142,81,234,.7)}
.title-btn:hover #container-stars{z-index:1;background-color:#161b22}
.title-btn:hover{transform:scale(1.05)}
.title-btn:active{border:double 4px #fe53bb;background-origin:border-box;background-clip:content-box,border-box;animation:none}
.title-btn:active .circle{background:#fe53bb}
#stars{position:relative;background:transparent;width:200rem;height:200rem}
#stars::after{content:"";position:absolute;top:-10rem;left:-100rem;width:100%;height:100%;animation:animStarRotate 90s linear infinite;background-image:radial-gradient(#fff 1px,transparent 1%);background-size:50px 50px}
#stars::before{content:"";position:absolute;top:0;left:-50%;width:170%;height:500%;animation:animStar 60s linear infinite;background-image:radial-gradient(#fff 1px,transparent 1%);background-size:50px 50px;opacity:.5}
@keyframes animStar{from{transform:translateY(0)}to{transform:translateY(-135rem)}}
@keyframes animStarRotate{from{transform:rotate(360deg)}to{transform:rotate(0)}}
@keyframes gradient_301{0%{background-position:0% 50%}50%{background-position:100% 50%}100%{background-position:0% 50%}}
@keyframes pulse_3011{0%{transform:scale(.75);box-shadow:0 0 0 0 rgba(0,0,0,.7)}70%{transform:scale(1);box-shadow:0 0 0 10px rgba(0,0,0,0)}100%{transform:scale(.75);box-shadow:0 0 0 0 rgba(0,0,0,0)}}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px}
.card h2{font-size:.9em;color:var(--text2);margin-bottom:12px;font-weight:500;text-transform:uppercase;letter-spacing:.5px}
.temp-sliders{display:flex;flex-direction:column;gap:8px}
.temp-row{display:flex;align-items:center;gap:12px}
.temp-row label{font-size:12px;color:var(--text2);width:72px;flex-shrink:0}
.temp-content{display:flex;align-items:center;gap:10px;flex:1;background:rgba(22,27,34,0.5);height:35px;padding-inline:10px;border-radius:6px}
.temp-bar-wrap{flex:1;height:8px;display:flex;align-items:center;border-radius:4px;overflow:hidden;background:#1a1a1a}
.temp-bar-fill{height:100%;border-radius:4px;transition:width .5s,background .5s}
.temp-divider{height:50%;width:1px;background:rgba(255,255,255,0.08)}
.temp-val{font-weight:600;font-size:.85em;min-width:56px;text-align:right;font-variant-numeric:tabular-nums}
.fan-visual{display:flex;gap:12px;justify-content:center;margin:12px 0}
.fan-icon{width:52px;height:52px;border-radius:50%;border:2px solid rgba(255,255,255,0.08);display:flex;align-items:center;justify-content:center;transition:all .3s}
.fan-icon svg{width:28px;height:28px}
.fan-icon.on{border-color:var(--green);color:var(--green);animation:spin .8s linear infinite}
.fan-icon.off{color:var(--text2);opacity:.5}
@keyframes spin{to{transform:rotate(360deg)}}
.mode-btns{display:flex;gap:8px;margin-bottom:12px}
.btn{padding:8px 16px;border:1px solid rgba(255,255,255,0.1);border-radius:8px;background:rgba(255,255,255,0.05);color:var(--text2);cursor:pointer;font-size:.85em;transition:all .2s}
.btn:hover{border-color:var(--accent);color:var(--text)}
.btn.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn-sm{padding:4px 10px;font-size:.75em}
.btn-danger{border-color:var(--red);color:var(--red)}
.btn-danger:hover{background:var(--red);color:#fff}
.btn-green{border-color:var(--green);color:var(--green)}
.btn-green:hover{background:var(--green);color:#fff}
.slider-row{display:flex;align-items:center;gap:12px;margin:8px 0}
.slider-row label{min-width:60px;font-size:.85em;color:var(--text2)}
input[type=range]{flex:1;-webkit-appearance:none;height:6px;border-radius:3px;background:#21262d;outline:none}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:18px;height:18px;border-radius:50%;background:var(--accent);cursor:pointer}
/* Glass card container */
.glass-card{
  background:linear-gradient(135deg,rgba(28,36,48,0.94) 0%,rgba(18,24,34,0.9) 100%);
  backdrop-filter:blur(16px);
  -webkit-backdrop-filter:blur(16px);
  border-radius:14px;padding:16px;
  border:1px solid rgba(255,255,255,0.1);
  box-shadow:0 8px 24px rgba(0,0,0,0.4),inset 0 1px 0 rgba(255,255,255,0.1)
}
.glass-card-header{display:flex;align-items:center;gap:8px}
.glass-card-title{font-size:.9em;font-weight:500;color:var(--text);letter-spacing:.5px;text-transform:uppercase}
.glass-divider{border:none;border-top:1px solid rgba(255,255,255,0.06);margin:10px 0}
.glass-bottom-bar{display:flex;gap:8px;align-items:center;margin-top:10px}
.glass-btn{
  padding:6px 14px;border:1px solid rgba(255,255,255,0.1);
  border-radius:8px;background:rgba(255,255,255,0.05);
  color:var(--text2);cursor:pointer;font-size:.8em;transition:all .2s
}
.glass-btn:hover{background:rgba(88,166,255,0.12);border-color:var(--accent);color:var(--text)}
/* Glass LED grid items */
.led-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
.led-card{
  background:linear-gradient(135deg,rgba(30,38,50,0.78) 0%,rgba(18,23,31,0.72) 100%);
  backdrop-filter:blur(12px);
  -webkit-backdrop-filter:blur(12px);
  border-radius:12px;padding:14px;text-align:center;
  border:1px solid rgba(255,255,255,0.06);
  box-shadow:0 6px 18px rgba(0,0,0,0.3),inset 0 1px 0 rgba(255,255,255,0.06);
  transition:transform .3s,border-color .3s
}
.led-card:hover{border-color:rgba(88,166,255,0.3);transform:translateY(-2px)}
.led-dot{
  width:40px;height:40px;border-radius:50%;margin:0 auto 10px;
  border:2px solid rgba(255,255,255,0.1);transition:all .3s
}
.led-name{font-size:.8em;color:var(--text2);margin-bottom:8px;font-weight:500}
.led-controls{display:flex;gap:4px;justify-content:center;flex-wrap:wrap;align-items:center}
.led-btn{
  padding:4px 10px;border:1px solid rgba(255,255,255,0.1);
  border-radius:6px;background:rgba(255,255,255,0.05);
  color:var(--text2);cursor:pointer;font-size:.72em;transition:all .2s
}
.led-btn:hover{background:rgba(88,166,255,0.15);border-color:var(--accent);color:var(--text)}
input[type=color]{
  width:28px;height:24px;border:1px solid rgba(255,255,255,0.1);
  border-radius:4px;cursor:pointer;background:transparent;padding:0
}
/* Glass slider with blue fill */
.glass-slider{
  -webkit-appearance:none;width:100%;height:6px;
  background:transparent;border-radius:3px;outline:none
}
.glass-slider::-webkit-slider-thumb{
  -webkit-appearance:none;width:20px;height:20px;border-radius:50%;
  background:#fff;border:1px solid rgba(0,0,0,0.1);cursor:pointer;
  box-shadow:-407px 0 0 400px var(--accent);transition:transform .2s
}
.glass-slider:hover::-webkit-slider-thumb{transform:scale(1.2)}
.glass-slider::-moz-range-progress{background:var(--accent)}
.glass-slider::-moz-range-thumb{
  height:14px;width:14px;border-radius:50%;background:#fff;border:none
}
.slider-wrap{flex:1;height:22px;display:flex;align-items:center;border-radius:6px;overflow:hidden;background:#1a1a1a}
.status-bar{position:fixed;bottom:0;left:0;right:0;background:rgba(13,17,23,0.9);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border-top:1px solid rgba(255,255,255,0.06);padding:8px 16px;font-size:.75em;color:var(--text2);display:flex;justify-content:space-between;gap:12px}
#health-indicator{cursor:pointer;display:flex;align-items:center;gap:4px}
#health-indicator .dot{width:7px;height:7px;border-radius:50%;display:inline-block}
#health-indicator .dot.green{background:var(--green)}
#health-indicator .dot.orange{background:var(--orange);box-shadow:0 0 6px var(--orange)}
#health-indicator .dot.red{background:var(--red);box-shadow:0 0 6px var(--red);animation:blink 1s infinite}
@keyframes blink{50%{opacity:0.4}}
.curve-info{font-size:.75em;color:var(--text2);margin-top:10px;line-height:1.6}

/* Service status */
.svc-row{display:flex;align-items:center;padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.06);gap:10px}
.svc-row:last-child{border:none}
.svc-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.svc-dot.active{background:var(--green);box-shadow:0 0 6px var(--green)}
.svc-dot.inactive{background:#555}
.svc-dot.failed{background:var(--red);box-shadow:0 0 6px var(--red)}
.svc-info{flex:1;min-width:0}
.svc-name{font-size:.85em;color:var(--text)}
.svc-label{font-size:.75em;color:var(--text2)}
.svc-status{font-size:.75em;font-weight:500;min-width:48px;text-align:right}
.svc-status.active{color:var(--green)}
.svc-status.inactive{color:var(--text2)}
.svc-status.failed{color:var(--red)}

/* Terminal-style config editor */
.terminal-dots{display:flex;gap:6px}
.terminal-dot{width:10px;height:10px;border-radius:50%}
.terminal-dot.red{background:#ff5f56}
.terminal-dot.yellow{background:#ffbd2e}
.terminal-dot.green{background:#27c93f}
.terminal-body{padding:0}
.terminal-body .config-editor{
  width:100%;min-height:320px;background:#0d1117;border:none;border-top:1px solid rgba(255,255,255,0.04);
  color:#e0e0e0;font-family:'JetBrains Mono','Cascadia Code','Fira Code',monospace;
  font-size:.82em;line-height:1.6;padding:14px 16px;resize:vertical;tab-size:4;outline:none
}
.terminal-body .config-editor:focus{border-color:transparent}
.terminal-toolbar{display:flex;gap:8px;align-items:center;padding:10px 16px;border-top:1px solid rgba(255,255,255,0.04)}
.terminal-toolbar .config-msg{font-size:.8em;margin-left:8px;transition:opacity .3s}

/* DKMS */
.dkms-info{display:flex;gap:16px;flex-wrap:wrap;font-size:.85em;margin-bottom:12px}
.dkms-info span{color:var(--text2)}
.dkms-info strong{color:var(--text)}
.dkms-output{background:#0d1117;border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:12px;font-family:'JetBrains Mono','Cascadia Code',monospace;font-size:.75em;line-height:1.5;max-height:300px;overflow-y:auto;white-space:pre-wrap;color:var(--text2);display:none;margin-top:8px}

/* Loading overlay */
.loading-overlay{position:fixed;inset:0;background:rgba(13,17,23,0.85);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);display:flex;align-items:center;justify-content:center;z-index:999;transition:opacity .4s}
.loading-overlay.hidden{opacity:0;pointer-events:none}
.wheel-and-hamster{--dur:1s;position:relative;width:12em;height:12em;font-size:14px}
.wheel,.hamster,.hamster div,.spoke{position:absolute}
.wheel,.spoke{border-radius:50%;top:0;left:0;width:100%;height:100%}
.wheel{background:radial-gradient(100% 100% at center,hsla(0,0%,60%,0) 47.8%,hsl(0,0%,60%) 48%);z-index:2}
.hamster{animation:hamster var(--dur) ease-in-out infinite;top:50%;left:calc(50% - 3.5em);width:7em;height:3.75em;transform:rotate(4deg) translate(-0.8em,1.85em);transform-origin:50% 0;z-index:1}
.hamster__head{animation:hamsterHead var(--dur) ease-in-out infinite;background:hsl(30,90%,55%);border-radius:70% 30% 0 100% / 40% 25% 25% 60%;box-shadow:0 -0.25em 0 hsl(30,90%,80%) inset,0.75em -1.55em 0 hsl(30,90%,90%) inset;top:0;left:-2em;width:2.75em;height:2.5em;transform-origin:100% 50%}
.hamster__ear{animation:hamsterEar var(--dur) ease-in-out infinite;background:hsl(0,90%,85%);border-radius:50%;box-shadow:-0.25em 0 hsl(30,90%,55%) inset;top:-0.25em;right:-0.25em;width:0.75em;height:0.75em;transform-origin:50% 75%}
.hamster__eye{animation:hamsterEye var(--dur) linear infinite;background-color:hsl(0,0%,0%);border-radius:50%;top:0.375em;left:1.25em;width:0.5em;height:0.5em}
.hamster__nose{background:hsl(0,90%,75%);border-radius:35% 65% 85% 15% / 70% 50% 50% 30%;top:0.75em;left:0;width:0.2em;height:0.25em}
.hamster__body{animation:hamsterBody var(--dur) ease-in-out infinite;background:hsl(30,90%,90%);border-radius:50% 30% 50% 30% / 15% 60% 40% 40%;box-shadow:0.1em 0.75em 0 hsl(30,90%,55%) inset,0.15em -0.5em 0 hsl(30,90%,80%) inset;top:0.25em;left:2em;width:4.5em;height:3em;transform-origin:17% 50%;transform-style:preserve-3d}
.hamster__limb--fr,.hamster__limb--fl{clip-path:polygon(0 0,100% 0,70% 80%,60% 100%,0% 100%,40% 80%);top:2em;left:0.5em;width:1em;height:1.5em;transform-origin:50% 0}
.hamster__limb--fr{animation:hamsterFRLimb var(--dur) linear infinite;background:linear-gradient(hsl(30,90%,80%) 80%,hsl(0,90%,75%) 80%);transform:rotate(15deg) translateZ(-1px)}
.hamster__limb--fl{animation:hamsterFLLimb var(--dur) linear infinite;background:linear-gradient(hsl(30,90%,90%) 80%,hsl(0,90%,85%) 80%);transform:rotate(15deg)}
.hamster__limb--br,.hamster__limb--bl{border-radius:0.75em 0.75em 0 0;clip-path:polygon(0 0,100% 0,100% 30%,70% 90%,70% 100%,30% 100%,40% 90%,0% 30%);top:1em;left:2.8em;width:1.5em;height:2.5em;transform-origin:50% 30%}
.hamster__limb--br{animation:hamsterBRLimb var(--dur) linear infinite;background:linear-gradient(hsl(30,90%,80%) 90%,hsl(0,90%,75%) 90%);transform:rotate(-25deg) translateZ(-1px)}
.hamster__limb--bl{animation:hamsterBLLimb var(--dur) linear infinite;background:linear-gradient(hsl(30,90%,90%) 90%,hsl(0,90%,85%) 90%);transform:rotate(-25deg)}
.hamster__tail{animation:hamsterTail var(--dur) linear infinite;background:hsl(0,90%,85%);border-radius:0.25em 50% 50% 0.25em;box-shadow:0 -0.2em 0 hsl(0,90%,75%) inset;top:1.5em;right:-0.5em;width:1em;height:0.5em;transform:rotate(30deg) translateZ(-1px);transform-origin:0.25em 0.25em}
.spoke{animation:spoke var(--dur) linear infinite;background:radial-gradient(100% 100% at center,hsl(0,0%,60%) 4.8%,hsla(0,0%,60%,0) 5%),linear-gradient(hsla(0,0%,55%,0) 46.9%,hsl(0,0%,65%) 47% 52.9%,hsla(0,0%,65%,0) 53%) 50% 50% / 99% 99% no-repeat}
@keyframes hamster{from,to{transform:rotate(4deg) translate(-0.8em,1.85em)}50%{transform:rotate(0) translate(-0.8em,1.85em)}}
@keyframes hamsterHead{from,25%,50%,75%,to{transform:rotate(0)}12.5%,37.5%,62.5%,87.5%{transform:rotate(8deg)}}
@keyframes hamsterEye{from,90%,to{transform:scaleY(1)}95%{transform:scaleY(0)}}
@keyframes hamsterEar{from,25%,50%,75%,to{transform:rotate(0)}12.5%,37.5%,62.5%,87.5%{transform:rotate(12deg)}}
@keyframes hamsterBody{from,25%,50%,75%,to{transform:rotate(0)}12.5%,37.5%,62.5%,87.5%{transform:rotate(-2deg)}}
@keyframes hamsterFRLimb{from,25%,50%,75%,to{transform:rotate(50deg) translateZ(-1px)}12.5%,37.5%,62.5%,87.5%{transform:rotate(-30deg) translateZ(-1px)}}
@keyframes hamsterFLLimb{from,25%,50%,75%,to{transform:rotate(-30deg)}12.5%,37.5%,62.5%,87.5%{transform:rotate(50deg)}}
@keyframes hamsterBRLimb{from,25%,50%,75%,to{transform:rotate(-60deg) translateZ(-1px)}12.5%,37.5%,62.5%,87.5%{transform:rotate(20deg) translateZ(-1px)}}
@keyframes hamsterBLLimb{from,25%,50%,75%,to{transform:rotate(20deg)}12.5%,37.5%,62.5%,87.5%{transform:rotate(-60deg)}}
@keyframes hamsterTail{from,25%,50%,75%,to{transform:rotate(30deg) translateZ(-1px)}12.5%,37.5%,62.5%,87.5%{transform:rotate(10deg) translateZ(-1px)}}
@keyframes spoke{from{transform:rotate(0)}to{transform:rotate(-1turn)}}
</style>
</head>
<body>
<div class="loading-overlay" id="loading-overlay">
  <div aria-label="Orange and tan hamster running in a metal wheel" role="img" class="wheel-and-hamster">
    <div class="wheel"></div>
    <div class="hamster">
      <div class="hamster__body">
        <div class="hamster__head">
          <div class="hamster__ear"></div>
          <div class="hamster__eye"></div>
          <div class="hamster__nose"></div>
        </div>
        <div class="hamster__limb hamster__limb--fr"></div>
        <div class="hamster__limb hamster__limb--fl"></div>
        <div class="hamster__limb hamster__limb--br"></div>
        <div class="hamster__limb hamster__limb--bl"></div>
        <div class="hamster__tail"></div>
      </div>
    </div>
    <div class="spoke"></div>
  </div>
</div>
<div class="app-layout">
<div class="sidebar">
  <div class="weather-card" id="weather-card" ondblclick="openLocModal()" style="cursor:pointer" title="双击设置城市">
    <section class="landscape-section">
      <div class="sky"></div>
      <div class="sun"></div>
      <div class="hill-1"></div>
      <div class="hill-2"></div>
      <div class="ocean">
        <div class="reflection"></div><div class="reflection"></div><div class="reflection"></div><div class="reflection"></div><div class="reflection"></div>
        <div class="shadow-hill-1"></div><div class="shadow-hill-2"></div>
      </div>
      <div class="hill-3"></div><div class="hill-4"></div>
      <div class="tree-1"><svg stroke-width="0.00064" stroke="#b77873" fill="#b77873" viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg"><path d="M32,0C18.148,0,12,23.188,12,32c0,9.656,6.883,17.734,16,19.594V60c0,2.211,1.789,4,4,4s4-1.789,4-4v-8.406C45.117,49.734,52,41.656,52,32C52,22.891,46.051,0,32,0z" fill="#b77873"></path></svg></div>
      <div class="tree-2"><svg stroke-width="0.00064" stroke="#b77873" fill="#b77873" viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg"><path d="M32,0C18.148,0,12,23.188,12,32c0,9.656,6.883,17.734,16,19.594V60c0,2.211,1.789,4,4,4s4-1.789,4-4v-8.406C45.117,49.734,52,41.656,52,32C52,22.891,46.051,0,32,0z" fill="#b77873"></path></svg></div>
      <div class="tree-3"><svg stroke-width="0.00064" stroke="#a16773" fill="#a16773" viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg"><path d="M32,0C18.148,0,12,23.188,12,32c0,9.656,6.883,17.734,16,19.594V60c0,2.211,1.789,4,4,4s4-1.789,4-4v-8.406C45.117,49.734,52,41.656,52,32C52,22.891,46.051,0,32,0z" fill="#a16773"></path></svg></div>
      <div class="filter-overlay"></div>
      <div class="weather-info">
        <div class="left-side">
          <div class="weather-icon" id="weather-icon"><svg stroke="#fff" fill="none" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-width="1.5" d="M22 14.3529C22 17.4717 19.4416 20 16.2857 20H11M14.381 9.02721C14.9767 8.81911 15.6178 8.70588 16.2857 8.70588C16.9404 8.70588 17.5693 8.81468 18.1551 9.01498M7.11616 11.6089C6.8475 11.5567 6.56983 11.5294 6.28571 11.5294C3.91878 11.5294 2 13.4256 2 15.7647C2 18.1038 3.91878 20 6.28571 20H7M7.11616 11.6089C6.88706 10.9978 6.7619 10.3369 6.7619 9.64706C6.7619 6.52827 9.32028 4 12.4762 4C15.4159 4 17.8371 6.19371 18.1551 9.01498M7.11616 11.6089C7.68059 11.7184 8.20528 11.9374 8.66667 12.2426M18.1551 9.01498C18.8381 9.24853 19.4623 9.60648 20 10.0614"></path></svg></div>
          <p id="weather-desc">--</p>
        </div>
        <div class="right-side">
          <div class="city-name" id="city-name">--</div>
          <div class="date-text" id="date-text">--</div>
          <div class="w-temp" id="w-temp">--°</div>
        </div>
      </div>
    </section>
    <section class="content-section">
      <div class="forecast" id="forecast-list">
        <div><span>--</span><span>--</span></div>
        <div class="forecast-sep"></div>
        <div><span>--</span><span>--</span></div>
        <div class="forecast-sep"></div>
        <div><span>--</span><span>--</span></div>
      </div>
    </section>
  </div>
  <div class="intro-3d">
    <div class="card3d">
      <div class="logo3d">
        <span class="c3d c3d1"></span><span class="c3d c3d2"></span><span class="c3d c3d3"></span><span class="c3d c3d4"></span>
        <span class="c3d c3d5">
          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40"><text x="20" y="28" text-anchor="middle" font-size="26" font-weight="900" fill="#fff" font-family="sans-serif">LX</text></svg>
        </span>
      </div>
      <div class="glass3d"></div>
      <div class="content3d">
        <span class="title3d">DX4600 Pro</span>
        <span class="text3d">UGREEN NAS · 硬件监控<br>fnOS · Intel N6005</span>
      </div>
      <div class="bottom3d">
        <div class="specs3d">
          <span class="chip3d">4-Core</span>
          <span class="chip3d">6-Bay</span>
          <span class="chip3d">2.5GbE</span>
        </div>
        <span class="link3d">UGREEN</span>
      </div>
    </div>
  </div>
</div>
<div class="main">
<div style="display:flex;justify-content:center;margin-bottom:16px">
  <button type="button" class="title-btn">
    <strong>DX4600 Pro</strong>
    <span class="title-sub">硬件监控面板</span>
    <div id="container-stars"><div id="stars"></div></div>
    <div id="glow">
      <div class="circle"></div>
      <div class="circle"></div>
    </div>
  </button>
</div>

<div class="grid">
  <!-- Clock Card -->
  <div class="glass-card" style="display:flex;flex-direction:column;align-items:center">
    <div class="glass-card-header">
      <span class="glass-card-title">时间</span>
    </div>
    <hr class="glass-divider">
    <div class="clock" id="clock">
      <div class="numbers">
        <span class="number big" style="transform:translate(-50%,-50%) rotateZ(0deg) translateY(-85px)"></span>
        <span class="number" style="transform:translate(-50%,-50%) rotateZ(30deg) translateY(-85px)"></span>
        <span class="number" style="transform:translate(-50%,-50%) rotateZ(60deg) translateY(-85px)"></span>
        <span class="number big" style="transform:translate(-50%,-50%) rotateZ(90deg) translateY(-85px)"></span>
        <span class="number" style="transform:translate(-50%,-50%) rotateZ(120deg) translateY(-85px)"></span>
        <span class="number" style="transform:translate(-50%,-50%) rotateZ(150deg) translateY(-85px)"></span>
        <span class="number big" style="transform:translate(-50%,-50%) rotateZ(180deg) translateY(-85px)"></span>
        <span class="number" style="transform:translate(-50%,-50%) rotateZ(210deg) translateY(-85px)"></span>
        <span class="number" style="transform:translate(-50%,-50%) rotateZ(240deg) translateY(-85px)"></span>
        <span class="number big" style="transform:translate(-50%,-50%) rotateZ(270deg) translateY(-85px)"></span>
        <span class="number" style="transform:translate(-50%,-50%) rotateZ(300deg) translateY(-85px)"></span>
        <span class="number" style="transform:translate(-50%,-50%) rotateZ(330deg) translateY(-85px)"></span>
      </div>
      <div class="needles">
        <span class="h" id="hand-h"></span>
        <span class="m" id="hand-m"></span>
        <span class="s" id="hand-s"></span>
        <span class="center"></span>
      </div>
    </div>
  </div>
  <!-- Temperature Card -->
  <div class="glass-card">
    <div class="glass-card-header">
      <span class="glass-card-title">温度监控</span>
    </div>
    <hr class="glass-divider">
    <div class="temp-sliders" id="temp-list"></div>
  </div>

  <!-- Fan Card -->
  <div class="glass-card">
    <div class="glass-card-header">
      <span class="glass-card-title">风扇控制</span>
    </div>
    <hr class="glass-divider">
    <div class="fan-visual" id="fan-visual"></div>
    <div class="mode-btns">
      <button class="btn active" id="btn-auto" onclick="setFanMode('auto')">自动温控</button>
      <button class="btn" id="btn-manual" onclick="setFanMode('manual')">手动控制</button>
    </div>
    <div id="manual-controls" style="display:none">
      <div class="slider-row">
        <label>风扇数量</label>
        <div class="slider-wrap"><input type="range" class="glass-slider" id="fan-slider" min="0" max="5" value="0" oninput="setFanCount(this.value)"></div>
        <span id="fan-count-label">0</span>
      </div>
    </div>
    <div class="curve-info">
      自动模式曲线: &lt;45°C 全关 → 50°C 1个 → 55°C 2个 → 60°C 3个 → 65°C 4个 → 70°C 全开
    </div>
  </div>
</div>

<!-- Service Status + DKMS -->
<div class="grid">
  <div class="glass-card">
    <div class="glass-card-header">
      <span class="glass-card-title">LED 服务状态</span>
    </div>
    <hr class="glass-divider">
    <div id="svc-list"><div style="color:var(--text2);font-size:.85em">加载中...</div></div>
    <div style="margin-top:10px">
      <button class="btn btn-sm" onclick="restartAllServices()">全部重启</button>
    </div>
  </div>
  <div class="glass-card">
    <div class="glass-card-header">
      <span class="glass-card-title">驱动编译 (DKMS)</span>
    </div>
    <hr class="glass-divider">
    <div class="dkms-info" id="dkms-info">
      <div><span>内核:</span> <strong id="dkms-kernel">-</strong></div>
      <div><span>模块:</span> <strong id="dkms-module">-</strong></div>
      <div><span>已加载:</span> <strong id="dkms-loaded">-</strong></div>
      <div><span>头文件:</span> <strong id="dkms-headers">-</strong></div>
    </div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <button class="btn" id="btn-headers" onclick="doInstallHeaders()">安装内核头文件</button>
      <button class="btn btn-green" id="btn-rebuild" onclick="doRebuild()">编译并加载</button>
      <span id="rebuild-status" style="font-size:.8em"></span>
    </div>
    <pre class="dkms-output" id="dkms-output"></pre>
  </div>
</div>

<!-- LED Card -->
<div class="glass-card" style="margin-bottom:16px">
  <div class="glass-card-header">
    <span class="glass-card-title">LED 灯控</span>
  </div>
  <hr class="glass-divider">
  <div class="led-grid" id="led-grid"></div>
  <hr class="glass-divider">
  <div class="glass-bottom-bar">
    <button class="glass-btn" onclick="setAllLeds()">统一设置</button>
    <input type="color" id="all-color" value="#ffffff" title="选择颜色" onfocus="colorPickerOpen=true" onblur="colorPickerOpen=false;refresh()">
    <div class="slider-row" style="flex:1;margin:0">
      <label>亮度</label>
      <div class="slider-wrap"><input type="range" class="glass-slider" id="all-brightness" min="0" max="255" value="128"></div>
    </div>
    <button class="btn btn-green" onclick="saveColorsToConfig()">保存到配置文件</button>
    <button class="btn" style="border-color:var(--orange);color:var(--orange)" onclick="ledRepair()" id="btn-repair">🔧 修复</button>
    <span id="repair-msg" style="font-size:.8em;color:var(--text2)"></span>
    <pre id="repair-steps" style="display:none;margin:8px 0 0;padding:8px 10px;background:rgba(0,0,0,.25);border-radius:8px;font-size:.72em;line-height:1.5;white-space:pre-wrap;color:var(--text2);max-height:240px;overflow:auto"></pre>
  </div>
</div>

<!-- Config Editor -->
<div class="glass-card" style="margin-bottom:16px">
  <div class="glass-card-header">
    <div class="terminal-dots">
      <div class="terminal-dot red"></div>
      <div class="terminal-dot yellow"></div>
      <div class="terminal-dot green"></div>
    </div>
    <span class="glass-card-title">LED 配置文件 — bash</span>
  </div>
  <div id="disk-serial-result" style="display:none;margin:12px 16px 0">
    <table style="width:100%;border-collapse:collapse;font-size:.82em">
      <thead><tr style="color:var(--text2);border-bottom:1px solid rgba(255,255,255,0.08)">
        <th style="padding:6px 8px;text-align:left">槽位</th>
        <th style="padding:6px 8px;text-align:left">设备</th>
        <th style="padding:6px 8px;text-align:left">型号</th>
        <th style="padding:6px 8px;text-align:left">容量</th>
        <th style="padding:6px 8px;text-align:left">序列号</th>
      </tr></thead>
      <tbody id="disk-serial-tbody"></tbody>
    </table>
    <div style="margin-top:8px;display:flex;gap:8px;align-items:center">
      <button class="btn btn-sm btn-green" onclick="fillDiskSerial()">填入 DISK_SERIAL</button>
      <span id="disk-fill-msg" style="font-size:.8em;color:var(--text2)"></span>
    </div>
  </div>
  <div class="terminal-body">
    <textarea class="config-editor" id="config-editor" spellcheck="false">加载中...</textarea>
  </div>
  <div class="terminal-toolbar">
    <button class="btn btn-sm" onclick="detectDiskSerials()">检测硬盘序列号</button>
    <span id="disk-serial-status" style="font-size:.8em;color:var(--text2)"></span>
    <div style="flex:1"></div>
    <button class="btn btn-sm btn-green" onclick="saveConfig()">保存配置</button>
    <button class="btn btn-sm" onclick="loadConfig()">重新加载</button>
    <button class="btn btn-sm" onclick="restartAllServices()">重启服务生效</button>
    <span class="config-msg" id="config-msg"></span>
  </div>
</div>
</div>
</div>

<div class="status-bar">
  <span id="last-update">等待数据...</span>
  <span id="health-indicator"></span>
  <span>DX4600 Pro · Intel N6005</span>
</div>

<div class="loc-modal-overlay" id="loc-modal">
  <div class="loc-modal">
    <h3>设置城市</h3>
    <input type="text" id="loc-input" placeholder="输入城市名，如：北京、上海、Tokyo..." autocomplete="off">
    <div class="loc-hint">输入城市名获取当地天气，留空则使用自动定位</div>
    <div class="loc-btns">
      <button class="btn btn-sm" onclick="clearCity()">自动定位</button>
      <button class="btn btn-sm btn-green" onclick="saveCity()">保存</button>
    </div>
  </div>
</div>
<script>
let currentMode = 'auto';
let pollTimer = null;
let configDirty = false;
let colorPickerOpen = false;

function updateClock() {
  const now = new Date();
  const s = now.getSeconds();
  const m = now.getMinutes();
  const h = now.getHours() % 12;
  document.getElementById('hand-s').style.transform = `translate(-50%,-80%) rotateZ(${s * 6}deg)`;
  document.getElementById('hand-m').style.transform = `translate(-50%,-100%) rotateZ(${m * 6 + s * 0.1}deg)`;
  document.getElementById('hand-h').style.transform = `translate(-50%,-100%) rotateZ(${h * 30 + m * 0.5}deg)`;
}

const WEATHER_CODES = {
  0: {desc:'晴天',icon:'☀️'}, 1: {desc:'少云',icon:'🌤️'}, 2: {desc:'多云',icon:'⛅'}, 3: {desc:'阴天',icon:'☁️'},
  45:{desc:'雾',icon:'🌫️'}, 48:{desc:'霜雾',icon:'🌫️'},
  51:{desc:'小雨',icon:'🌦️'}, 53:{desc:'中雨',icon:'🌧️'}, 55:{desc:'大雨',icon:'🌧️'},
  61:{desc:'小雨',icon:'🌧️'}, 63:{desc:'中雨',icon:'🌧️'}, 65:{desc:'暴雨',icon:'🌧️'},
  71:{desc:'小雪',icon:'🌨️'}, 73:{desc:'中雪',icon:'❄️'}, 75:{desc:'大雪',icon:'❄️'},
  80:{desc:'阵雨',icon:'🌦️'}, 81:{desc:'中阵雨',icon:'🌧️'}, 82:{desc:'大阵雨',icon:'🌧️'},
  85:{desc:'小雪',icon:'🌨️'}, 86:{desc:'大雪',icon:'❄️'},
  95:{desc:'雷阵雨',icon:'🌩️'}, 96:{desc:'雷阵雨',icon:'🌩️'}, 99:{desc:'雷阵雨',icon:'🌩️'}
};
const WEEKDAYS = ['日','一','二','三','四','五','六'];

async function fetchWeather() {
  try {
    const savedCity = localStorage.getItem('wx-city');
    let lat, lon, cityName;
    if (savedCity) {
      cityName = savedCity;
      const geoRes = await fetch(`https://geocoding-api.open-meteo.com/v1/search?name=${encodeURIComponent(savedCity)}&count=1&language=zh`);
      const geo = await geoRes.json();
      if (geo.results && geo.results.length) {
        lat = geo.results[0].latitude;
        lon = geo.results[0].longitude;
        const r = geo.results[0];
        cityName = [r.admin1, r.name].filter(Boolean).join(' ') || savedCity;
        if (r.country) cityName += ', ' + r.country;
      }
    }
    if (!lat) {
      const locRes = await fetch('http://ip-api.com/json?lang=zh-CN&fields=city,country,lat,lon');
      const loc = await locRes.json();
      lat = loc.lat; lon = loc.lon; cityName = loc.city || loc.country || '--';
    }
    document.getElementById('city-name').textContent = cityName;
    const wRes = await fetch(`https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lon}&current=temperature_2m,weather_code&daily=temperature_2m_max,temperature_2m_min,weather_code&timezone=auto&forecast_days=4`);
    const w = await wRes.json();
    const cur = w.current;
    const wc = WEATHER_CODES[cur.weather_code] || {desc:'--',icon:'🌤️'};
    document.getElementById('weather-icon').textContent = wc.icon;
    document.getElementById('weather-desc').textContent = wc.desc;
    document.getElementById('w-temp').textContent = Math.round(cur.temperature_2m) + '°C';
    const now = new Date();
    document.getElementById('date-text').textContent = `星期${WEEKDAYS[now.getDay()]} ${now.getMonth()+1}/${now.getDate()}`;
    let fcHtml = '';
    for (let i = 1; i <= 3; i++) {
      const d = w.daily;
      const date = new Date(d.time[i]);
      const fc = WEATHER_CODES[d.weather_code[i]] || {desc:'--'};
      const hi = Math.round(d.temperature_2m_max[i]);
      const lo = Math.round(d.temperature_2m_min[i]);
      fcHtml += `<div><span>${date.getMonth()+1}/${date.getDate()} ${fc.desc}</span><span>${lo}°/${hi}°</span></div>`;
      if (i < 3) fcHtml += '<div class="forecast-sep"></div>';
    }
    document.getElementById('forecast-list').innerHTML = fcHtml;
  } catch(e) { console.log('Weather fetch error:', e); }
}

function openLocModal() {
  const saved = localStorage.getItem('wx-city') || '';
  document.getElementById('loc-input').value = saved;
  document.getElementById('loc-modal').classList.add('show');
  setTimeout(() => document.getElementById('loc-input').focus(), 100);
}
function saveCity() {
  const city = document.getElementById('loc-input').value.trim();
  if (city) localStorage.setItem('wx-city', city);
  else localStorage.removeItem('wx-city');
  document.getElementById('loc-modal').classList.remove('show');
  fetchWeather();
}
function clearCity() {
  localStorage.removeItem('wx-city');
  document.getElementById('loc-input').value = '';
  document.getElementById('loc-modal').classList.remove('show');
  fetchWeather();
}
document.getElementById('loc-modal').addEventListener('click', function(e) {
  if (e.target === this) this.classList.remove('show');
});

function tempColor(t) {
  if (t < 45) return 'var(--green)';
  if (t < 60) return 'var(--blue)';
  if (t < 70) return 'var(--orange)';
  return 'var(--red)';
}

function renderTemps(temps) {
  let html = '';
  if (temps.cpu_package !== undefined) {
    const t = temps.cpu_package;
    html += `<div class="temp-row"><label>CPU 封装</label><div class="temp-content"><div class="temp-bar-wrap"><div class="temp-bar-fill" style="width:${Math.min(100,t/105*100)}%;background:${tempColor(t)}"></div></div><div class="temp-divider"></div><span class="temp-val" style="color:${tempColor(t)}">${t.toFixed(1)}°C</span></div></div>`;
  }
  for (let i = 0; i < 4; i++) {
    const key = `cpu_core${i}`;
    if (temps[key]) {
      const t = temps[key].temp;
      html += `<div class="temp-row"><label>${temps[key].label}</label><div class="temp-content"><div class="temp-bar-wrap"><div class="temp-bar-fill" style="width:${Math.min(100,t/105*100)}%;background:${tempColor(t)}"></div></div><div class="temp-divider"></div><span class="temp-val" style="color:${tempColor(t)}">${t.toFixed(1)}°C</span></div></div>`;
    }
  }
  if (temps.nvme !== undefined) {
    const t = temps.nvme;
    html += `<div class="temp-row"><label>NVMe 固态</label><div class="temp-content"><div class="temp-bar-wrap"><div class="temp-bar-fill" style="width:${Math.min(100,t/85*100)}%;background:${tempColor(t)}"></div></div><div class="temp-divider"></div><span class="temp-val" style="color:${tempColor(t)}">${t.toFixed(1)}°C</span></div></div>`;
  }
  if (temps.acpi !== undefined) {
    const t = temps.acpi;
    html += `<div class="temp-row"><label>ACPI</label><div class="temp-content"><span class="temp-val" style="color:${tempColor(t)}">${t.toFixed(1)}°C</span></div></div>`;
  }
  document.getElementById('temp-list').innerHTML = html;
}

function renderFans(data) {
  const fans = data.fans;
  let html = '';
  fans.forEach((f, i) => {
    html += `<div class="fan-icon ${f.on ? 'on' : 'off'}" title="风扇 ${i+1}"><svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="2.2" fill="none" stroke="currentColor" stroke-width=".8"/><path d="M12 10C11 7.5 10.5 4.8 11 3c1-.5 2-.5 3 0 .5 1.8 0 4.5-1 7z"/><path d="M12 10C11 7.5 10.5 4.8 11 3c1-.5 2-.5 3 0 .5 1.8 0 4.5-1 7z" transform="rotate(72 12 12)"/><path d="M12 10C11 7.5 10.5 4.8 11 3c1-.5 2-.5 3 0 .5 1.8 0 4.5-1 7z" transform="rotate(144 12 12)"/><path d="M12 10C11 7.5 10.5 4.8 11 3c1-.5 2-.5 3 0 .5 1.8 0 4.5-1 7z" transform="rotate(216 12 12)"/><path d="M12 10C11 7.5 10.5 4.8 11 3c1-.5 2-.5 3 0 .5 1.8 0 4.5-1 7z" transform="rotate(288 12 12)"/></svg></div>`;
  });
  document.getElementById('fan-visual').innerHTML = html;
  if (data.mode !== currentMode) {
    currentMode = data.mode;
    document.getElementById('btn-auto').classList.toggle('active', currentMode === 'auto');
    document.getElementById('btn-manual').classList.toggle('active', currentMode === 'manual');
    document.getElementById('manual-controls').style.display = currentMode === 'manual' ? 'block' : 'none';
  }
  if (currentMode === 'manual') {
    document.getElementById('fan-slider').value = data.manual_count;
    document.getElementById('fan-count-label').textContent = data.manual_count;
  }
}

function renderLeds(leds) {
  let html = '';
  leds.forEach(led => {
    const hex = '#' + [led.r, led.g, led.b].map(v => v.toString(16).padStart(2, '0')).join('');
    const isOn = !(led.state === 'off' || led.brightness === 0);
    const dotStyle = isOn ? `background:${hex};box-shadow:0 0 20px ${hex}` : 'background:#333';
    html += `<div class="led-card">
      <div class="led-dot" style="${dotStyle}"></div>
      <div class="led-name">${led.name}</div>
      <div class="led-controls">
        <input type="color" value="${hex}" onfocus="colorPickerOpen=true" onblur="colorPickerOpen=false;refresh()" onchange="setLedColor('${led.name}',this.value)" title="颜色">
        <button class="led-btn" onclick="setLedState('${led.name}','on')">开</button>
        <button class="led-btn" onclick="setLedState('${led.name}','off')">关</button>
        <button class="led-btn" onclick="setLedBlink('${led.name}')">闪</button>
      </div>
    </div>`;
  });
  document.getElementById('led-grid').innerHTML = html;
}

function renderServices(services) {
  let html = '';
  services.forEach(svc => {
    const dotClass = svc.status === 'active' ? 'active' : svc.status === 'failed' ? 'failed' : 'inactive';
    const statusText = svc.status === 'active' ? '运行中' : svc.status === 'failed' ? '失败' : svc.status === 'inactive' ? '已停止' : svc.status;
    html += `<div class="svc-row">
      <div class="svc-dot ${dotClass}"></div>
      <div class="svc-info">
        <div class="svc-name">${svc.name}</div>
        <div class="svc-label">${svc.label}</div>
      </div>
      <div class="svc-status ${dotClass}">${statusText}</div>
      <button class="btn btn-sm" onclick="restartService('${svc.name}')" title="重启">↻</button>
    </div>`;
  });
  document.getElementById('svc-list').innerHTML = html;
}

function renderDkms(dkms) {
  document.getElementById('dkms-kernel').textContent = dkms.kernel;
  document.getElementById('dkms-module').textContent = dkms.module;
  document.getElementById('dkms-loaded').textContent = dkms.module_loaded ? '是' : '否';
  document.getElementById('dkms-loaded').style.color = dkms.module_loaded ? 'var(--green)' : 'var(--red)';
  const hdrEl = document.getElementById('dkms-headers');
  const hdrBtn = document.getElementById('btn-headers');
  if (dkms.headers_installed) {
    hdrEl.textContent = '已安装';
    hdrEl.style.color = 'var(--green)';
    hdrBtn.textContent = '头文件已安装';
    hdrBtn.disabled = true;
  } else {
    hdrEl.textContent = '未安装';
    hdrEl.style.color = 'var(--orange)';
    hdrBtn.textContent = '安装内核头文件';
    hdrBtn.disabled = false;
  }
}

function updateHealth(health) {
  const el = document.getElementById('health-indicator');
  if (!health) { el.innerHTML = ''; return; }
  const errors = health.issues.filter(i => i.severity === 'error').length;
  const warns = health.issues.filter(i => i.severity === 'warning').length;
  if (errors > 0) {
    el.innerHTML = '<span class="dot red"></span> ' + errors + ' 个错误';
    el.title = health.issues.filter(i => i.severity === 'error').map(i => i.component + ': ' + i.message).join('\n');
    el.style.color = 'var(--red)';
  } else if (warns > 0) {
    el.innerHTML = '<span class="dot orange"></span> ' + warns + ' 个警告';
    el.title = health.issues.map(i => i.component + ': ' + i.message).join('\n');
    el.style.color = 'var(--orange)';
  } else {
    el.innerHTML = '<span class="dot green"></span> 系统正常';
    el.title = health.ok_count + ' 项检查通过';
    el.style.color = 'var(--green)';
  }
}

// ─── API helpers ───
async function api(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  return res.json();
}

let firstLoad = true;
async function refresh() {
  // Skip polling entirely when the tab is hidden — saves CPU/network when
  // the user isn't looking. Browser-throttled but still wasted on a NAS.
  if (document.hidden) return;
  try {
    const data = await api('GET', '/api/realtime');
    renderTemps(data.temps);
    renderFans(data.fans);
    if (!colorPickerOpen && data.leds) renderLeds(data.leds);
    document.getElementById('last-update').textContent = '最后更新: ' + new Date().toLocaleTimeString('zh-CN');
    if (firstLoad) {
      firstLoad = false;
      document.getElementById('loading-overlay').classList.add('hidden');
      refreshAll(); // load everything on first visit
    }
  } catch(e) {
    document.getElementById('last-update').textContent = '连接失败: ' + e.message;
  }
}

async function refreshAll() {
  if (document.hidden) return;
  try {
    const data = await api('GET', '/api/all');
    if (!colorPickerOpen) renderLeds(data.leds);
    renderServices(data.services);
    renderDkms(data.dkms);
    updateHealth(data.health);
  } catch(e) { console.log('Status refresh error:', e); }
}

// Catch up immediately when the tab becomes visible again
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) { refresh(); refreshAll(); }
});

// ─── Fan controls ───
async function setFanMode(mode) {
  document.getElementById('manual-controls').style.display = mode === 'manual' ? 'block' : 'none';
  await api('POST', '/api/fan/mode', { mode, count: parseInt(document.getElementById('fan-slider').value) });
  refresh();
}

async function setFanCount(val) {
  document.getElementById('fan-count-label').textContent = val;
  await api('POST', '/api/fan/set', { count: parseInt(val) });
  refresh();
}

// ─── LED controls ───
function hexToRgb(hex) {
  return { r: parseInt(hex.slice(1,3),16), g: parseInt(hex.slice(3,5),16), b: parseInt(hex.slice(5,7),16) };
}

async function setLedColor(name, hex) { await api('POST', '/api/led/set', { name, color: hexToRgb(hex) }); refresh(); }
async function setLedState(name, state) { await api('POST', '/api/led/set', { name, brightness: state === 'on' ? 128 : 0 }); refresh(); }
async function setLedBlink(name) { await api('POST', '/api/led/set', { name, blink_type: 'blink 500 500' }); refresh(); }
async function setAllLeds() {
  await api('POST', '/api/led/all', { color: hexToRgb(document.getElementById('all-color').value), brightness: parseInt(document.getElementById('all-brightness').value) });
  refresh();
}

async function ledRepair() {
  const btn = document.getElementById('btn-repair');
  const msg = document.getElementById('repair-msg');
  btn.disabled = true; btn.textContent = '修复中...'; msg.textContent = '';
  try {
    const res = await api('POST', '/api/led/repair');
    msg.textContent = res.message;
    msg.style.color = res.ok ? 'var(--green)' : 'var(--red)';
    const steps = document.getElementById('repair-steps');
    if (res.steps && res.steps.length) {
      steps.style.display = 'block';
      steps.textContent = res.steps.join('\n');
    } else {
      steps.style.display = 'none';
    }
    if (res.ok) refreshAll();
  } catch(e) {
    msg.textContent = '修复请求失败: ' + e.message;
    msg.style.color = 'var(--red)';
  }
  btn.disabled = false; btn.textContent = '🔧 修复';
}

async function saveColorsToConfig() {
  try {
    const res = await api('POST', '/api/config/sync-leds');
    if (res.ok) {
      showConfigMsg(res.message || '已保存', 'var(--green)');
      loadConfig();
    } else {
      showConfigMsg('保存失败: ' + (res.message || '未知错误'), 'var(--red)');
    }
  } catch(e) {
    showConfigMsg('保存失败: ' + e.message, 'var(--red)');
  }
}

// ─── Disk serial detection ───
let detectedDisks = [];

async function detectDiskSerials() {
  const statusEl = document.getElementById('disk-serial-status');
  statusEl.textContent = '检测中...';
  statusEl.style.color = 'var(--orange)';
  try {
    const disks = await api('GET', '/api/disk-serials');
    detectedDisks = disks;
    if (disks.length === 0) {
      statusEl.textContent = '未检测到 SATA 硬盘';
      statusEl.style.color = 'var(--red)';
      document.getElementById('disk-serial-result').style.display = 'none';
      return;
    }
    const tbody = document.getElementById('disk-serial-tbody');
    tbody.innerHTML = disks.map(d => `<tr style="border-bottom:1px solid var(--border)">
      <td style="padding:6px 8px;color:var(--accent)">槽位 ${d.slot}</td>
      <td style="padding:6px 8px">/dev/${d.dev}</td>
      <td style="padding:6px 8px;color:var(--text2)">${d.model || '-'}</td>
      <td style="padding:6px 8px;color:var(--text2)">${d.size_gb ? d.size_gb + ' GB' : '-'}</td>
      <td style="padding:6px 8px;font-family:monospace;color:var(--green)">${d.serial || '(空)'}</td>
    </tr>`).join('');
    document.getElementById('disk-serial-result').style.display = 'block';
    statusEl.textContent = `检测到 ${disks.length} 块硬盘`;
    statusEl.style.color = 'var(--green)';
  } catch(e) {
    statusEl.textContent = '检测失败: ' + e.message;
    statusEl.style.color = 'var(--red)';
  }
}

function fillDiskSerial() {
  if (detectedDisks.length === 0) return;
  const maxSlot = Math.max(...detectedDisks.map(d => d.slot), 4);
  const slots = [];
  for (let i = 1; i <= maxSlot; i++) {
    const disk = detectedDisks.find(d => d.slot === i);
    slots.push(disk && disk.serial ? disk.serial : '');
  }
  const serialLine = 'DISK_SERIAL="' + slots.map(s => s || '\\"\\"').join(' ') + '"';
  const editor = document.getElementById('config-editor');
  const lines = editor.value.split('\n');
  let found = false;
  for (let i = 0; i < lines.length; i++) {
    const stripped = lines[i].trim();
    if (stripped.startsWith('#') || !stripped) continue;
    if (stripped.split('=', 1)[0].trim() === 'DISK_SERIAL') {
      lines[i] = serialLine;
      found = true;
      break;
    }
  }
  if (!found) {
    lines.push(serialLine);
  }
  editor.value = lines.join('\n');
  configDirty = true;
  const msgEl = document.getElementById('disk-fill-msg');
  msgEl.textContent = '已填入，记得保存配置';
  msgEl.style.color = 'var(--green)';
  setTimeout(() => { msgEl.textContent = ''; }, 5000);
}

// ─── Service controls ───
async function restartService(name) {
  const res = await api('POST', '/api/service/restart', { name });
  refresh();
}

async function restartAllServices() {
  const services = ['ugreen-led-init', 'ugreen-probe-leds', 'ugreen-power-led', 'ugreen-diskiomon', 'ugreen-netdevmon@enp2s0'];
  for (const svc of services) {
    await api('POST', '/api/service/restart', { name: svc });
  }
  refresh();
}

// ─── Config editor ───
async function loadConfig() {
  try {
    const data = await api('GET', '/api/config/leds');
    document.getElementById('config-editor').value = data.content || '';
    configDirty = false;
    showConfigMsg('已加载', 'var(--green)');
  } catch(e) {
    showConfigMsg('加载失败: ' + e.message, 'var(--red)');
  }
}

async function saveConfig() {
  const content = document.getElementById('config-editor').value;
  try {
    const res = await api('POST', '/api/config/leds', { content });
    if (res.ok) {
      configDirty = false;
      showConfigMsg('已保存', 'var(--green)');
    } else {
      showConfigMsg('保存失败', 'var(--red)');
    }
  } catch(e) {
    showConfigMsg('保存失败: ' + e.message, 'var(--red)');
  }
}

function showConfigMsg(text, color) {
  const el = document.getElementById('config-msg');
  el.textContent = text;
  el.style.color = color;
  el.style.opacity = 1;
  setTimeout(() => { el.style.opacity = 0; }, 3000);
}

// ─── DKMS headers install ───
async function doInstallHeaders() {
  const btn = document.getElementById('btn-headers');
  const statusEl = document.getElementById('rebuild-status');
  const outputEl = document.getElementById('dkms-output');
  btn.disabled = true;
  btn.textContent = '安装中...';
  statusEl.textContent = '正在安装内核头文件，请稍候...';
  statusEl.style.color = 'var(--orange)';
  try {
    const res = await api('POST', '/api/dkms/headers');
    outputEl.style.display = 'block';
    outputEl.textContent = res.output;
    if (res.ok) {
      statusEl.textContent = '头文件安装成功';
      statusEl.style.color = 'var(--green)';
    } else {
      statusEl.textContent = '头文件安装失败，请查看输出';
      statusEl.style.color = 'var(--red)';
    }
  } catch(e) {
    statusEl.textContent = '请求失败: ' + e.message;
    statusEl.style.color = 'var(--red)';
  }
  btn.textContent = '安装内核头文件';
  btn.disabled = false;
  refresh();
}

// ─── DKMS rebuild ───
async function doRebuild() {
  const btn = document.getElementById('btn-rebuild');
  const statusEl = document.getElementById('rebuild-status');
  const outputEl = document.getElementById('dkms-output');
  btn.disabled = true;
  btn.textContent = '编译中...';
  statusEl.textContent = '正在编译并加载驱动，请稍候...';
  statusEl.style.color = 'var(--orange)';
  try {
    const res = await api('POST', '/api/dkms/rebuild');
    outputEl.style.display = 'block';
    outputEl.textContent = res.output;
    if (res.ok) {
      statusEl.textContent = '编译成功';
      statusEl.style.color = 'var(--green)';
    } else {
      statusEl.textContent = '编译过程有错误，请查看输出';
      statusEl.style.color = 'var(--red)';
    }
  } catch(e) {
    statusEl.textContent = '请求失败: ' + e.message;
    statusEl.style.color = 'var(--red)';
  }
  btn.disabled = false;
  btn.textContent = '编译并加载';
  refresh();
}

// ─── Init ───
refresh();
loadConfig();
pollTimer = setInterval(refresh, 3000);
setInterval(refreshAll, 30000);
updateClock();
setInterval(updateClock, 1000);
fetchWeather();
</script>
</body>
</html>"""

if __name__ == "__main__":
    # Start auto fan control
    start_auto()
    print(f"Starting DX4600 Pro Hardware Monitor on http://{HOST}:{PORT}")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True  # don't block shutdown on lingering requests
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_auto()
        server.server_close()
        print("Stopped.")

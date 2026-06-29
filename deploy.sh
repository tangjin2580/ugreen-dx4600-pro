#!/bin/bash
# ============================================================
# UGREEN DX4600 Pro LED + 面板 一键部署脚本
# 目标系统: fnOS (Debian 12 bookworm, kernel 6.18.18-trim)
# 运行方式: sudo bash deploy.sh
# ============================================================
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[-]${NC} $*"; }

# 检查 root
if [[ $EUID -ne 0 ]]; then
    err "请使用 sudo 或 root 用户运行此脚本"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET_USER="${TARGET_USER:-lixin}"
TARGET_HOME="/home/${TARGET_USER}"

echo ""
echo "========================================"
echo "  UGREEN DX4600 Pro 一键部署"
echo "========================================"
echo ""

# ─── 1. 系统依赖 ───
log "安装系统依赖..."
apt-get update -qq
apt-get install -y -qq dkms make gcc linux-headers-$(uname -r) smartmontools i2c-tools lm-sensors > /dev/null 2>&1 || true
log "系统依赖安装完成"

# ─── 2. DKMS 内核模块 ───
log "安装 DKMS 内核模块 led-ugreen/0.3..."
DKMS_SRC="${SCRIPT_DIR}/dkms/led-ugreen-0.3"
DKMS_DST="/usr/src/led-ugreen-0.3"

if [[ ! -d "$DKMS_SRC" ]]; then
    err "DKMS 源码目录不存在: ${DKMS_SRC}"
    exit 1
fi

# 清理旧版本
dkms remove led-ugreen/0.3 --all 2>/dev/null || true
dkms remove ugreen-led/0.1 --all 2>/dev/null || true

# 安装源码
mkdir -p "$DKMS_DST"
cp -f "$DKMS_SRC"/* "$DKMS_DST/"

# 编译安装
dkms add led-ugreen/0.3
dkms build led-ugreen/0.3 -k $(uname -r)
dkms install led-ugreen/0.3 -k $(uname -r) --force
log "DKMS 模块编译安装完成"

# ─── 3. LED 控制脚本 ───
log "安装 LED 控制脚本..."
SCRIPTS_SRC="${SCRIPT_DIR}/usr/bin"
for f in ugreen-power-led ugreen-diskiomon ugreen-netdevmon ugreen-probe-leds \
         ugreen-blink-disk ugreen-check-standby ugreen-detect-disks ugreen-detect-network; do
    if [[ -f "${SCRIPTS_SRC}/${f}" ]]; then
        cp -f "${SCRIPTS_SRC}/${f}" "/usr/bin/${f}"
        chmod +x "/usr/bin/${f}"
        log "  已安装 /usr/bin/${f}"
    else
        warn "  跳过 ${f} (文件不存在)"
    fi
done

# ─── 4. 配置文件 ───
log "安装配置文件..."
cp -f "${SCRIPT_DIR}/etc/ugreen-leds.conf" /etc/ugreen-leds.conf
log "  /etc/ugreen-leds.conf"

# ─── 5. Sudoers ───
log "安装 sudoers 规则..."
cp -f "${SCRIPT_DIR}/etc/sudoers.d/lixin-ugreen" /etc/sudoers.d/lixin-ugreen
chmod 440 /etc/sudoers.d/lixin-ugreen
log "  /etc/sudoers.d/lixin-ugreen"

# ─── 6. Systemd 服务 ───
log "安装 systemd 服务..."
SERVICES=(
    fnos-panel.service
    ugreen-led-init.service
    ugreen-probe-leds.service
    ugreen-power-led.service
    ugreen-diskiomon.service
    ugreen-netdevmon@.service
)
for svc in "${SERVICES[@]}"; do
    cp -f "${SCRIPT_DIR}/etc/systemd/system/${svc}" "/etc/systemd/system/${svc}"
    log "  ${svc}"
done
systemctl daemon-reload

# ─── 7. Web 面板 ───
log "安装 Web 面板..."
cp -f "${SCRIPT_DIR}/fnos_panel.py" "${TARGET_HOME}/fnos_panel.py"
chown "${TARGET_USER}:${TARGET_USER}" "${TARGET_HOME}/fnos_panel.py"

# ─── 8. 启动服务 ───
log "启动服务..."

# 加载内核模块 + 创建 I2C 设备
modprobe led-ugreen 2>/dev/null || true
sleep 0.5
echo 0x3a > /sys/bus/i2c/devices/i2c-0/delete_device 2>/dev/null || true
sleep 0.2
echo "led-ugreen 0x3a" > /sys/bus/i2c/devices/i2c-0/new_device 2>/dev/null || true

# 启动服务链
systemctl enable --now ugreen-led-init.service
systemctl enable --now ugreen-probe-leds.service
systemctl enable --now ugreen-power-led.service
systemctl enable --now ugreen-diskiomon.service
systemctl enable --now "ugreen-netdevmon@enp2s0.service"
systemctl enable --now fnos-panel.service

echo ""
log "========================================"
log "  部署完成!"
log "========================================"
echo ""
echo "  Web 面板: http://$(hostname -I | awk '{print $1}'):9092"
echo ""
echo "  服务状态:"
systemctl is-active fnos-panel.service              && echo "    fnos-panel:              运行中" || echo "    fnos-panel:              未运行"
systemctl is-active ugreen-led-init.service          && echo "    ugreen-led-init:         运行中" || echo "    ugreen-led-init:         未运行"
systemctl is-active ugreen-probe-leds.service        && echo "    ugreen-probe-leds:       运行中" || echo "    ugreen-probe-leds:       未运行"
systemctl is-active ugreen-power-led.service         && echo "    ugreen-power-led:        运行中" || echo "    ugreen-power-led:        未运行"
systemctl is-active ugreen-diskiomon.service         && echo "    ugreen-diskiomon:        运行中" || echo "    ugreen-diskiomon:        未运行"
systemctl is-active ugreen-netdevmon@enp2s0.service  && echo "    ugreen-netdevmon@enp2s0: 运行中" || echo "    ugreen-netdevmon@enp2s0: 未运行"
echo ""
echo "  管理命令:"
echo "    sudo systemctl restart fnos-panel       # 重启 Web 面板"
echo "    sudo systemctl restart ugreen-led-init  # 重启 LED 驱动"
echo "    sudo systemctl restart ugreen-diskiomon # 重启磁盘 IO 监控"
echo ""

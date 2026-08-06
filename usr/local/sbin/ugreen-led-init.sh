#!/bin/sh
# ugreen-led-init.sh — 加载 UGREEN LED 驱动并创建 I2C 设备(0x3a)。
# 由 systemd 单元 ugreen-led-init.service 调用(ExecStart 指向本文件)。
# 之所以把全部逻辑放进脚本文件、而非写在 unit 的 ExecStart=/bin/sh -c '...' 里，
# 是因为 systemd 对 -c 字符串里 "命令替换 $(...) + 循环" 的解析存在怪异行为：
# 循环体里赋值的 BUS 在末尾 echo 处会被吞成空串，导致 "i2c-/new_device" 报错、
# 冷重启后 LED 不亮。放进独立脚本文件即可彻底规避该解析问题(已实测 BUS=6 正常)。

K=$(uname -r)

# 1) 确保 DKMS 模块可用(极少数情况下 dkms 未自动安装)
if ! /usr/sbin/modprobe -n led-ugreen >/dev/null 2>&1; then
  /usr/sbin/dkms add led-ugreen/0.3 >/dev/null 2>&1 || true
  /usr/sbin/dkms install led-ugreen/0.3 -k "$K" >/dev/null 2>&1 || true
fi

# 2) 幂等: 若 LED class 设备已存在(热重启/fnOS 已加载),直接退出,避免拆掉正常工作的灯。
#    注意必须看 LED class 设备(power/disk1/netdev),而非 i2c 客户端节点(6-003a)——
#    后者在 modprobe -r 卸载驱动后仍会残留,导致误判"已就绪"而不重载驱动。
for l in power disk1 netdev; do
  if [ -e "/sys/class/leds/$l" ]; then
    echo "ugreen-led-init: LED 设备已存在($l),跳过" >&2
    exit 0
  fi
done

# 3) 定位 SMBus I801 适配器(i2c 总线)。
#    关键: 不能依赖"检测 0x3a 设备是否存在"来拿总线号——异常断电冷重启时该设备
#    尚未被本脚本创建,且 ugreen-detect-i2c 在客户端节点不存在时会返回空(鸡生蛋问题),
#    正是它导致服务冷启动失败。改为直接按"适配器名称"找 SMBus I801: 该适配器由
#    内核/udev 独立枚举,与 LED 芯片是否已被探测无关,只要总线就绪即可拿到正确编号。
#    兜底: 若按名称没找到,再用 ugreen-detect-i2c(基于 i2cdetect 扫描物理芯片)。
BUS=""
n=0
while [ "$n" -lt 60 ]; do
  BUS=$(for p in /sys/bus/i2c/devices/i2c-*; do
          nm=$(cat "$p/name" 2>/dev/null)
          case "$nm" in *SMBus*I801*) echo "${p##*/i2c-}"; break ;; esac
        done)
  if [ -n "$BUS" ]; then
    break
  fi
  BUS=$(/usr/bin/ugreen-detect-i2c 2>/dev/null)
  if [ -n "$BUS" ]; then
    break
  fi
  sleep 0.5
  n=$((n + 1))
done

echo "ugreen-led-init: BUS=[$BUS] after $n poll(s)" >&2

if [ -z "$BUS" ]; then
  echo "ugreen-led-init: 未在任何 I2C 总线上找到 SMBus I801 适配器 / LED 控制器" >&2
  exit 1
fi

# 4) 加载驱动并创建 i2c 客户端(0x3a)。
#    modprobe 是异步的: 模块加载并注册 i2c 驱动需要一点时间。若立刻 echo new_device,
#    驱动可能尚未注册导致创建失败。故轮询: 等驱动目录出现后反复尝试 new_device,
#    并以 LED class 设备是否出现作为最终成功判据(最多 ~10s)。
/usr/sbin/modprobe led-ugreen 2>/dev/null || true

ok=0
t=0
while [ "$t" -lt 20 ]; do
  if [ -e /sys/class/leds/power ] || [ -e /sys/class/leds/disk1 ] || [ -e /sys/class/leds/netdev ]; then
    ok=1
    break
  fi
  if [ -d /sys/bus/i2c/drivers/led-ugreen ]; then
    echo led-ugreen 0x3a > "/sys/bus/i2c/devices/i2c-${BUS}/new_device" 2>/dev/null || true
  fi
  sleep 0.5
  t=$((t + 1))
done

if [ "$ok" -eq 1 ]; then
  echo "ugreen-led-init: LED 设备已就绪" >&2
  exit 0
else
  echo "ugreen-led-init: 警告: 超时仍未创建 LED 设备,交给定时重试(Restart=on-failure)" >&2
  exit 1
fi

#!/bin/sh
# ugreen-led-init.sh — 加载 UGREEN LED 驱动并创建 I2C 设备(0x3a)。
# 由 systemd 单元 ugreen-led-init.service 调用(ExecStart 指向本文件)。
# 之所以把全部逻辑放进脚本文件、而非写在 unit 的 ExecStart=/bin/sh -c '...' 里，
# 是因为 systemd 对 -c 字符串里 "命令替换 $(...) + 循环" 的解析存在怪异行为：
# 循环体里赋值的变量在后续 echo 处会被吞成空串，导致 "i2c-/new_device" 报错、
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
    exit 0
  fi
done

# 3) 轮询等待 SMBus I801 适配器(i2c 总线)就绪,解决异常断电冷重启时的开机竞态。
#    冷启动时该总线尚被内核/udev 枚举, ugreen-detect-i2c 会暂时返回空,需等待。
BUS=""
n=0
while [ "$n" -lt 60 ]; do
  BUS=$(/usr/bin/ugreen-detect-i2c 2>/dev/null)
  if [ -n "$BUS" ]; then
    break
  fi
  sleep 0.5
  n=$((n + 1))
done

# 调试信息: 把最终 BUS 写进 journal,便于排障(不影响功能)。
echo "ugreen-led-init: BUS=[$BUS] after $n poll(s)" >&2

if [ -z "$BUS" ]; then
  echo "ugreen-led-init: LED controller not found on any I2C bus" >&2
  exit 1
fi

# 4) 加载驱动并创建 i2c 客户端(0x3a)。若客户端已存在(modprobe -r 后仍残留),
#    new_device 写入会报 EEXIST,用 || true 忽略即可;驱动绑定会重建 LED class 设备。
/usr/sbin/modprobe led-ugreen 2>/dev/null || true
sleep 0.5
echo led-ugreen 0x3a > "/sys/bus/i2c/devices/i2c-${BUS}/new_device" 2>/dev/null || true

exit 0

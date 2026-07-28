## UGREEN DX4600 Pro LED 控制 + 硬件监控面板

适配飞牛 fnOS（Debian 12 bookworm, kernel 6.18.18-trim），零外部依赖。

### 项目结构

```
ugreen-dx4600-pro/
├── fnos_panel.py                       # Web 面板 (Python stdlib, 端口 9092)
├── deploy.sh                           # 一键部署脚本
├── etc/
│   ├── ugreen-leds.conf                # LED 配置文件
│   ├── sudoers.d/
│   │   └── lixin-ugreen                # sudoers 免密规则
│   └── systemd/system/
│       ├── fnos-panel.service          # Web 面板服务
│       ├── ugreen-led-init.service     # LED 驱动加载 + I2C 设备创建
│       ├── ugreen-probe-leds.service   # LED 硬件探测
│       ├── ugreen-power-led.service    # 电源 LED 配置
│       ├── ugreen-diskiomon.service    # 磁盘 IO 监控
│       └── ugreen-netdevmon@.service   # 网络 IO 监控 (模板)
├── usr/bin/
│   ├── ugreen-power-led                # 电源 LED 脚本
│   ├── ugreen-probe-leds               # LED 探测脚本
│   ├── ugreen-diskiomon                # 磁盘 IO 监控脚本
│   ├── ugreen-netdevmon                # 网络 IO 监控脚本
│   ├── ugreen-blink-disk               # 磁盘闪烁程序 (ELF)
│   ├── ugreen-check-standby            # 待机检测程序 (ELF)
│   ├── ugreen-detect-disks             # 磁盘检测脚本
│   └── ugreen-detect-network           # 网络检测脚本
└── dkms/led-ugreen-0.3/
    ├── dkms.conf                       # DKMS 配置
    ├── Makefile                        # 内核模块构建
    ├── led-ugreen.c                    # 驱动源码
    └── led-ugreen.h                    # 驱动头文件
```

### 一键部署

将整个项目目录上传到 NAS 后执行：

```bash
sudo bash deploy.sh
```

脚本自动完成以下步骤：安装系统依赖 (dkms, gcc, smartmontools, i2c-tools, lm-sensors)、编译安装 DKMS 内核模块、部署 LED 控制脚本和配置文件、安装 systemd 服务和 sudoers 规则、启动所有服务。

### 手动部署

如果不使用一键脚本，按以下步骤手动操作。

**1. 安装系统依赖**

```bash
apt-get update
apt-get install -y dkms make gcc linux-headers-$(uname -r) smartmontools i2c-tools lm-sensors
```

**2. 编译安装 DKMS 内核模块**

```bash
cp -r dkms/led-ugreen-0.3 /usr/src/
dkms add led-ugreen/0.3
dkms build led-ugreen/0.3 -k $(uname -r)
dkms install led-ugreen/0.3 -k $(uname -r) --force
modprobe led-ugreen
```

**3. 创建 I2C 设备**

```bash
# 通过自动检测脚本定位总线，避免内核升级后总线号变化导致失败
BUS=$(/usr/bin/ugreen-detect-i2c)
echo "led-ugreen 0x3a" > /sys/bus/i2c/devices/i2c-${BUS}/new_device
```

验证 LED 设备出现：`ls /sys/class/leds/` 应看到 power, netdev, disk1-4。

**4. 安装脚本和配置**

```bash
cp usr/bin/ugreen-* /usr/bin/
chmod +x /usr/bin/ugreen-*
cp etc/ugreen-leds.conf /etc/
cp etc/sudoers.d/lixin-ugreen /etc/sudoers.d/
chmod 440 /etc/sudoers.d/lixin-ugreen
```

**5. 安装 systemd 服务**

```bash
cp etc/systemd/system/ugreen-*.service /etc/systemd/system/
cp etc/systemd/system/fnos-panel.service /etc/systemd/system/
systemctl daemon-reload
```

**6. 启用并启动服务**

```bash
systemctl enable --now ugreen-led-init
systemctl enable --now ugreen-probe-leds
systemctl enable --now ugreen-power-led
systemctl enable --now ugreen-diskiomon
systemctl enable --now ugreen-netdevmon@enp2s0
systemctl enable --now fnos-panel
```

**7. 部署 Web 面板**

```bash
cp fnos_panel.py /home/lixin/
chown lixin:lixin /home/lixin/fnos_panel.py
```

面板启动后访问 `http://<NAS-IP>:9092`。

### 内核升级后重新编译

fnOS 升级 Debian 内核后，DKMS 模块需要为新内核重新编译。两种方式：

**方式一：Web 面板**

打开 `http://<NAS-IP>:9092`，在"驱动编译 (DKMS)"卡片中：先点击"安装内核头文件"（如果头文件未安装），再点击"编译并加载"。

**方式二：命令行**

```bash
apt-get install -y linux-headers-$(uname -r)
dkms build led-ugreen/0.3 -k $(uname -r)
dkms install led-ugreen/0.3 -k $(uname -r) --force
modprobe led-ugreen
BUS=$(/usr/bin/ugreen-detect-i2c)
echo "led-ugreen 0x3a" > /sys/bus/i2c/devices/i2c-${BUS}/new_device
systemctl restart ugreen-led-init ugreen-probe-leds ugreen-power-led ugreen-diskiomon ugreen-netdevmon@enp2s0
```

### 常用管理命令

```bash
# 服务管理
sudo systemctl restart fnos-panel                # 重启 Web 面板
sudo systemctl restart ugreen-led-init           # 重启 LED 驱动
sudo systemctl restart ugreen-diskiomon          # 重启磁盘 IO 监控
sudo systemctl restart ugreen-netdevmon@enp2s0   # 重启网络 IO 监控

# 查看状态
sudo systemctl status fnos-panel
dkms status
lsmod | grep led_ugreen

# 查看 LED 设备
ls /sys/class/leds/
cat /sys/class/leds/power/color
i2c-detect -y 0
```

### 修改 LED 配置

编辑 `/etc/ugreen-leds.conf`，修改颜色、亮度、磁盘序列号等参数后重启相关服务：

```bash
# 编辑配置
nano /etc/ugreen-leds.conf

# 使配置生效 (需要修改 DISK_SERIAL 等映射配置时)
sudo systemctl restart ugreen-diskiomon
sudo systemctl restart ugreen-netdevmon@enp2s0
```

也可以通过 Web 面板在线编辑配置文件和调节 LED 颜色。面板的"保存到配置文件"按钮会将当前 LED 的颜色和亮度同步写入配置文件。

### 注意事项

- `DISK_SERIAL` 中的序列号需要与实际硬盘的物理槽位对应，可通过 `hdparm -I /dev/sdX | grep Serial` 查看
- `ugreen-netdevmon@.service` 是模板服务，`@enp2s0` 对应网卡名，如果网卡名不同需修改启动命令
- `LED_INVERT=1` 表示磁盘活动时 LED 熄灭（反转逻辑），如果希望活动时亮灯改为 `0`
- Web 面板端口 9092，如需修改编辑 `fnos_panel.py` 中的 `PORT` 常量
- DKMS 设置了 `AUTOINSTALL="yes"`，系统升级内核后通常会自动重编译

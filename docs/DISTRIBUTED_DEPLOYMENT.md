# 三端独立部署

每台机器拉取同一仓库、同一提交，各运行一个角色。无需把三端拆成三个仓库；通过 pip extras 和独立虚拟环境选择依赖。录制、OpenPI、Sii 主臂和电机总线实现均已内置，三端无需再拉取其他源码项目。

## 1. 准备机器与安装

推荐 Linux / Python 3.10；本项目使用 Unix 文件锁与信号接口。Linux 若缺少 venv，先安装对应 Python 的 venv 系统包。设备权限、ROS 和驱动由各自机器准备。

```bash
git clone <仓库地址> remote_teleop
cd remote_teleop
bash scripts/install.sh cloud --python python3.10
# master 机器换成 master；slave 机器换成 slave。
```

参数：`--venv /absolute/path` 指定环境；`--policy` 安装 OpenPI 客户端依赖；`--serial` 仅用于 master；`--dev` 安装开发工具。已有虚拟环境会复用，脚本拒绝将普通目录当成虚拟环境。需要安装包源/代理时使用 pip 标准环境变量。安装器不会运行任何角色。

slave 默认安装完整录制依赖。实际请求 OpenPI 的那一端加 `--policy`；Sii 串口主臂所在 master 加 `--serial`：

```bash
bash scripts/install.sh slave                 # 包含录制与视频编码
bash scripts/install.sh cloud --policy       # 仅当 cloud 请求 OpenPI
bash scripts/install.sh master --serial      # 仅当 master 接 Sii / Feetech
# 若策略也在机器人端：
bash scripts/install.sh slave --policy
```

内置实现全部位于 `remote_teleop.components`，不加载外部 `lerobot` 包，不再提供 `--evo-root` 安装参数。旧配置中的 `policy.lerobot_source` 会兼容读取后丢弃，新配置和配置中心不再包含这个字段。首次安装 Torch 等第三方包时，按本机 CPU / GPU 平台选择合适的软件源与构建版本。

## 2. 一次生成配置和凭据

在可信机器的仓库根目录运行一次：

```bash
.venv-cloud/bin/python scripts/prepare_deployment.py \
  --template configs/simulation.json \
  --output state/deployment \
  --hours 24
```

工具不会覆盖已有输出目录。输出含三份配置和三份凭据，目录权限 0700、文件权限 0600。默认配置为模拟；工具不启动进程、不连接硬件。需要硬件时从审阅过的 ROS / 串口模板生成，再按现场接口修改。公共网络的 URL 可用 `--gateway-url wss://gateway.example.com:8800` 初始设置；这不自动生成证书或修改监听地址。

| 机器 | 配置来源 → 目标 | 凭据来源 → 目标 | 凭据角色 |
| --- | --- | --- | --- |
| cloud | `cloud.json` → `configs/local/cloud.json` | `credentials-cloud.json` → `state/credentials-cloud.json` | client + robot，用于验证两端 |
| master | `master.json` → `configs/local/master.json` | `credentials-master.json` → `state/credentials-master.json` | client + ui |
| slave | `slave.json` → `configs/local/slave.json` | `credentials-slave.json` → `state/credentials-slave.json` | robot |

用受控 SSH/SCP 或团队密钥分发方式传输，仅把对应两份文件交给对应机器。目标上先 `mkdir -p configs/local state`，复制后 `chmod 600 state/credentials-*.json`。各端的相对路径都相对于**仓库根目录**。master 不能拿 robot 凭据，slave 不能拿 UI 凭据；云端不需要 UI 登录令牌。

同机验证可把三对文件都放入同一克隆，然后分别启动三个角色。slave 安装已包含模拟遥操、视频及 HIL 录制所需组件。若只验证手动遥操，可在三份配置共同设置 `general.operation_mode=teleop`；不要为测试关闭安全检查。

凭据过期后用新输出目录生成整组并重新分发、重启三端，不能只在某台生成随机新令牌。工具会根据指定模板生成配置；轮换时如要保留三端各自的现场设置，保留现有配置，只替换匹配的凭据文件。

## 3. 设置网络

### 三机 WSS 部署

- 三端 `network.gateway_url` 指向同一个可访问 WSS 网关。
- cloud 设置 `network.gateway_host=0.0.0.0`、`gateway_port=8800`，并填入有效的 `certificate_file` / `private_key_file`。证书必须匹配网关域名；使用私有 CA 时，在连接方设置 `network.ca_file`。
- master 网页默认仅在 `127.0.0.1:8810` 监听。浏览器与 master 在同机时直接访问；远程访问可用 SSH 转发。若将 UI 直接绑定非回环地址，也必须配置证书和私钥。
- cloud 开放配置的 TCP 网关端口；master 和 slave 能访问该端口。WebRTC 的媒体直连还需要可达的 ICE 路径；跨 NAT 时在 `network.ice_servers` 配置 STUN/TURN，用户名和密码使用配置指定的环境变量。

控制通过网关并不保证 WebRTC 媒体一定可达。若部署环境无法提供媒体通路，可在三端配置 `network.webrtc_enabled=false`，使用 JPEG 预览；这时仍需 OpenCV，相机适配器仍由媒体 extra 提供。

### 先用 SSH 隧道验证三机连接

本节用于模拟模式的三机连通验证；真机配置校验要求 WSS，真机仍按上一节配置 TLS。保留三端默认的 `ws://127.0.0.1:8800`，cloud 只监听回环。在 master 和 slave **各自**另开终端保持隧道：

```bash
ssh -N -o ExitOnForwardFailure=yes -L 8800:127.0.0.1:8800 <cloud-ssh-host>
```

这时控制和状态通过 SSH 加密。为使预览不依赖跨机器 WebRTC，可在三份配置中都关闭 `network.webrtc_enabled`。浏览器不在 master 本机时，再建立到 master 的 UI 隧道：

```bash
ssh -N -o ExitOnForwardFailure=yes -L 18810:127.0.0.1:8810 <master-ssh-host>
```

浏览器打开 `http://127.0.0.1:18810`。这类回环 HTTP / WS 仅适用于受控本机或 SSH 隧道；公网使用上面的 TLS 部署。

## 4. 保持三端公共契约一致

不能给每端随意选择不同的模拟 / 真机模板。请从同一配置生成三端，再调整各机路径、接口和监听地址。

公共字段包括 `general.robot_id`、`general.operation_mode`、`general.simulation`、策略位置 / 后端 / ID、相机名称 / 尺寸 / 旋转、关节限位与录制 FPS。实际以 `AppConfig.control_contract` 和握手校验为准。不匹配会拒绝连接。`robot|client|cloud` 分别对应 `slave|master|cloud`。

允许各机使用自己的证书路径、凭据路径、数据目录和监听地址。`robot`、`leader` 整体也进入公共契约，所以 ROS domain、话题、设备映射、主臂标定路径等字段应在三份配置中一致；未使用该设备的机器无需实际存在该路径。主臂配置在 master 上生效；从臂 / 相机 / 录制在 slave 上生效。三端均保留完整配置结构以支持契约校验与配置中心。

## 5. ROS 真机与启动

ROS 主臂所在 master、ROS 从臂所在 slave 需准备 ROS 2、`rclpy`、`sensor_msgs`、`std_msgs`、设备权限和驱动。USB 相机建议安装 `v4l-utils`。ROS domain 和话题需与各自的本地驱动匹配；三端之间无需共享 DDS 网络。

```bash
# ROS Python ABI 必须与安装环境匹配。
export TELEOP_ROS_SETUP=/opt/ros/humble/setup.bash
bash scripts/run.sh slave configs/local/slave.json
```

在各端分别启动，建议先 cloud，再 slave、master：

```bash
bash scripts/run.sh cloud configs/local/cloud.json
bash scripts/run.sh slave configs/local/slave.json
bash scripts/run.sh master configs/local/master.json
```

`run.sh` 解释器优先级：`TELEOP_PYTHON` 绝对路径 → 本角色 `.venv-ROLE/bin/python` → siinuc 旧 Conda 路径。配置优先级：显式参数 → `configs/local/ROLE.json` → 旧 `configs/siinuc/ROLE.json`。因此 siinuc 现有服务无需换环境；新机器通过安装器生成 venv 即可。

在每台机器启动前使用对应解释器执行：

```bash
.venv-slave/bin/python -m remote_teleop validate --config configs/local/slave.json
.venv-slave/bin/python -m remote_teleop doctor --config configs/local/slave.json
```

`validate` 仅校验配置结构，`doctor` 展示依赖与路径，不代表硬件、停止链或外部模型服务已通过验收。真实运动仍需现场确认限位、夹爪单位和停止链；启动服务不会自动使能或回零。现有 systemd 文件是 siinuc 参考模板，复制到新机器前须修改用户、路径、解释器和配置，不能直接原样启用。

## 6. 更新与排障

停止当前任务并让设备进入安全状态，再停止对应进程。三端拉取同一个已验证的提交，然后重新运行各自的安装命令；安装器使用常规安装，修改源码后必须重装包，`run.sh` 也会通过 `PYTHONPATH` 使用当前源码。确认三端版本一致后再重新启动、人工使能。仅在三端都完成验证后恢复作业。

| 现象 | 检查 |
| --- | --- |
| `CREDENTIAL_UNAVAILABLE` / 鉴权失败 | 角色是否正确、两端令牌是否同源、是否过期、文件是否 0600、时钟是否同步 |
| 录制或串口依赖缺失 | 重新执行本机角色的安装命令；确认已选择所需的 `--policy` / `--serial` |
| 视频编码失败 | 检查 PyAV / H.264 编码器、磁盘空间与录制目录权限 |
| 契约不匹配 | 使用同一模板，比较 `AppConfig.control_contract` 的公共字段 |
| ROS 导入失败 | ROS setup、Python ABI、驱动工作区 overlay 和所选解释器是否匹配 |
| 控制在线但视频没有画面 | ICE / TURN / UDP 网络；必要时共同切换 JPEG 预览 |
| 非回环监听被拒绝 | 配置有效证书与私钥，或使用回环监听和 SSH 转发 |

`state/`、`datasets/`、`configs/local/`、`configs/siinuc/` 均被 Git 忽略。更新代码不会自动迁移或轮换这些现场文件。

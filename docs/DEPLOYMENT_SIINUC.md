# siinuc 真机部署记录

部署时间：2026-10-22。全部新版代码、配置、证书和运行数据位于 `/home/siinuc2/Evo-RL/remote_teleop`。旧 `reomte_controller`、OpenEAI 和 ARX 驱动源码未改动，原来正在运行的 ROS 驱动进程继续使用。

## 已部署内容

| 角色 | 配置 | Python 环境 | 入口 |
|---|---|---|---|
| cloud | `configs/siinuc/cloud.json` | `evo-rl` | `wss://127.0.0.1:8800` |
| master | `configs/siinuc/master.json` | `evo-rl` + ROS Humble | `http://127.0.0.1:8810` |
| slave | `configs/siinuc/slave.json` | `remote-controller` + ROS Humble | ROS + WSS 客户端 |

三端 `simulation=false`，默认普通遥操 `operation_mode=teleop`。策略后端为真实 OpenPI，URL 从 `test_hil_dual_arm_fold_shirt.sh` 提取，`runner_location=robot`；正常待机和普通遥操不请求推理。`policy.ca_file` 独立于网关证书，空值使用系统 CA。

用户服务已 link/start，没有配置开机自动启动，也不会自动启动或复位底层驱动。服务异常退出由 systemd 重启，新进程仍从 DISARMED 开始。新脚本无配置参数时读取此处的真机配置；模拟必须显式指定 `configs/simulation.json`。

```bash
systemctl --user status remote-teleop-cloud remote-teleop-slave remote-teleop-master
journalctl --user -u remote-teleop-slave -f
# 需要重启新版平台时，先停止并解除使能，再执行：
systemctl --user restart remote-teleop-cloud remote-teleop-slave remote-teleop-master
# 停止新版，不影响原有 ROS 驱动：
systemctl --user stop remote-teleop-master remote-teleop-slave remote-teleop-cloud
```

## 访问工作台

siinuc 本机访问 `http://127.0.0.1:8810`。从其他电脑访问：

```bash
ssh -N -L 18810:127.0.0.1:8810 siinuc
# 浏览器打开 http://127.0.0.1:18810
```

登录令牌在 siinuc 的 `/home/siinuc2/Evo-RL/remote_teleop/state/siinuc/operator-token.txt`。令牌文件权限 0600，有效期 168 小时；角色凭据在同目录 `credentials.json`。TLS 证书期限 90 天，客户端严格校验；网页 HTTP 仅绑定回环地址，通过 SSH 隧道访问。不要把令牌、私钥放进 Git 或聊天截图。凭据到期时连接会被拒绝，需按 README 的凭据轮换流程生成新文件并更新三端配置。

## 真实设备映射

| 设备 | 状态 | 目标 / 切换 |
|---|---|---|
| 左 / 右 ARX 从臂 | `/{left,right}/ARX_joint_states` | `/{left,right}/ARX_joint_targets` |
| 左 / 右 OpenEAI 主臂 | `/{left,right}/joint_states` | `/{left,right}/joint_targets`；`/master/set_control_mode_{left,right}` |
| 主视角 | USB by-path `usb-0:6:1.0-video-index0` | 640×480，MJPG，旋转 180° |
| 左手 | USB by-path `usb-0:4:1.0-video-index0` | 同上 |
| 右手 | USB by-path `usb-0:5:1.0-video-index0` | 同上 |

ROS domain 0，每臂 joint_1…joint_7；主臂 directions 全 1、offsets 全 0，沿用旧 ROS 模式。命令 QoS 为 RELIABLE、depth 1，修复之前 BEST_EFFORT 无法匹配现有可靠订阅者的问题。状态仍允许 Best Effort。

相机名称按 HIL 脚本配置，已通过真实画面核对。旧 `usb_cam/ros_cam/config/cameras.yaml` 的主视角和左手名称对调，不直接复制它的标签。请求采集 60 FPS，V4L2 报告设备模式约 120.1 FPS；这不是实测帧率。日志记录/策略按配置 60 Hz 采样，首轮相机启动曾出现右手超时，重开后恢复；需要现场长稳验收。

## 当前运动状态及剩余验收

**2026-10-23：按现场用户请求解除临时测试限制，开放可重复的普通手动遥操。** 三端 `safety.hardware_validated=false`，不声明完整验收通过。保留 `attended_test.enabled=true` 的关节相对位移接入方式；夹爪改为直接跟随主臂绝对输入。已关闭：约 3° 相对范围、120 秒时长、截止时间及 15 分钟开始窗口、每进程仅一次使能、固定夹爪、夹爪基线检查、夹爪反馈变化检查、测试附加速度及加速度上限，以及测试模式禁止录制。暂停后可复位、重新使能，无需重启。

普通运动保护继续启用：关节绝对位置范围、速度和加速度保护，以及状态/相机健康、心跳、跟踪误差、超时和手动锁存停止。2026-10-23 按用户要求将当前配置及默认基线的速度/加速度限额翻倍：关节速度 0.25→0.5 rad/s、加速度 0.5→1.0 rad/s²；夹爪速度 0.03→0.06、加速度 0.1→0.2（控制单位）。`safety.checks.velocity_limits` 和 `acceleration_limits` 均为 true。`safety.checks` 提供各运动检查开关；阈值、测试限制、停止确认、模式确认、策略和录制约束均集中在网页“配置中心 → 危险区域 · 安全限制”。可空限制勾选“关闭此限制”即写入 `null`，不需手改 JSON。实体急停、驱动内部保护、鉴权、数据维度和协议有效性检查独立保留。

网页保存会将 general、safety、robot、leader、cameras、policy、recording 共同控制配置同步到三端；network、security、ui 保留各端独立设置，仅更新所选端。保存前读取三端版本、校验三份候选配置和控制契约，逐端带版本写入；失败后尝试回退本次已写入值，不覆盖并发编辑。断连或并发编辑导致回退不完整时明确报错，须恢复连接并重新同步。文件写入完成后，safety 下全部参数进入热更新流程；其他分组仍需重启。

## 恢复默认配置与生效方式

按用户选择，以本机当前部署作为默认基线，而不是切回软件的模拟环境默认值。三端分别保存到 `configs/siinuc/defaults/master.json`、`slave.json`、`cloud.json`；首次建立后，保存配置和重启都不会覆盖基线。基线包含本机设备话题、映射、地址及当前安全设置，其中包括已关闭的临时测试限制。需随部署文件一起备份。

配置中心“恢复默认配置”会显示目标端，经确认后恢复所选端的本地参数，并将基线的共同控制参数同步恢复到三端，同时丢弃表单未保存修改；沿用未使能、录制空闲和配置版本冲突检查。该操作不会自动重启或启动运动。若还需恢复其他端的网络、凭据引用或界面设置，可分别选择对应端恢复。界面显示基线建立时间；三端已保存配置均与当前运行配置一致时不提示重启。

当前后端支持未使能且未录制时热更新全部 `safety` 参数：速度、加速度、检查开关、位置范围、超时、控制频率和现场测试限额。流程为：校验保存候选值与当前运行配置的兼容性 → 带版本同步写入三端 → 各端切换完整 safety 快照 → 读回三端活动参数及控制契约 → 等待机器人遥测确认网关也已识别一致状态。只有全部确认后才返回“已实时生效”；不会自动使能或启动运动。ROS 停止确认历史缓冲窗口随阈值调整；机器人审计记录 safety_hot_update。

热更新失败时显示 CONFIG_APPLY_PENDING，保留已经一致写入的文件。恢复连接后，重新加载并保存即可重试，包括文件内容未改变的情况。鉴权连接使用不含可热更新 safety 的静态契约，以便中断后仍能连上修复；运动指令、使能、开始和录制操作仍由网关检查完整活动契约，任一端不一致即阻止并请求暂停。这项一致性检查独立于可配置的运动检查开关。

默认恢复也走上述热更新流程。网络、设备、ROS 话题、相机、策略、录制、界面等非 safety 字段仍只保存并提示重启；混合修改时安全参数立即生效，其他改动等待重启。若新的 safety 值依赖尚未重启应用的其他配置而无法校验（例如控制频率低于当前录制频率），整次保存会被拒绝，不会留下半套文件。

此前一次网页保存只修改 master 的速度/加速度检查开关，而 slave/cloud 仍开启，重启后被控制契约拒绝。本次修复自动同步，并统一校验模型默认数值类型，避免整数/浮点表示差异误报契约不一致。

现场用户已确认左夹爪 0→0.002 的 2 秒微开试验只使左夹爪张开，其他部分未见运动。原始记录 `state/siinuc/gripper-left-1792682026351059972.json`；物理观察另存 `gripper-left-physical-observation.json`。反馈从 −0.0083923 变为 0.0217438，尚无毫米量测，不能据此宣称单位标定完成。软件保持现在保留最后下发的夹爪指令，不将未知单位的夹爪反馈回发为指令。

现场存在以下已确认差异：

1. 原 ARX 桥只有位置目标接口，没有独立 stop/stopped 接口或可读取的驱动停止状态。此部署使用 `position_hold` 软件保持兼容方式；`stop_confirmed=true` 仅表示实测关节连续静止。它不能替代实体急停，也不能证明进程被杀后的停止时延。
2. OpenEAI 原驱动没有模式确认话题；它的 ctrl_mode 参数不会随话题切换更新。`openeai_graph` 根据实际驱动在拖动模式创建、在位置模式删除 joint_targets 回显发布器的行为确认软件模式，并要求驱动订阅者和新鲜状态。驱动版本更新后必须重新核对这一契约。
3. ARX 闭合夹爪原始反馈左约 -0.00801、右约 -0.00191；当前桥配置 `command_gripper_scale=50.0`、`status_gripper_scale=1.0`。遥操适配层新增 `robot.arms[].gripper_feedback_scale=0.02`、`gripper_feedback_offset=0`，将反馈换算到命令坐标，API 的 q 及新录制数据使用该坐标；未修改底层桥。小于闭合下限的反馈只在初始化命令轨迹时夹到有效命令边界。物理行程、零点及米制精度仍需现场量测，软件换算不代表完成物理标定。
4. 主从关节并非完全对齐；运动验收前需按实测姿态对齐或确定标定偏移。不会自动拖动主臂进行对齐。

完成 HARDWARE_ACCEPTANCE.md 中适用的现场停止、夹爪映射、对齐及低速检查后，记录验收证据，再通过配置中心统一设置 `hardware_validated=true`；须同时关闭 `attended_test.enabled`，保存后实时生效。该字段纳入三端控制契约，不允许仅修改一端后混用。生产发布还需驱动独立看门狗、硬件停止链和长稳测试。

HIL 启用时三端统一将 `general.operation_mode` 改为 `hil`，设置可用的策略 URL 和所需 `policy.runner_location`，保存并重启。开始录制后才能开始 HIL；`i` 请求接管/交还，等待状态确认再操作主臂。不能把普通遥操成功等同于策略推理及 HIL 运动验收。

本次对参考脚本中的策略 URL 只做了 TLS/WebSocket 元数据握手，没有发送图像或推理请求；服务器返回 HTTP 404。HIL 前须更新为当前有效的策略服务 URL。报告在 `state/siinuc/policy-check.json`。普通遥操的关节和视频链路不依赖该策略服务器。

## 验证证据

- 热更新：两个环境各 57 项单元测试通过；三进程验证安全参数立即生效、默认恢复、混合配置仍提示重启、使能时拒绝写入、配置失配时拒绝使能及无需重启恢复；控制器行为测试验证新速度/加速度限额实际生效。完整手动遥操和录制仿真通过。

- 配置中心更新：两个指定环境各 51 项单元测试通过；三进程仿真验证三端同步保存、单端本地设置保留、一键恢复共同默认值、使能时拒绝写入，单元测试覆盖失败回退、提交后回包丢失及并发修改保护。按新限额 0.5/1.0（关节）、0.06/0.2（夹爪）运行两轮普通遥操、夹爪跟随与录制仿真通过；不代替现场运动验收。
- 本次两个指定环境各 41 个单元测试通过，包括取消测试限制、重复使能、自由夹爪、反馈缩放、可配置检查、端点减速和保留的锁存停止。
- 三进程仿真验证两轮大范围手动遥操、左右夹爪跟随、暂停复位后重复使能、实际录制保存与配置 API；旧受限测试模式亦回归通过。
- 三进程模拟受限遥操验证：初始主从姿态不同时无追赶动作，相对位移控制、夹爪固定、超范围停止、禁止重复使能均通过；三路视频正在播放时主端正常退出约 0.21 秒。
- 隔离 ROS domain 218 中，实际 DDS 验证 RELIABLE 目标投递、被动启动、位置保持、OpenEAI 软件模式双向切换及陈旧反馈拒绝；这些测试没有接触 domain 0。
- 独立仿真三进程 HIL 回归通过，280 帧，S0/S1/S2 = 186/93/1，真实 Parquet 与三路视频格式检查通过。
- 真机 API 连续 41 次采样，14 维从臂实测状态、三路 WebRTC、TLS 校验和三端真机配置正常；数据集目录读取 28 个已有数据集。
- `state/siinuc/deployment-check.json`：最新 API 验证报告。`state/siinuc/passive-ros-check.json`：现场 ROS 被动观测报告。
- `docs/siinuc-hardware-preview.png`：真实工作台截图。

只读复验（会短暂登录工作台，勿与正在操作的会话同时运行）：

```bash
cd /home/siinuc2/Evo-RL/remote_teleop
PYTHONPATH=src /home/siinuc2/miniconda3/envs/evo-rl/bin/python scripts/check_siinuc.py
source /opt/ros/humble/setup.bash
export PYTHONPATH="$PWD/src:$PYTHONPATH"
/home/siinuc2/miniconda3/envs/evo-rl/bin/python scripts/check_ros_passive.py
# 隔离 ROS 契约回归：
/home/siinuc2/miniconda3/envs/evo-rl/bin/python tests/ros_contract.py
```

回滚：停止上述三个新版用户服务，确认新进程和相机句柄已退出，再按原流程启动旧版。不要让旧版和新版同时作为运动命令源。原始 ROS 驱动继续运行，回滚不需要重建驱动。

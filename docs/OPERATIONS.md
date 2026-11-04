# 操作与功能参考（siinuc 历史部署）

新机器安装请先阅读 [项目 README](../README.md) 和 [三端部署指南](DISTRIBUTED_DEPLOYMENT.md)。本文包含现场历史路径与验收记录，不能作为新机器的默认配置。

独立项目目录：`/home/siinuc2/Evo-RL/remote_teleop`。旧的 `reomte_controller` 不参与新平台启动，也没有被本项目修改。云端、遥操 master、机器人 slave 是三个独立 Python 进程；前端是随 Python 包分发的 HTML/CSS/JavaScript，无 Node 服务和构建依赖。

**siinuc 已接入真实 ROS 双主臂、双 ARX 从臂和三路 USB 相机。** 现场配置在 `configs/siinuc/`，启动脚本默认读取对应角色的真机配置。普通遥操模式、真实反馈、WSS 网关、双语网页和数据集检查已有验证记录；停止链和夹爪单位尚未完成完整硬件验收。详见 [现场部署说明](DEPLOYMENT_SIINUC.md)。

2026-10-25 更新：主从臂接入配置模块已规范化。检查时上述三个服务均为停止状态，本次部署保留停止状态；现场安全与手动会话设置保持原值，具体运动权限以当前配置和现场部署说明为准。

在 siinuc 本机访问 `http://127.0.0.1:8810`；其他电脑先运行 `ssh -N -L 18810:127.0.0.1:8810 siinuc`，然后访问 `http://127.0.0.1:18810`。登录令牌位于 siinuc 的 `remote_teleop/state/siinuc/operator-token.txt`（0600，168 小时有效期）。

## 快速开始：在 siinuc 上运行模拟系统

实际已有环境名为 `evo-rl`，不是 `evl-rl`。本项目无需修改这两个环境或安装整个父仓库。

```bash
cd /home/siinuc2/Evo-RL/remote_teleop
# 首次生成 0600 的访问凭据；已经存在时拒绝覆盖。
PYTHONPATH=src /home/siinuc2/miniconda3/envs/evo-rl/bin/python -m remote_teleop credentials --config configs/simulation.json

# 分别在三个终端执行，顺序不限：
bash scripts/run.sh cloud configs/simulation.json
bash scripts/run.sh slave configs/simulation.json
bash scripts/run.sh master configs/simulation.json
```

云端和 master 使用 `evo-rl`；slave 使用 `remote-controller`。打开 master 所在机器的 `http://127.0.0.1:8810`。远程访问模拟页面可以使用 `ssh -L 8810:127.0.0.1:8810 siinuc`，再在本机打开该地址。

在受控终端本地查看 `state/credentials.json` 的 `role=ui` 令牌并粘贴到登录框；不要把凭据复制到工单、Git 或日志。默认有效期 24 小时，过期后会断开已建立的会话。生成新文件、分发对应角色凭据后更新配置即可轮换；服务端每条消息及空闲时都会重新检查凭据。

1. 等待三路图像和主臂输入就绪，填写新的数据集名称，点击「开始录制」。首次加载 LeRobot 可能需要数秒，界面会显示初始化状态。
2. 点击「使能」→「开始任务」。HIL 模式先运行策略，主臂跟随从臂的实际关节反馈。
3. 按 `i` 或点击接管按钮，等待界面确认「人工接管」。模拟模式下展开主臂滑块；真实模式下拖拽已进入拖动模式的主臂。
4. 再按 `i` 交还策略。平台重新对齐主从状态、清空上一轮动作队列，收到本次控制世代的新策略动作后才恢复。
5. `s` 成功保存，`f` 失败保存，`a` 确认后丢弃；也可点击对应按钮。文本输入框内不会响应快捷键。每次录制创建一个新的单 Episode 数据集目录，拒绝覆盖或自动追加已有数据。
6. 保存后机器人保持停止。复位返回未使能状态；下一次作业需要重新使能和开始。

关闭页面、登录失效或状态轮询中断超过默认 3 秒，master 不再续约操作员租约；机器人进入安全保持。不要在另一个程序里持续轮询 `/api/status` 来绕过这项机制。一个 UI 实例同一时间只接受一个活跃登录操作员。

## 已实现的能力

- 三种策略位置：`policy.runner_location=robot|client|cloud`。三端使用相同的公共控制契约；不一致的部署会拒绝连接。`policy.backend=openpi` 复用现有 OpenPI MessagePack WebSocket 客户端，支持 `pi05/openpi/lerobot` 请求格式和动作分块队列。
- 机器人端独占控制状态机：未使能、准备、策略、接管准备、人工、恢复准备、安全保持、锁存急停。控制消息绑定 epoch、序号、观察帧 ID 和来源；过期、重复、越界、NaN、错误来源被拒绝。
- 有界队列、最新状态覆盖、独立控制线程与看门狗、速度/加速度限制、关节反馈/相机/主臂/操作员超时、停止确认和录制故障联锁。停止不会产生回零轨迹，重连不会自动恢复运动。
- ROS 双臂、ROS 主臂、3D 打印 Feetech/Sii 主臂适配器；USB/V4L2 与 ROS 图像输入；三路有名称的 WebRTC 视频。关闭 WebRTC 时可用经控制连接传输的低分辨率 JPEG 预览。
- 中英文操作页面；配置按通用、网络、身份安全、运动安全、机器人、主臂、相机、策略、录制和界面分类；支持数组/嵌套配置、搜索、完整 JSON、校验、乐观锁保存、重启提示，以及分别编辑 client/robot/cloud 配置。运动相关设置不热更新。
- 数据集检查子页：允许目录内的列表、HIL schema 对比、FPS/机器人类型/帧数检查、最多 120 行采样曲线、Episode 结果、三相机视频预览。此检查是有界抽样，不声称完整校验全部视频和全部帧。
- 沿用现有 `LeRobotDataset` v3/H.264 writer，HIL 和普通遥操统一字段。相机名称固定 `main/left_hand/right_hand`，关节顺序为左臂 6 关节+gripper、右臂 6 关节+gripper。录制有容量限制、磁盘检查、未完成标记和审计文件。

## 配置与部署

控制面板新增“从臂回零”“遥操臂回零”和取消按钮。ROS 接入的 `homing.reset_topic` 可配置驱动原生 Int32 回零；留空时自动生成限速、限加速度的位置目标序列。回零需要明确点击确认，启动平台不会触发。详见 [机械臂回零说明](HOMING.md)。

主从臂接入已统一为可保存的多方案配置：真机从臂仅使用 ROS（EAI Arm / ARX5）；遥操臂支持 ROS（EAI Arm / ARX5）和 Sii/Feetech 3D 打印臂。配置中心的从臂、遥操臂页面提供内置方案切换及自定义方案管理，可分别配置话题、关节名称、ROS domain、映射和可选的模式切换协议。只编辑方案库不要求重启，实际设备接入变更保存后需重启三端。现场方案保留原有接线。详见 [主从臂接入规范](ARM_ACCESS_CONFIGURATION.md)。

完整默认配置见 [configs/simulation.json](../configs/simulation.json)，ROS 示例见 [configs/hardware.ros.example.json](../configs/hardware.ros.example.json)，3D 打印主臂示例见 [configs/hardware.serial.example.json](../configs/hardware.serial.example.json)。这些硬件示例是配置模板，不代表新增的停止/确认话题已存在。

```bash
PYTHONPATH=src /home/siinuc2/miniconda3/envs/evo-rl/bin/python -m remote_teleop validate --config configs/simulation.json
PYTHONPATH=src /home/siinuc2/miniconda3/envs/remote-controller/bin/python -m remote_teleop doctor --config configs/simulation.json
# 只读检查数据：
PYTHONPATH=src /home/siinuc2/miniconda3/envs/remote-controller/bin/python -m remote_teleop inspect --config configs/simulation.json
```

每个部署角色使用自己的配置文件，如 `configs/cloud.json`、`configs/master.json`、`configs/slave.json`。跨机器时共同字段必须相同，包括 robot_id、operation_mode、simulation、policy runner/id/backend、相机名称/尺寸/旋转、关节限位和录制 FPS；允许各机文件路径、服务监听地址、证书、设备接口不同。切换策略位置须停止作业，更新三端配置，重启后重新使能。

所有相对文件路径相对于项目工作目录；启动脚本和 systemd 模板已固定工作目录。`recording.inspection_roots` 可把 `/home/siinuc2/Evo-RL/data` 等已有数据目录加入只读检查范围。

公共网络必须使用有效 TLS、独立角色凭据和受限网络访问。证书/密钥使用路径引用，策略 API key 和 TURN 密码通过环境变量提供。云端需要验证 client 和 robot 凭据；master 只分发 client 和 ui；slave 只分发 robot。不要把模拟用的完整凭据文件复制到所有生产机器。ROS DDS 的访问控制、网络分段和硬件急停由现场部署负责。

`deploy/remote-teleop@.service` 是尚未安装/启用的 systemd 模板。部署者需先建立可写目录、复制各角色配置、设置设备权限及实际存储路径。使用 ROS 时显式设置 `TELEOP_ROS_SETUP=/opt/ros/humble/setup.bash`；启动脚本随后仍使用指定 Conda 环境的 Python。角色重启后只会进入未使能状态。

## 数据契约与兼容性

| 字段 | 类型/形状 | 含义 |
|---|---|---|
| `observation.state` | float32[14] | 机器人实际关节反馈 |
| `action` | float32[14] | 本平台通过安全限制后交给驱动的目标指令 |
| `complementary_info.policy_action` | float32[14] | 当前原始策略动作；人工帧填零 |
| `complementary_info.is_intervention` | float32[1] | HIL 人工接管=1，其余=0；Parquet 中标量化 |
| `complementary_info.state` | float32[1] | S0=0，S1=1，恢复策略首个录制帧 S2=2 |
| `complementary_info.collector_policy_id` | string[1] | 策略 ID 或 `human` |
| `observation.images.{main,left_hand,right_hand}` | video HWC | 默认 480×640×3、60 FPS、H.264 |

LeRobot 自动字段、任务和 Episode 元数据由复用的 writer 生成。Episode 的 `episode_success` 值为 `success` 或 `failure`。普通遥操沿用相同字段，`state=0`、`is_intervention=0`、`collector_policy_id=human`。选择普通遥操：三端配置 `general.operation_mode=teleop`。

参考脚本的部分路径记录限幅前的 action；这里明确改为**下发目标**，不等同于机械臂已实际到达的位置，真实位置始终在 observation.state。`audit/contract.json` 用 `action_semantics=applied_command_v1` 标注差异；`audit/frames.jsonl` 保存原始请求、下发目标、控制世代、序号、观察帧 ID 和各相机接收时刻。不要在未检查 action 语义的情况下混合历史数据训练。

录制和视频编码不在实时控制线程内。磁盘不足、写入失败、队列溢出、相机超时会停止有效 HIL 作业并保留 `audit/incomplete.json`。不会把故障数据自动标成成功。配置和数据集接口没有删除/上传功能。

## 工程结构与测试

```text
src/remote_teleop/
  config.py, security.py           配置与凭据
  control.py, protocol.py          硬件无关控制域与传输契约
  gateway.py, transport.py         云端与角色连接
  runtime.py, policy.py            主从运行时与统一策略 Runner
  adapters/hardware.py, uvc.py     硬件边界与复用的相机组件
  media.py, recording.py           视频和数据记录
  datasets.py, webapp.py, web/     数据检查、HTTP API、中英文 UI
tests/                            安全回归和显式模拟集成测试
configs/, scripts/, deploy/, docs/
```

```bash
cd /home/siinuc2/Evo-RL/remote_teleop
TELEOP_TEST_PYTHON=/home/siinuc2/miniconda3/envs/evo-rl/bin/python bash scripts/check.sh
TELEOP_TEST_PYTHON=/home/siinuc2/miniconda3/envs/remote-controller/bin/python bash scripts/check.sh
PYTHONPATH=src /home/siinuc2/miniconda3/envs/evo-rl/bin/python tests/integration_smoke.py
PYTHONPATH=src /home/siinuc2/miniconda3/envs/evo-rl/bin/python tests/integration_smoke.py --locations robot --mode teleop
PYTHONPATH=src /home/siinuc2/miniconda3/envs/evo-rl/bin/python tests/policy_contract.py
```

集成测试只使用模拟后端、随机回环端口、临时凭据，在 `state/smoke-*` 下保留配置、日志和数据，并只结束本次创建的子进程。不会打开串口、发布 ROS 控制或访问外部策略。格式检查器 Ruff 和前端 Prettier 仅用于开发，不是运行依赖。

更详细的驱动接入、真实硬件验收、当前边界见 [docs/HARDWARE_ACCEPTANCE.md](HARDWARE_ACCEPTANCE.md)；复用来源与架构见 [docs/ARCHITECTURE.md](ARCHITECTURE.md)；英文使用说明见 [docs/README.en.md](README.en.md)。

> 此文保留现场历史验收记录。当前内置组件与独立安装验证见 [内置组件说明](BUILTIN_COMPONENTS.md) 和 [仓库验证记录](REPOSITORY_VALIDATION.md)。

# 验证记录 · 2026-10-22

验证对象为本目录 Python 重构版本。先完成模拟验收，随后完成 siinuc 真实 ROS/USB 设备接入、左夹爪微开试验及受限普通遥操测试部署。下列记录不构成真实机器人上线认证。

## 现场受限普通遥操追加

用户确认左夹爪微开试验仅左夹爪张开、其他部分未见运动，随后要求直接启动普通遥操。新增一次性有人看护测试配置：120 秒、每关节 ±0.05 rad、速度 ≤0.05 rad/s、加速度 ≤0.1 rad/s²、夹爪固定、禁止录制；保留原始实测状态。完整硬件验收标记仍为 false。停止时保留最后夹爪命令，避免未知反馈单位引起额外开合。

两个环境各 27 项单元测试通过，隔离 ROS domain 218 契约通过；三进程模拟验证相对接入、夹爪固定、超范围停止和不可重复使能。三路视频活跃时正常关闭主端约 0.21 秒，修复原先等待 15 秒后取消视频任务的问题。尚未完成夹爪物理宽度标定、硬件停止和全范围运动验收。

## 真机部署追加验证

2026-10-22 22:59（Asia/Shanghai），三端 `simulation=false`、`DISARMED`、`motion_permitted=false`。40 次 API 采样均收到真实 14 维从臂状态、主臂状态、三路视频；最后一轮最大状态年龄约 11 ms、视频年龄约 47 ms，28 个已有数据集可列出。WSS 证书验证通过。三个 systemd 用户服务运行正常，自动重启次数为 0，原 ROS 驱动进程持续运行。

现场 domain 0 被动订阅 15 秒，左右从臂目标话题和两条主臂模式话题的消息数均为 0。原 OpenEAI 驱动在拖动模式持续回显 joint_targets，这些回显与新增平台发运动命令不是同一回事。ROS Humble Python 回调不提供逐消息发布者信息，观测报告保留实际发布者列表，未伪造来源归属。

新增软件位置保持、OpenEAI 图模式确认、未验收禁止使能的测试后，两环境各 **20** 项单元回归通过。另在隔离 domain 218 使用实际 DDS 和测试驱动完成 RELIABLE 投递、被动启动、位置保持、模式往返和陈旧状态拒绝测试。HIL 仿真重跑 280 帧，S0/S1/S2=186/93/1，WebRTC 与 Parquet 格式通过。

外部策略仅尝试元数据握手，返回 HTTP 404；未发送推理数据。证据见 `siinuc-deployment-check.json`、`siinuc-passive-ros-check.json`、`siinuc-policy-check.json` 及 `siinuc-hardware-preview.png`。部署和未完成验收项见 `DEPLOYMENT_SIINUC.md`。

## 已执行并通过

| 验证 | 结果 |
|---|---|
| Ruff 静态检查与格式检查 | 通过；新 Python 源码和测试 |
| Python compileall | `evo-rl` 与 `remote-controller` 均通过 |
| 单元/契约回归 | 两环境各 17 项通过：状态切换、锁存停止、位置/速度/加速度、超时、来源/epoch/seq/数值、原子配置冲突、凭据撤销、路径约束、队列优先级、串口预装目标及驱动确认 |
| HIL 策略位置 robot | 301 帧：S0=202 / S1=98 / S2=1；WebRTC 三路、保存和检查通过 |
| HIL 策略位置 client | 280 帧：S0=187 / S1=92 / S2=1；WebRTC 三路、保存和检查通过 |
| HIL 策略位置 cloud | 296 帧：S0=197 / S1=98 / S2=1；WebRTC 三路、保存和检查通过 |
| 普通遥操 | 178 帧，全部 S0，人工来源、policy_action 填零，统一 schema 通过 |
| 数据文件 | 实际读取 LeRobot v3 Parquet，检查 14 维字段、60 FPS、三路 H.264 文件、success 结果、JPEG 预览和 incomplete 标记移除 |
| 真正 OpenPI 客户端 + 本机协议夹具 | MessagePack 握手、pi05 图像请求、14 维动作以及动作分块缓存通过；没有访问外部模型 |
| 故障注入 | 运动中的操作员租约过期、gateway 进程退出均进入 SAFE_HOLD，恢复通信不会自动开始运动 |
| API 集成 | 未认证/缺少请求头拒绝；急停无法用 start 绕过；复位、三端配置保存与重启提示、凭据撤销断线通过 |
| 实际浏览器 | 中英文切换；10 类配置字段和校验；数据列表、采样轨迹、Episode 标签和三路预览；i 接管/交还、s 保存通过。另保存一段 2433 帧模拟 UI 作业 |
| 旧源码隔离 | 旧 `reomte_controller` 37 个 Python/C++/JS/HTML/CSS/shell 文件前后 SHA-256 清单逐项相同 |

自动验收原始摘要见 `acceptance-hil.json` 和 `acceptance-teleop.json`；对应远端日志和数据路径在摘要内。历史环境与源码指纹见 `environment-evo-rl.json`、`environment-remote-controller.json`。测试过程生成的凭据不属于交付代码，位于 Git 忽略的 `state/` 内并有时效。

## 验证范围

三进程模拟测试在 siinuc 同机回环连接上执行，使用 `evo-rl` 运行 cloud/master，`remote-controller` 运行 slave。动作安全、驱动确认和串口先预装目标后开力矩的测试使用内存对象，不能代替真实设备测试。数据记录调用实际 v3 writer，视频调用实际 OpenCV/aiortc/AV。

真实 ROS 运动控制链、Sii/Feetech 串口、双臂碰撞/工作空间约束、物理急停、外部策略推理、公网 TLS/证书轮换、NAT/TURN、实地延迟、长稳运行和掉电恢复仍须按 `HARDWARE_ACCEPTANCE.md` 验收。现已安装并启动三个 systemd 用户服务，启用了真实设备监看配置，但未开放运动。

# 内置组件与独立安装

当前包已包含远程遥操平台需要的录制、策略通信、串口主臂及其辅助代码。应用层只导入 `remote_teleop.components`，不需要外部源码仓库。

| 安装入口 | 包含的功能依赖 |
| --- | --- |
| `.[cloud]` | 基础 HTTP / WebSocket 网关 |
| `.[master]` | 基础服务、OpenCV、WebRTC / PyAV |
| `.[slave]` | 媒体、数据检查、完整 writer 依赖 |
| `.[policy]` | OpenPI 客户端的 OpenCV、MessagePack、Torch |
| `.[serial]` | Sii / Feetech 的 pyserial、SDK、DeepDiff、tqdm、draccus |
| `.[recording]` | Torch / Torchvision、Datasets、PyArrow、Pandas、Pillow、Hub、PyAV 等 |

writer 保持现有数据语义：14 维关节反馈与实际下发动作、HIL 标签、三相机、独立 Episode 文件、成功 / 失败结果、审计与故障未完成标记。数据格式仍兼容 LeRobot v3；这是格式名称，不代表需要安装其源码包。

## 目录与资源

```text
remote_teleop/components/
  datasets/              writer、统计、图片队列、视频、数据卡模板
  remote_policy/         OpenPI 客户端、MessagePack、可选监看
  teleoperators/         Sii 主臂及其配置和接口
  motors/                Feetech 总线、编码、控制表
  configs/               数据字段类型
  utils/                 必要的共享工具
  device_types.py
```

组件通过包内命名空间互相引用，数据卡模板也包含在 wheel 中。没有向 `sys.path` 注入外部项目目录，不依赖外部 `lerobot` 导入顺序。缓存默认使用 `~/.cache/remote_teleop`，可用 `TELEOP_CACHE_HOME` 和 `TELEOP_CALIBRATION_HOME` 自定义缓存 / 电机标定目录。

## 旧部署兼容

旧现场 JSON 中即使包含 `policy.lerobot_source`，新版本仍可读取；该字段随后被丢弃，不参与执行，也不会再次保存或展示。配置工具、通用模板与 doctor 已移除源码路径要求。`scripts/configure_siinuc.py` 不再读取父目录中的 HIL 脚本；运行它时显式传入 `--policy-url`，可用 `--inspection-root` 添加只读数据目录。

保留的现场 Conda 回退只是 Python 解释器选择，和包内组件的源码位置无关。更新前停止当前作业，再按原部署方式重启；三端应使用同一提交。

## 验证

```bash
# 独立包全部组件验收所需 extras：
python -m pip install '.[slave,policy,serial]'
python tests/standalone_components_smoke.py
PYTHONPATH=src python tests/policy_contract.py
# 需 dev extra（pytest），模拟总线，不访问设备：
PYTHONPATH=src python -m pytest -q tests/serial_sii_contract.py
PYTHONPATH=src python tests/integration_smoke.py
```

`standalone_components_smoke.py` 强制拒绝任何外部 `lerobot` 导入，验证串口组件可导入与配置、OpenPI 编解码、包内资源，以及 6 帧 / 3 路相机的真实 H.264 编码和保存。串口验证不打开设备，录制使用合成图像。策略合同测试连接本地模拟服务。三进程集成测试使用模拟主从臂、临时凭据和回环端口，覆盖三种策略 Runner 位置及视频 / 录制。

ROS 库、设备驱动、硬件权限以及模型服务仍由部署环境提供；这些是系统服务和第三方依赖，不再要求其他业务源码仓库。

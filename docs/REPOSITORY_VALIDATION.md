# 独立仓库验证（2026-11-03）

## 内置组件版本 0.2.0

已合并 writer、OpenPI 客户端、Sii 主臂与 Feetech 电机总线，应用层不再导入外部源码。旧配置路径字段兼容读取后丢弃。以下验证均在临时目录进行；未修改现场配置、凭据、数据集或重启服务。

- 构建并安装独立 wheel，包内组件、数据卡模板与网页资产完整；安装元数据不依赖外部源码包或 Git URL。
- 在无外部源码、Torch、ROS、OpenCV 或 aiortc 的 cloud 环境安装 0.2.0，健康接口通过。
- 全部组件验收使用已安装的 wheel，并强制拒绝外部 `lerobot` 导入。串口组件导入 / 配置、OpenPI 编解码、数据卡资源、6 帧 / 3 相机 H.264 录制与保存通过。测试复用已有第三方依赖，未引用其他业务源码。
- 三进程 HIL 模拟：robot、client、cloud 三种策略位置均通过，覆盖 WebRTC、接管、恢复、录制、v3 数据检查、急停 / 复位和凭据撤销。所有子进程也禁止外部源码导入。
- OpenPI 合同测试通过：本地 MessagePack 服务、pi05 图像载荷、14 维动作、动作块队列。
- Sii 主臂模拟回归 6 项通过：关节映射、跨半圈连续性、反馈目标、跨整圈目标、参考位姿和重设零点。总线使用 mock，不打开串口。
- 完整 unittest：131 项，129 项通过；仍有下述 2 项原有回零测试失败。新增源码隔离和旧配置兼容测试均通过。
- 修正原三进程模拟测试写死的 60 FPS 断言，改为与实际录制配置比较。

已知原有失败：

```text
test_homing.HomingTests.test_native_reset_matches_both_subscribers_and_exits_twice
test_homing.HomingTests.test_scheduler_stall_does_not_jump_trajectory
```

前者未观察到测试期望的 native reset=1 消息；后者调度停顿时的轨迹步长断言失败。相关控制代码未为本次迁移修改。`scripts/check.sh` 仍会在这些回归失败处退出，不能宣称全部检查或真机回零验收已通过。

各端使用范围依赖而非锁文件；第三方包仍须按平台选择兼容构建。完整部署见 [三端部署指南](DISTRIBUTED_DEPLOYMENT.md)。

# slave：机器人 / 从臂端

1. 拉取本仓库，执行 `bash scripts/install.sh slave`，默认安装完整录制和媒体依赖。
2. 接收 `slave.json` → `configs/local/slave.json`、`credentials-slave.json` → `state/credentials-slave.json`；凭据权限设为 0600。
3. 设置网关地址、本地从臂 ROS 话题、三路相机、录制目录。准备 ROS 2、驱动、设备权限、磁盘空间和编码环境。
4. 导出 `TELEOP_ROS_SETUP=/opt/ros/humble/setup.bash`，运行 `bash scripts/run.sh slave`。

slave 拥有机器人控制状态机和实际目标下发，负责相机、看门狗、安全保持与数据录制。协议称为 robot；若 `policy.runner_location=robot` 且后端为 OpenPI，再增加安装参数 `--policy`。默认安装包含内置 writer、媒体、Parquet 检查和编码依赖。

配置结构通过 `validate` 不代表真实停止链通过验收。完整部署步骤与依赖说明见 [三端部署指南](../DISTRIBUTED_DEPLOYMENT.md)，真机接入见 [硬件验收](../HARDWARE_ACCEPTANCE.md)。

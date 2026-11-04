# master：操作端 / 主臂端

1. 拉取本仓库，执行 `bash scripts/install.sh master`。
2. 接收 `master.json` → `configs/local/master.json`、`credentials-master.json` → `state/credentials-master.json`；凭据权限设为 0600。
3. 设置网关地址与本机主臂接口。ROS 主臂准备本机 ROS / 驱动；Sii 串口主臂安装时加 `--serial` 并设置标定文件。
4. ROS 接入时导出 `TELEOP_ROS_SETUP=/opt/ros/humble/setup.bash`，运行 `bash scripts/run.sh master`。
5. 在本机浏览器访问 `http://127.0.0.1:8810`，以凭据文件中的 ui 令牌登录。

master 安装网页 API、WebRTC / OpenCV，不安装从臂驱动和录制 writer。协议将它称为 client；如果 `policy.runner_location=client`，再增加 `--policy`。主臂设置与真实从臂反馈需完成现场检查后，才能人工使能与开始任务。

完整配置、SSH UI 转发、网络与凭据步骤见 [三端部署指南](../DISTRIBUTED_DEPLOYMENT.md)。

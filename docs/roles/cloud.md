# cloud：云端网关

1. 拉取本仓库，执行 `bash scripts/install.sh cloud`。
2. 从一次性部署包接收 `cloud.json` → `configs/local/cloud.json`、`credentials-cloud.json` → `state/credentials-cloud.json`；凭据权限设为 0600。
3. 设置网关监听、证书与网络。默认回环可搭配 SSH 隧道；公网使用 WSS。
4. 运行 `bash scripts/run.sh cloud`。

cloud 默认只需要基础 Python 网络依赖，不需要 ROS、Torch、相机或录制库。只有当 `policy.runner_location=cloud` 且后端为 OpenPI 时，才增加 `--policy` 并设置策略 URL 和 API key 环境变量。cloud 上的凭据只含 client / robot，用来验证两端。

完整配置与网络步骤见 [三端部署指南](../DISTRIBUTED_DEPLOYMENT.md)。

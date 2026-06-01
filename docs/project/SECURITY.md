# Security Policy

## Supported Scope

当前仅对本仓库公开代码与文档进行维护。

## Sensitive Data Rules

请不要在仓库中提交以下信息：

- QMT 账号与密码
- 钉钉 Webhook / Secret
- 数据库账号密码
- Git Token / API Key
- 本地绝对路径中的敏感信息
- 实盘导出的订单或成交明细（如含敏感账户信息）

## Reporting a Vulnerability

如果发现以下类型问题，建议优先报告并暂不公开提交：

- 可导致凭据泄露的问题
- 可导致错误下单/重复下单的问题
- 重连导致状态错乱的问题
- 撤单/成交回调映射错误
- 持仓恢复导致仓位不一致

## Open-Source Use Reminder

本项目包含交易相关逻辑，但不应默认视为可直接实盘使用。

在真实环境运行前，请自行完成：

1. 接口版本核验
2. 权限核验
3. 风控规则核验
4. 长时稳定性测试
5. 模拟盘与回放验证

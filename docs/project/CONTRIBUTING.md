# Contributing

欢迎对 cytrade 提交改进。

## 提交前建议

1. 先阅读 [README.md](README.md)
2. 尽量保持变更聚焦，不做无关重构
3. 不要提交真实账号、密码、令牌、Webhook 或本地路径

## 开发约定

- Python 代码保持现有风格
- 优先补充或更新测试
- 新增策略建议放在 `strategy/` 下
- 改动交易链路时，至少回归以下模块：
  - `tests/test_trading.py`
  - `tests/test_order_manager.py`
  - `tests/test_position.py`
  - `tests/test_main.py`

## 本地验证

```bash
python -m pytest tests/ -v
```

## 变更建议范围

适合直接贡献：

- 文档改进
- 测试补充
- Web 展示优化
- 监控与日志增强
- 非破坏性性能优化

需要更谨慎评审：

- QMT 回调逻辑
- 持仓成本算法
- 订单状态流转
- 重连与重订阅链路
- 状态持久化格式变更

## 提交说明建议

提交信息建议采用：

- `feat: ...`
- `fix: ...`
- `test: ...`
- `docs: ...`
- `chore: ...`

示例：

- `fix: repair reconnect callback registration`
- `docs: improve open-source README`
- `test: add web cancel route regression`

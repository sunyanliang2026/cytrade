# QMT 原生 Python 只读行情探针

券商迁移手册要求新建 Python 策略时勾选“原生Python”，再依次点击“编译”“运行”。本探针用于确认该入口能读取快照和订阅 Level2，不连接交易账户、不下单、不撤单。

## 脚本

- [qmt_native_smoke.py](../qmt_native_smoke.py)：最小入口测试，仅记录 `SCRIPT_START` 和 `SCRIPT_OK`。
- [qmt_internal_probe.py](../qmt_internal_probe.py)：完整行情测试。通过 `if __name__ == "__main__"` 启动，直接使用 `xtdata`，不依赖公式模型的 `init(ContextInfo)`。

完整探针默认观察 `000001.SZ` 的 `tick`、`l2quote`、`l2order`、`l2transaction`、`l2orderqueue`。运行 30 秒后自动取消订阅并退出。只修改脚本顶部的 `STOCK_CODE` 即可更换标的。

## QMT 操作

1. 在“模型研究”中新建 Python 策略，确认勾选“原生Python”。不要把脚本放在“模型交易”的实盘策略中运行。
2. 先运行最小探针，确认出现 `SCRIPT_START`、`SCRIPT_OK`。
3. 再载入完整探针，点“编译”“运行”。预期先出现 `SCRIPT_START`、`IMPORT xtdata=ok`、`SNAPSHOT`，然后出现五类 `SUBSCRIBE` 结果；运行结束时出现 `SUMMARY`、`UNSUBSCRIBE`、`STOP_END`。
4. 交易时段若数据确实到达，每类会记录一次 `CALLBACK_FIRST`。`SUBSCRIBE ok` 仅表示接口接受订阅，不能证明已收到 Level2 数据。盘后没有回调不代表订阅不可用。

日志优先写到 QMT 用户数据目录的 `log/qmt_probe_YYYYMMDD.log`；如果该目录不可用，则尝试 `%TEMP%/qmt_probe_YYYYMMDD.log`。若 `IMPORT`、`SNAPSHOT` 或订阅失败，保留探针及 QMT 客户端日志以区分入口、权限、行情服务和时段问题。

## 本地验证

```powershell
python -m pytest strategies/large_order_limit_up_buy/tests/test_qmt_internal_probe.py strategies/large_order_limit_up_buy/tests/test_qmt_native_smoke.py
python -m py_compile strategies/large_order_limit_up_buy/qmt_internal_probe.py
```

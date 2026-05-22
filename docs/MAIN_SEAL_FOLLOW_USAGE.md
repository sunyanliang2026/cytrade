# 主封单跟随策略使用说明

## 股票池

默认股票池文件：

```text
config/main_seal_follow_pool.csv
```

CSV 表头：

```csv
股票代码,名称,计划买入金额
001259,利仁科技,10000
600604,市北高新,10000
```

- 股票代码：支持 `001259`、`001259.SZ`、`600604`、`600604.SH`，策略内部会归一成 6 位代码。
- 名称：只用于日志和诊断，不参与交易判断。
- 计划买入金额：每只股票本次计划买入的总金额，支持纯数字，也支持 `2万` 这种写法。
- 金额小于等于 0 的行会被跳过，可用于保留样例但不启用。

## 本地配置

`config/local_runtime.json` 已配置：

```json
"CYTRADE_MAIN_SEAL_FOLLOW_CSV_PATH": "C:\\Users\\ysun\\workspace\\cytrade\\config\\main_seal_follow_pool.csv",
"CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN": true
```

- `CYTRADE_MAIN_SEAL_FOLLOW_CSV_PATH`：股票池路径。
- `CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN`：是否只模拟下单。`true` 只打日志不真实委托；`false` 才会走真实交易执行器。

## 启用方式

可以直接运行专用入口：

```powershell
python run_main_seal_follow.py
```

该入口只注册 `MainSealFollowStrategy`。等价代码如下：

```python
from main import run_scheduler_service
from strategy.main_seal_follow_strategy import MainSealFollowStrategy

run_scheduler_service(strategy_classes=[MainSealFollowStrategy])
```

启动后，系统会在选股阶段读取股票池，每一行有效股票生成一个独立策略实例。

Level2 订阅采用两层模式：

- 全股票池默认只订阅 `l2quote`，用于轻量扫描是否接近涨停。
- 当某只股票的最新价或买一价进入 `quote_trigger_near_limit_ticks` 范围内，才动态打开 `l2transaction`、`l2order`、`l2orderqueue`。
- 已经打开详细 Level2 的股票，本轮运行中会保持订阅，避免频繁订阅/退订造成抖动。

## 安全检查

首次使用建议保持：

```json
"CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN": true
```

验收标准：

- 日志里能看到股票池中正金额股票被加载。
- 日志里出现 `[DRY_RUN]`，说明只模拟下单。
- Level2 校准目录继续输出该股票的行情诊断文件。

确认信号、队列、撤单指标都符合预期后，再把 `CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN` 改为 `false` 做真实委托。

## 相关文档

- `docs/MAIN_SEAL_FOLLOW_DESIGN.md`
- `docs/MAIN_SEAL_FOLLOW_TRIGGER_PARAMS.md`

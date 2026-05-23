# 股票池生成逻辑

本文记录当前 MainSealFollow 策略使用的股票池生成方案。

## 目标

股票池只负责确定“今天允许策略监控哪些股票”和“每只股票计划买入金额”。策略是否排板、是否下单，仍由盘中 Level2 行情和排板指标决定。

输出文件固定为：

```text
config/main_seal_follow_pool.csv
```

CSV 字段固定为：

```text
股票代码,名称,计划买入金额
```

## 当前默认来源：问财 pywencai

默认使用 `scripts/collect_main_seal_pool.py --source iwencai` 调用 `pywencai` 生成股票池。

默认问财查询语句：

```text
涨停，实际流通市值大于19亿,30日最大振幅小于50%，非st，主板
```

含义：

- `涨停`：最近收盘涨停。
- `实际流通市值大于19亿`：使用问财口径的实际流通市值，不使用 QMT 的 `FloatVolume * close` 近似值。
- `30日最大振幅小于50%`：过滤最近 30 日波动过大的股票。
- `非st`：排除 ST、*ST、退市等风险标的。
- `主板`：只保留主板股票。

主板代码过滤在程序里还会再做一次，当前允许前缀：

```text
000 / 001 / 002 / 003 / 600 / 601 / 603 / 605
```

问财结果进入 CSV 前会做：

- 识别股票代码列和名称列。
- 股票代码归一化为 6 位代码，例如 `001259.SZ` 写成 `001259`。
- 主板过滤。
- 非 ST 名称过滤。
- 去重。
- 按实际流通市值从大到小排序。
- 写入统一计划买入金额，默认 `1000`。

## Cookie 配置

问财来源需要登录后的 cookie。读取优先级：

```text
--iwencai-cookie > 环境变量 IWENCAI_COOKIE > config/local_runtime.json
```

本机已使用 `config/local_runtime.json` 保存：

```json
{
  "IWENCAI_COOKIE": "..."
}
```

`config/local_runtime.json` 已被 `.gitignore` 忽略，不能提交到 Git。

## 日常生成命令

立即生成一次：

```powershell
python scripts\collect_main_seal_pool.py --source iwencai --once --amount 1000
```

定时生成，例如每天 08:45：

```powershell
python scripts\collect_main_seal_pool.py --source iwencai --schedule-time 08:45 --amount 1000
```

覆盖正式股票池前默认会备份旧文件：

```text
config/main_seal_follow_pool.backup_YYYYMMDD_HHMMSS.csv
```

## 备用来源一：QMT 本地近似

命令：

```powershell
python scripts\collect_main_seal_pool.py --source qmt --once --amount 1000
```

QMT 来源使用本地日线和证券资料筛选：

- 最近收盘涨停。
- 主板代码。
- 非 ST。
- 30 日最大振幅小于阈值。
- 本地流通市值大于阈值。

限制：

QMT 的流通市值当前口径是 `FloatVolume * 最近收盘价`，不是问财的“实际流通市值/自由流通市值”。例如沪电股份问财实际流通市值约 `1499.44 亿`，QMT 近似值曾计算为约 `2193.40 亿`，口径不一致。因此 QMT 来源只作为离线 fallback，不作为默认口径。

## 备用来源二：韭研公社文章

命令：

```powershell
python scripts\collect_main_seal_pool.py --source jiuyangongshe --once --amount 1000
```

用途：

从韭研公社盘前文章中提取热点、公告、涨停事件涉及的股票，再通过 QMT 名称映射成代码，写入同一个股票池 CSV。

当前关注节点：

- `No.1 盘前热点事件`
- `No.2 公告精选 -> 一、日常公告`
- `No.4 连板梯队和涨停事件 -> 三、涨停事件`

限制：

这是事件驱动股票池，不包含“最近收盘涨停、实际流通市值、30 日振幅”等问财量化条件。除非明确要做盘前热点池，否则默认不使用它替代问财来源。

## 策略侧如何使用股票池

策略启动时读取 `CYTRADE_MAIN_SEAL_FOLLOW_CSV_PATH`，默认指向：

```text
config/main_seal_follow_pool.csv
```

每一行有效股票生成一个独立策略实例：

- `计划买入金额 <= 0` 的行跳过。
- 每只股票初始只做轻量行情监控。
- 当前排板逻辑只关注“买一已到涨停价”后的排板机会，不做远离涨停时的扫板。
- 股票池只决定监控范围，不代表启动后立即下单。

## 当前验证记录

最近一次使用问财 cookie 真实验证：

- 查询语句：`涨停，实际流通市值大于19亿,30日最大振幅小于50%，非st，主板`
- 返回股票数：`52`
- 输出文件：`config/main_seal_follow_pool.csv`
- 沪电股份实际流通市值解析值：`149943676160`，约 `1499.44 亿`
- 相关测试：`tests/test_collect_main_seal_pool.py`、`tests/test_import_iwencai_pool.py`
- 测试结果：`10 passed`

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

## 当前结构：来源脚本 + 总入口汇总

当前股票池生成分为两层：

- 来源脚本：只负责从单一来源生成候选池，便于单独调试。
- 总入口：负责汇总多个来源，再统一做代码归一化、去重、主板过滤、非 ST 过滤、金额写入。

当前来源脚本：

```text
scripts/pool/collect_iwencai_pool.py
scripts/pool/collect_jiuyangongshe_pool.py
```

当前总入口：

```text
scripts/pool/collect_main_seal_pool.py
```

总入口默认来源为 `combined`，会先按类型调用问财，再叠加韭研公社，最后统一处理后写入正式 CSV。

`combined` 默认是容错模式：某个非核心来源失败时会打印 `WARNING` 并继续汇总其他来源，避免因为韭研公社名称转代码依赖 QMT 不可用而导致整个问财股票池生成失败。如果要求任一来源失败就退出，使用：

```powershell
python scripts\pool\collect_main_seal_pool.py --source combined --strict-sources --once
```

## 来源一：问财 pywencai

问财来源使用 `pywencai` 生成候选股票。

股票池来源参数默认从统一 JSON 文件读取：

```text
config/main_seal_pool_sources.json
```

当前主配置采用“命名结果集 + 集合表达式”：

- `sets`：定义每个可引用的结果集。问财查询和韭研公社节点都在这里定义。
- `final`：定义最终股票池如何由这些结果集组合出来。
- `union`：并集。
- `intersect`：交集，支持任意两个或多个结果集。

示例：

```json
{
  "sets": {
    "iwencai.limitup_direct": {
      "source": "iwencai",
      "query": "涨停，实际流通市值大于19亿,30日最大振幅小于50%，非st，主板"
    },
    "iwencai.base_strong": {
      "source": "iwencai",
      "query": "收盘价大于30日最高价的95%，收盘价小于60日最低价的150%，主板非st，实际换手率大于6%，实际流通市值大于20亿"
    },
    "jiuyangongshe.hot_events": {
      "source": "jiuyangongshe",
      "node": "hot_events"
    }
  },
  "final": {
    "union": [
      "iwencai.limitup_direct",
      {
        "intersect": [
          "jiuyangongshe.hot_events",
          "iwencai.base_strong"
        ]
      }
    ]
  }
}
```

这个模型可以表达旧的三类语义：

- `direct`：直接把某个结果集放进 `final.union`。
- `base`：作为 `intersect` 的准入集合。
- `gated`：候选结果集与 `base` 取交集后再进入 `final.union`。

其中 `iwencai.queries` 的每条条件包含 `name/type/query`。`type` 分三类：

- `base`：初步筛选池，只作为准入门槛，不直接进入最终股票池。
- `direct`：一次性结果筛选池，命中后直接进入最终股票池。
- `gated`：候选筛选池，结果必须同时命中 `base`，才进入最终股票池。

最终关系：

```text
最终股票池 = direct + (gated ∩ base) + (韭研公社 ∩ base)
```

当前默认配置：

```json
{
  "iwencai": {
    "queries": [
      {
        "name": "最近收盘涨停低波动池",
        "type": "direct",
        "query": "涨停，实际流通市值大于19亿,30日最大振幅小于50%，非st，主板"
      },
      {
        "name": "强势基础准入池",
        "type": "base",
        "query": "收盘价大于30日最高价的90%，收盘价小于60日最低价的135%，主板非st，实际换手率大于4%小于25%，实际流通市值大于20亿小于1000亿，最近30日单日最高成交额，几天几板"
      }
    ]
  },
  "jiuyangongshe": {
    "enabled": true,
    "user_url": "https://www.jiuyangongshe.com/u/4df747be1bf143a998171ef03559b517",
    "require_today": true
  }
}
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

单独调试问财来源：

```powershell
python scripts\pool\collect_iwencai_pool.py
```

默认输出：

```text
data/iwencai_pool_candidates.csv
```

该候选文件会记录每条问财条件的 `类型/查询名称/条件/股票代码/股票名称`，用于检查每类条件的命中情况。

问财结果进入总入口前会解析：

- 识别股票代码列和名称列。
- 股票代码归一化为 6 位代码，例如 `001259.SZ` 写成 `001259`。
- 实际流通市值。
- 30 日最大振幅。

最终是否进入正式股票池，由总入口统一过滤和去重。

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

总入口立即生成一次：

```powershell
python scripts\pool\collect_main_seal_pool.py --source combined --once --amount 50000
```

定时生成，例如每天 08:45：

```powershell
python scripts\pool\collect_main_seal_pool.py --source combined --schedule-time 08:45 --amount 50000
```

只使用问财，不叠加其他来源：

```powershell
python scripts\pool\collect_main_seal_pool.py --source iwencai --once --amount 50000
```

使用 `combined` 但临时不叠加韭研公社：

```powershell
python scripts\pool\collect_main_seal_pool.py --source combined --no-jiuyangongshe --once --amount 50000
```

严格模式，任一来源失败就退出：

```powershell
python scripts\pool\collect_main_seal_pool.py --source combined --strict-sources --once --amount 50000
```

覆盖正式股票池前默认会备份旧文件：

```text
config/main_seal_follow_pool.backup_YYYYMMDD_HHMMSS.csv
```

## 备用来源一：QMT 本地近似

命令：

```powershell
python scripts\pool\collect_main_seal_pool.py --source qmt --once --amount 50000
```

QMT 来源使用本地日线和证券资料筛选：

- 最近收盘涨停。
- 主板代码。
- 非 ST。
- 30 日最大振幅小于阈值。
- 本地流通市值大于阈值。

限制：

QMT 的流通市值当前口径是 `FloatVolume * 最近收盘价`，不是问财的“实际流通市值/自由流通市值”。例如沪电股份问财实际流通市值约 `1499.44 亿`，QMT 近似值曾计算为约 `2193.40 亿`，口径不一致。因此 QMT 来源只作为离线 fallback，不作为默认口径。

## 来源二：韭研公社文章

命令：

```powershell
python scripts\pool\collect_jiuyangongshe_pool.py
```

默认输出：

```text
data/jiuyangongshe_pool_candidates.csv
```

只使用韭研公社写正式股票池：

```powershell
python scripts\pool\collect_main_seal_pool.py --source jiuyangongshe --once --amount 50000
```

用途：

从韭研公社盘前文章中提取热点、公告、涨停事件涉及的股票，再通过 QMT 名称映射成代码，写入同一个股票池 CSV。

自动取最新文章时默认启用日期保护：

- `require_today=true` 时，用户页最新文章的日期必须等于当天。
- 如果最新文章不是当天文章，韭研公社来源失败；`combined` 默认会打印 `WARNING` 并继续汇总其他来源。
- 手工传 `--article-url` 时用于历史文章测试，不做“最新文章必须当天”的保护。

当前关注节点：

- `No.1 盘前热点事件`
- `No.2 公告精选 -> 一、日常公告`
- `No.4 连板梯队和涨停事件 -> 三、涨停事件`

限制：

这是事件驱动股票池，不包含“最近收盘涨停、实际流通市值、30 日振幅”等问财量化条件。除非明确要做盘前热点池，否则默认不使用它替代问财来源。

## 统一处理规则

所有来源候选进入总入口后，统一执行：

- 股票代码归一化为 6 位代码。
- 主板代码过滤：`000/001/002/003/600/601/603/605`。
- 非 ST 名称过滤。
- 按首次出现顺序去重。
- 写入统一计划买入金额，默认 `50000`。

这个阶段是最终裁决阶段。来源脚本可以尽量保留原始候选，避免各来源重复实现同一套过滤逻辑。

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

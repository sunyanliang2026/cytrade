# 早盘竞价主力态度策略实施方案 V1.0

> 适用系统：cytrade / miniQMT / QMT / xtquant
> 当前阶段：数据验证 + 干跑观测
> 实盘状态：不启用自动实盘交易
> 核心窗口：09:24:50—09:25:00 竞价最后 10 秒，09:30:00—09:35:00 开盘验证窗口

---

## 1. 项目背景

本策略围绕 A 股早盘集合竞价最后阶段的资金态度展开，重点观察 **09:24:50—09:25:00** 这最后 10 秒内，是否存在明显的大资金抢筹行为。

策略核心不是简单追高开，也不是单纯看涨幅榜，而是要回答三个问题：

1. 最后 10 秒有没有快速抬价？
2. 抬价背后有没有大单成交或大单委托支撑？
3. 09:30 开盘后，市场是否继续承认这次竞价抢筹？

因此，本策略分为两个主要阶段：

```text
09:24:50—09:25:00
    识别竞价最后 10 秒是否存在抢筹行为

09:30:00—09:35:00
    验证是真抢筹、洗盘后拉升，还是假抢筹
```

---

## 2. 策略总目标

建立一套基于 miniQMT / QMT / xtquant 的早盘动态选股与验证系统。

系统目标：

```text
全市场 L1 扫描
    -> 竞价最后 10 秒涨速筛选
    -> Level2 大单成交 / 大单委托验证
    -> 生成今日观察池
    -> 09:30 后判断真假抢筹
    -> 干跑记录理论交易
    -> 后续再接入真实执行
```

V1 阶段只做观测、打标、干跑，不直接实盘下单。

---

## 3. 核心策略思想

本策略的核心判断句：

```text
09:24:50—09:25:00 看主力是否真实抢筹；
09:30 之后看市场是否承认这次抢筹。
```

如果竞价末段资金明显抢筹，开盘后继续拉升，则视为真抢筹。

如果竞价末段资金明显抢筹，开盘后先下砸，但下方承接强，随后重新拉回开盘价并突破早盘高点，则视为洗盘后拉升。

如果竞价末段价格被拉高，但开盘后直接砸盘、无承接、无修复，则视为假抢筹，当日放弃。

---

## 4. 整体系统架构

策略系统拆成三个核心模块：

```text
AuctionSpeedScanner
    负责全市场 L1 扫描，生成竞价涨速候选池

OpeningAuctionL2Probe
    负责验证 09:24:50—09:25:00 的 Level2 数据可得性

OpeningAuctionAttitudeStrategy
    负责结合 L1 + Level2 结果，判断真假抢筹并生成干跑交易记录
```

与现有系统的关系：

```text
AuctionSpeedScanner
    -> 生成今日观察池

OpeningAuctionAttitudeStrategy
    -> 判断早盘竞价抢筹真假

MainSealFollowStrategy
    -> 如果标的快速冲击涨停或封板，则交给现有主封跟随逻辑处理
```

---

## 5. 股票池设计

本策略不使用固定小股票池，而是动态生成今日观察池。

股票池分为四层：

```text
基础候选池
    -> L1 竞价预观察池
        -> Level2 最后 10 秒采样池
            -> 今日开盘验证池
```

### 5.1 基础候选池

基础候选池在盘前或 09:20 前准备。

初始范围：

```text
A 股全市场
```

基础过滤条件：

```text
剔除 ST / *ST
剔除停牌
剔除退市整理
剔除极低流动性股票
剔除昨日成交额过低股票
剔除价格过低或异常票
```

V1 建议条件：

```text
昨日成交额 >= 1 亿
昨日收盘价 >= 2 元
非 ST
非停牌
非退市整理
```

### 5.2 L1 竞价预观察池

时间窗口：

```text
09:24:30—09:24:45
```

使用全市场 L1 全推行情快速扫描。

筛选依据：

```text
当前竞价高开
09:24:30 后价格有抬升
匹配金额增加
流动性充足
最终高开区间合理
```

该池数量控制在：

```text
30—80 只
```

目的是在 09:24:50 前提前订阅 Level2，避免错过竞价最后 10 秒的逐笔数据。

### 5.3 Level2 最后 10 秒采样池

时间窗口：

```text
09:24:50—09:25:00
```

采集对象：

```text
L1 预观察池中的股票
```

采集内容：

```text
l2quote
l2order
l2transaction
l2quoteaux，若可用
```

### 5.4 今日开盘验证池

时间窗口：

```text
09:25:00—09:25:10
```

生成依据：

```text
最后 10 秒涨速
最后 10 秒匹配金额变化
Level2 大单成交
Level2 大单委托
竞价最终高开幅度
是否存在诱多风险
```

输出数量：

```text
重点观察池 Top 20
普通观察池 Top 50
最多保留 Top 100 做轻量跟踪
```

---

## 6. 全市场 L1 扫描方案

使用 miniQMT / xtquant 的全推行情能力：

```python
xtdata.subscribe_whole_quote(['SH', 'SZ'], callback=on_data)
```

全推行情用于解决“全市场范围内快速找异动票”的问题。

### 6.1 回调处理原则

全推行情一次可能推送几千只股票，回调函数不能做重计算。

错误方式：

```python
def on_data(datas):
    for stock, tick in datas.items():
        save_to_database(stock, tick)
        calculate_signal(stock, tick)
```

正确方式：

```python
def on_data(datas):
    data_queue.put(datas)
```

后台线程异步消费：

```python
def consumer():
    while True:
        datas = data_queue.get()
        update_latest_tick_map(datas)
```

### 6.2 全推 tick 数据使用方式

全推行情回调格式为：

```python
{
    "600000.SH": tick,
    "000001.SZ": tick
}
```

注意它不是单股订阅的：

```python
{
    "600000.SH": [tick1, tick2, ...]
}
```

因此处理逻辑要单独写，不能直接复用单股订阅处理代码。

### 6.3 L1 快照采样点

需要记录以下时间点的全市场快照：

```text
09:24:30
09:24:40
09:24:45
09:24:50
09:24:55
09:24:58
09:25:00
```

核心使用点：

```text
09:24:30—09:24:45
    生成 Level2 预观察池

09:24:50—09:25:00
    计算最后 10 秒竞价涨速
```

---

## 7. 竞价最后 10 秒涨速模型

核心指标：

```text
price_2450
price_2455
price_2458
price_2500

pre_close
auction_lift_pct_10s
auction_speed_per_sec
auction_speed_per_min
final_gap_pct
```

### 7.1 最后 10 秒涨幅

```text
auction_lift_pct_10s =
    (price_2500 - price_2450) / pre_close
```

### 7.2 每秒涨速

```text
auction_speed_per_sec =
    auction_lift_pct_10s / 10
```

### 7.3 等效每分钟涨速

```text
auction_speed_per_min =
    auction_lift_pct_10s * 6
```

### 7.4 最终竞价高开幅度

```text
final_gap_pct =
    (price_2500 - pre_close) / pre_close
```

---

## 8. 竞价涨速入选规则

### 8.1 必须满足条件

```text
price_2500 > price_2450
auction_lift_pct_10s >= 0.30%
final_gap_pct >= 1.50%
matched_amount_delta_10s > 0
price_2500 接近 09:24:50—09:25:00 区间高点
```

### 8.2 强观察条件

满足以下条件越多，优先级越高：

```text
auction_lift_pct_10s >= 0.50%
最后 10 秒匹配金额明显增加
final_gap_pct 在 3%—9% 之间
price_2500 接近最后 10 秒最高价
最后 3 秒没有明显回落
```

### 8.3 剔除条件

```text
最后 10 秒价格上移，但匹配金额没有增加
09:25 最终价明显低于 09:24:58 高点
最后 1 秒孤立尖刺拉高
final_gap_pct 过低，没有攻击性
final_gap_pct 过高，但成交金额不足
昨日流动性太差
```

---

## 9. Level2 数据可得性验证

这是 V1 实施前最关键的实验。

核心问题：

```text
09:25—09:30 之间，能不能拿到 09:24:50—09:25:00 的 Level2 数据？
```

策略原则：

```text
09:25—09:30 可以作为竞价 Level2 数据的计算窗口，
但不能作为竞价 Level2 数据的补采窗口。
```

也就是说：

```text
如果要分析 09:24:50—09:25:00 的大单成交和大单委托，
必须在 09:24:50 之前完成 Level2 订阅，
并且最好在回调里实时落盘。
```

### 9.1 实验 A：提前订阅组

流程：

```text
09:24:40
    从 L1 全推行情中选出预观察池

09:24:45
    对预观察池提前订阅 Level2

09:24:50—09:25:00
    接收并原样落盘 Level2 数据

09:25:05
    统计是否存在 event_time 在 09:24:50—09:25:00 的数据
```

判断标准：

```text
如果能看到 09:24:50—09:25:00 的 l2order / l2transaction / l2quote，
说明提前订阅后，可以在 09:25—09:30 处理竞价最后 10 秒 Level2 数据。
```

### 9.2 实验 B：延后订阅组

流程：

```text
09:25:05
    才对同一批股票订阅 Level2

09:25:05—09:29:59
    检查是否能获取 event_time < 09:25:00 的数据
```

判断标准：

```text
如果没有数据，说明 09:25 后订阅无法补回竞价最后 10 秒 Level2。

如果有少量数据，也要检查完整性，不能直接认为可靠。
```

### 9.3 策略采用原则

V1 正式策略只采用提前订阅路径。

延后订阅路径只作为实验观察，不作为策略依赖。

---

## 10. Level2 最后 10 秒采集内容

优先采集：

```text
l2quote
l2order
l2transaction
l2quoteaux
```

如果资源有限，优先级为：

```text
1. l2quote
2. l2order
3. l2transaction
4. l2quoteaux
```

### 10.1 l2quote

用途：

```text
观察竞价价格、盘口状态、累计量、买卖盘口变化
```

### 10.2 l2order

用途：

```text
观察最后 10 秒是否有大额买入委托新增
观察最后 10 秒卖单是否反压
观察大单申报方向
```

核心字段：

```text
time
price
volume
entrustNo
entrustType
entrustDirection
```

方向解释先按以下方式记录：

```text
entrustDirection = 1
    买入委托

entrustDirection = 2
    卖出委托

entrustDirection = 3 / 4
    上交所撤买 / 撤卖，具体需实测确认
```

### 10.3 l2transaction

用途：

```text
观察最后 10 秒是否存在大单成交
观察成交方向
观察买卖不平衡
```

核心字段：

```text
time
price
volume
amount
tradeIndex
buyNo
sellNo
tradeType
tradeFlag
```

成交方向先按以下方式记录：

```text
tradeFlag = 1
    可能为主动买 / 外盘

tradeFlag = 2
    可能为主动卖 / 内盘

tradeFlag = 3
    可能为撤单事件，深市需特殊处理
```

注意：

```text
集合竞价最后 10 秒是否有连续竞价式成交，必须实测。
不能假设 l2transaction 一定有完整逐笔成交。
```

---

## 11. 大单成交与大单委托模型

### 11.1 大单阈值

V1 不固定单一阈值，而是同时统计多个阈值：

```text
10 万
30 万
50 万
100 万
300 万
```

原因：

```text
不同市值、不同价格、不同流动性的股票，大单标准不同。
一开始固定 200 万或 300 万，可能漏掉中小盘有效资金行为。
```

### 11.2 大单成交指标

```text
big_trade_amount_10s_10w
big_trade_amount_10s_30w
big_trade_amount_10s_50w
big_trade_amount_10s_100w
big_trade_amount_10s_300w

big_trade_count_10s
max_trade_amount_10s
big_buy_amount_10s
big_sell_amount_10s
big_trade_imbalance_10s
big_buy_ratio_10s
```

计算：

```text
big_trade_imbalance_10s =
    big_buy_amount_10s - big_sell_amount_10s
```

```text
big_buy_ratio_10s =
    big_buy_amount_10s /
    max(big_buy_amount_10s + big_sell_amount_10s, 1)
```

### 11.3 大单委托指标

```text
big_buy_order_add_amount_10s
big_sell_order_add_amount_10s
big_order_add_imbalance_10s
big_buy_order_count_10s
big_sell_order_count_10s
max_buy_order_amount_10s
max_sell_order_amount_10s
```

计算：

```text
big_order_add_imbalance_10s =
    big_buy_order_add_amount_10s - big_sell_order_add_amount_10s
```

---

## 12. 竞价态度标签

结合 L1 涨速和 Level2 大单行为，生成竞价标签。

### 12.1 标签定义

```text
AUCTION_SPEED_ONLY
    只有最后 10 秒涨速，没有 Level2 大单确认

AUCTION_BIG_TRADE_CONFIRMED
    最后 10 秒涨速 + 大单成交确认

AUCTION_BIG_ORDER_CONFIRMED
    最后 10 秒涨速 + 大单委托确认

AUCTION_STRONG_CONFIRMED
    最后 10 秒涨速 + 大单成交 + 大单委托同时确认

AUCTION_FAKE_RISK
    价格被拉高，但大单成交 / 大单委托不支持

NO_SIGNAL
    无有效竞价信号
```

### 12.2 强确认条件

```text
auction_lift_pct_10s >= 0.30%
matched_amount_delta_10s > 0
big_buy_ratio_10s >= 60%
big_trade_imbalance_10s > 0
big_order_add_imbalance_10s > 0
price_2500 接近最后 10 秒高点
最后 1 秒不是孤立尖刺
```

满足以上大部分条件，标记为：

```text
AUCTION_STRONG_CONFIRMED
```

### 12.3 诱多风险条件

```text
最后 10 秒价格明显上移
但匹配金额没有明显增加

或者：

最后 10 秒价格明显上移
但 Level2 大单成交很少

或者：

最后 10 秒价格明显上移
但大单卖出 / 卖委托明显占优

或者：

最后 1 秒孤立尖刺拉高，前 9 秒没有连续抬升
```

标记为：

```text
AUCTION_FAKE_RISK
```

---

## 13. 竞价评分模型

### 13.1 AuctionSpeedScore

用于决定是否进入今日观察池。

总分 100：

```text
最后 10 秒涨幅              35 分
最后 10 秒金额增量          25 分
最终高开幅度                15 分
走势连续性                  15 分
接近涨停强度                10 分
```

标签：

```text
score >= 75
    SPEED_STRONG

60 <= score < 75
    SPEED_MEDIUM

45 <= score < 60
    SPEED_WEAK

score < 45
    DROP
```

今日观察池只保留：

```text
SPEED_STRONG
SPEED_MEDIUM 中排名靠前的标的
```

### 13.2 AuctionAttitudeScore

用于判断主力态度强弱。

总分 100：

```text
最后 10 秒价格上移              25 分
匹配金额明显增加                20 分
Level2 大单成交买入占优         25 分
Level2 大单委托买入占优         20 分
板块 / 题材同步强                10 分
```

扣分项：

```text
价格拉升但金额不增加             -25 分
最后 1 秒尖刺拉高                -20 分
大单卖出明显占优                 -30 分
卖委托明显反压                   -20 分
高开过大但买盘不足               -20 分
```

标签：

```text
score >= 80
    AUCTION_STRONG_CONFIRMED

65 <= score < 80
    AUCTION_MEDIUM_CONFIRMED

50 <= score < 65
    AUCTION_WEAK

score < 50
    NO_SIGNAL
```

---

## 14. 09:30 后开盘验证

09:25 生成的信号不能直接等同于买点。

09:30 后必须验证。

验证窗口：

```text
09:30:00—09:35:00
```

输出路径：

```text
DIRECT_PULL
WASH_THEN_PULL
FAKE_BREAKDOWN
NO_FOLLOW_THROUGH
```

---

## 15. 路径 A：真抢筹，开盘直接拉升

### 15.1 识别条件

```text
开盘后 5—45 秒内价格不快速跌破开盘价
主动买成交持续占优
价格快速突破开盘价上方
回落幅度小
接近涨停或快速冲击日内高点
```

### 15.2 状态流转

```text
AUCTION_STRONG_CONFIRMED
    -> OPEN_VERIFY
    -> DIRECT_PULL
    -> ENTRY_PENDING
```

### 15.3 执行原则

V1 干跑阶段只记录理论买点。

未来实盘执行时，不在第一笔冲高时盲追，而是等待：

```text
第一次回踩不破开盘价 / VWAP

或者：

接近涨停后封单确认
```

若快速冲击涨停，可转交给现有 MainSealFollowStrategy 处理。

---

## 16. 路径 B：真抢筹，先洗盘后拉升

### 16.1 识别条件

```text
竞价信号强
开盘后先下砸
下砸幅度受控
下砸过程中有明显承接
低点后主动买重新增强
重新站回开盘价或 VWAP
再突破早盘第一波反弹高点
```

### 16.2 状态流转

```text
AUCTION_STRONG_CONFIRMED / AUCTION_MEDIUM_CONFIRMED
    -> OPEN_VERIFY
    -> OPEN_DIP
    -> SUPPORT_TEST
    -> SUPPORT_CONFIRMED
    -> ENTRY_PENDING
```

### 16.3 V1 理论买点

V1 固定买点：

```text
收复开盘价后，不立即买；
等再次突破开盘后第一波反弹高点时，记录理论买点。
```

原因：

```text
过滤假反抽。
```

---

## 17. 路径 C：假抢筹，开盘直接砸

### 17.1 识别条件

```text
竞价阶段价格被拉高
开盘后快速跌破开盘价
主动卖成交持续占优
反抽无法重新站回开盘价
下方买盘承接弱
竞价涨幅快速回吐
```

### 17.2 状态流转

```text
AUCTION_SPEED_ONLY / AUCTION_FAKE_RISK
    -> OPEN_VERIFY
    -> FAKE_BREAKDOWN
    -> EXITED
```

### 17.3 固定规则

```text
一旦标记为 FAKE_BREAKDOWN，当日不再参与这只票。
```

---

## 18. 策略状态机

完整状态机：

```text
PREPARE_POOL
    -> L1_MARKET_SCAN
    -> L2_PREWATCH_BUILD
    -> L2_PRE_SUBSCRIBED
    -> AUCTION_10S_CAPTURE
    -> AUCTION_CLASSIFIED
    -> WAIT_OPEN
    -> OPEN_VERIFY
        -> DIRECT_PULL
        -> OPEN_DIP
        -> SUPPORT_TEST
        -> SUPPORT_CONFIRMED
        -> FAKE_BREAKDOWN
        -> NO_FOLLOW_THROUGH
    -> ENTRY_PENDING
    -> HAS_POSITION
    -> EXITED
```

状态说明：

```text
PREPARE_POOL
    盘前准备基础候选池

L1_MARKET_SCAN
    使用全市场 L1 全推行情扫描异动

L2_PREWATCH_BUILD
    在 09:24:30—09:24:45 生成 Level2 预观察池

L2_PRE_SUBSCRIBED
    在 09:24:50 前完成 Level2 订阅

AUCTION_10S_CAPTURE
    原样记录 09:24:50—09:25:00 的 L1 + Level2 数据

AUCTION_CLASSIFIED
    在 09:25—09:30 计算竞价标签

WAIT_OPEN
    等待 09:30 开盘

OPEN_VERIFY
    验证是真抢筹还是假抢筹

DIRECT_PULL
    开盘直接拉升

OPEN_DIP
    开盘先下砸

SUPPORT_TEST
    检查下砸承接

SUPPORT_CONFIRMED
    承接确认

FAKE_BREAKDOWN
    假抢筹，放弃

NO_FOLLOW_THROUGH
    有竞价信号，但开盘后无延续

ENTRY_PENDING
    理论买点出现

HAS_POSITION
    后续实盘阶段使用

EXITED
    当日结束
```

---

## 19. 代码模块设计

### 19.1 新增 AuctionSpeedScanner

文件：

```text
strategy/auction_speed_scanner.py
```

职责：

```text
维护全市场 L1 最新 tick
生成 L1 预观察池
记录 09:24:50—09:25:00 快照
计算 auction_speed_score
输出今日观察池候选
```

核心类：

```python
class AuctionSpeedScanner:
    def on_whole_quote(self, datas):
        pass

    def update_latest_ticks(self, datas):
        pass

    def build_l2_prewatch_pool(self, now):
        pass

    def capture_snapshot(self, label):
        pass

    def calculate_speed_scores(self):
        pass

    def build_watch_pool(self):
        pass
```

### 19.2 新增 OpeningAuctionL2Probe

文件：

```text
scripts/probe/probe_opening_auction_l2.py
```

职责：

```text
验证 09:25—09:30 是否能处理竞价最后 10 秒 Level2 数据
验证提前订阅和延后订阅的差异
输出原始数据和字段结构
```

核心输出：

```text
data/probe/opening_l2_raw_YYYYMMDD.jsonl
data/probe/opening_l2_summary_YYYYMMDD.csv
data/probe/opening_l2_schema_YYYYMMDD.json
```

### 19.3 新增 OpeningAuctionAttitudeStrategy

文件：

```text
strategy/opening_auction_attitude_strategy.py
```

职责：

```text
读取今日观察池
结合 L1 涨速和 Level2 大单行为生成竞价标签
09:30 后验证真假抢筹
记录理论买点和理论结果
暂不实盘下单
```

核心类：

```python
class OpeningAuctionAttitudeStrategy(BaseStrategy):
    def required_data_kinds(cls):
        pass

    def on_tick(self, tick):
        pass

    def on_l2_quote(self, data):
        pass

    def on_l2_order(self, data):
        pass

    def on_l2_transaction(self, data):
        pass

    def classify_auction(self):
        pass

    def verify_open_behavior(self):
        pass

    def emit_dry_run_signal(self):
        pass
```

### 19.4 新增数据模型

文件：

```text
strategy/opening_auction_models.py
```

核心结构：

```python
@dataclass
class AuctionSnapshot:
    symbol: str
    time_label: str
    price: float
    pre_close: float
    matched_amount: float
    volume: float
    raw: dict
```

```python
@dataclass
class AuctionL2Summary:
    symbol: str
    transaction_count: int
    order_count: int
    big_buy_amount: float
    big_sell_amount: float
    big_trade_imbalance: float
    big_buy_order_amount: float
    big_sell_order_amount: float
    big_order_imbalance: float
```

```python
@dataclass
class AuctionDecision:
    symbol: str
    auction_speed_score: float
    auction_attitude_score: float
    auction_label: str
    watch_rank: int
    reason: str
```

---

## 20. 数据输出设计

### 20.1 今日观察池 CSV

路径：

```text
data/runtime/auction_watch_pool_YYYYMMDD.csv
```

字段：

```text
date
symbol
name
pre_close
price_2450
price_2455
price_2458
price_2500
auction_lift_pct_10s
auction_speed_per_min
final_gap_pct
matched_amount_2450
matched_amount_2500
matched_amount_delta_10s
auction_speed_score
auction_attitude_score
auction_label
speed_label
rank
```

### 20.2 Level2 原始数据 JSONL

路径：

```text
data/probe/opening_l2_raw_YYYYMMDD.jsonl
```

格式：

```json
{
  "recv_time": "09:24:58.123",
  "event_time": "09:24:58.080",
  "symbol": "000001.SZ",
  "kind": "l2transaction",
  "raw": {}
}
```

### 20.3 Level2 汇总 CSV

路径：

```text
data/probe/opening_l2_summary_YYYYMMDD.csv
```

字段：

```text
date
symbol
l2_subscribe_mode
has_l2_2450_2500
l2_quote_count_10s
l2_order_count_10s
l2_transaction_count_10s
big_trade_amount_10w
big_trade_amount_30w
big_trade_amount_50w
big_trade_amount_100w
big_trade_amount_300w
big_buy_amount_10s
big_sell_amount_10s
big_trade_imbalance_10s
big_buy_order_amount_10s
big_sell_order_amount_10s
big_order_imbalance_10s
schema_seen
```

### 20.4 策略事件日志

事件名：

```text
MSF_AUCTION_ATTITUDE
```

字段：

```json
{
  "date": "YYYY-MM-DD",
  "symbol": "000001.SZ",
  "name": "",

  "auction_price_2450": 0,
  "auction_price_2455": 0,
  "auction_price_2458": 0,
  "auction_price_2500": 0,
  "auction_lift_pct_10s": 0,

  "matched_amount_2450": 0,
  "matched_amount_2500": 0,
  "matched_amount_delta_10s": 0,

  "l2_transaction_count_10s": 0,
  "l2_order_count_10s": 0,

  "big_buy_amount_10s": 0,
  "big_sell_amount_10s": 0,
  "big_trade_imbalance_10s": 0,

  "big_buy_order_amount_10s": 0,
  "big_sell_order_amount_10s": 0,
  "big_order_imbalance_10s": 0,

  "auction_speed_score": 0,
  "auction_attitude_score": 0,
  "auction_label": "",

  "open_price": 0,
  "open_verify_path": "",
  "entry_signal_time": "",
  "entry_reason": "",

  "fake_breakdown": false,
  "support_confirmed": false,
  "final_result": ""
}
```

---

## 21. 开发阶段规划

### 阶段 1：Level2 探针验证

目标：

```text
确认 09:24:50—09:25:00 的 Level2 数据是否能被提前订阅采集
确认 09:25 后延迟订阅是否能补回竞价数据
确认 l2order / l2transaction / l2quote 在集合竞价最后 10 秒的真实字段形态
```

产出：

```text
probe_opening_auction_l2.py
opening_l2_raw_YYYYMMDD.jsonl
opening_l2_summary_YYYYMMDD.csv
opening_l2_schema_YYYYMMDD.json
```

验收标准：

```text
至少采集 1—3 个交易日
确认哪些 Level2 数据在 09:24:50—09:25:00 有效
确认提前订阅是否必要
确认字段是否稳定
```

### 阶段 2：L1 全市场竞价涨速扫描器

目标：

```text
实现 AuctionSpeedScanner
完成全市场 L1 全推行情接入
实现 09:24:50—09:25:00 涨速计算
生成今日观察池
```

产出：

```text
auction_speed_scanner.py
auction_watch_pool_YYYYMMDD.csv
```

验收标准：

```text
09:25 后能稳定生成 Top 20 / Top 50 今日观察池
涨速排名与实际盘口表现基本一致
回调无明显卡顿
```

### 阶段 3：L1 + Level2 竞价态度打分

目标：

```text
融合 L1 涨速和 Level2 大单行为
生成 auction_attitude_score
生成 auction_label
识别 AUCTION_STRONG_CONFIRMED / AUCTION_FAKE_RISK
```

产出：

```text
opening_auction_models.py
auction_attitude_score 计算函数
auction_label 分类函数
```

验收标准：

```text
对每只观察票输出清晰标签
能解释每个分数来源
能保留原始证据
```

### 阶段 4：09:30 后真假验证

目标：

```text
实现 DIRECT_PULL
实现 WASH_THEN_PULL
实现 FAKE_BREAKDOWN
实现 NO_FOLLOW_THROUGH
```

产出：

```text
opening_auction_attitude_strategy.py
MSF_AUCTION_ATTITUDE 日志
理论买点记录
理论结果记录
```

验收标准：

```text
每只观察票 09:35 前完成路径分类
假抢筹能被快速剔除
真抢筹能被稳定打标
```

### 阶段 5：干跑复盘与参数校准

目标：

```text
连续运行 5—20 个交易日
统计不同标签的成功率
统计不同大单阈值的有效性
统计 DIRECT_PULL / WASH_THEN_PULL / FAKE_BREAKDOWN 的分布
```

产出：

```text
daily_opening_auction_review_YYYYMMDD.md
auction_strategy_stats.csv
参数建议报告
```

验收标准：

```text
明确哪些信号有正向价值
明确哪些条件是假抢筹高发
确定大单阈值和观察池规模
```

### 阶段 6：小仓位实盘前准备

V1 不直接进入该阶段。

进入条件：

```text
干跑数据足够
Level2 数据稳定
假抢筹过滤有效
理论买点有统计优势
风控规则明确
```

---

## 22. 风控边界

### 22.1 V1 不实盘

固定规则：

```text
V1 只做 dry_run
不自动下真实订单
不自动关闭 dry_run
不自动修改账户配置
不自动修改 QMT 路径
不自动修改资金参数
```

### 22.2 单票风控，未来阶段使用

```text
单票最大仓位：策略资金 10%—20%
首次观察单：策略资金 5%—10%
```

### 22.3 买入后止损，未来阶段使用

直接拉升路径：

```text
跌破开盘价且无法快速收复，退出
```

洗盘后拉升路径：

```text
跌破承接确认低点，退出
```

### 22.4 时间止损，未来阶段使用

```text
买入后 3—5 分钟没有继续走强，减仓或退出
```

### 22.5 当日禁入

以下情况当日不再参与：

```text
FAKE_BREAKDOWN
竞价强但开盘连续卖压
跌破竞价关键价后无修复
第一次承接失败
```

---

## 23. 关键技术风险

### 23.1 9:25 后不能补采 Level2

风险：

```text
如果 09:25 后才订阅 Level2，可能无法拿到 09:24:50—09:25:00 的逐笔数据。
```

应对：

```text
必须提前生成 Level2 预观察池
必须在 09:24:50 前完成 Level2 订阅
必须实时落盘
```

### 23.2 全市场 L1 回调压力大

风险：

```text
全推行情一次可能包含几千只股票，回调里做重计算会卡死。
```

应对：

```text
回调只入队
后台线程处理
减少 print
批量写入
只在关键时间点快照
```

### 23.3 集合竞价阶段 Level2 字段不确定

风险：

```text
l2transaction 在集合竞价最后 10 秒可能没有连续竞价式成交。
tradeFlag / entrustDirection 在集合竞价阶段的含义可能需要实测确认。
```

应对：

```text
先做 OpeningAuctionL2Probe
原样记录 raw 数据
不提前写死解释逻辑
用 1—3 个交易日样本确认字段
```

### 23.4 大单阈值不适配所有股票

风险：

```text
固定 100 万或 300 万可能适合大票，不适合中小票。
```

应对：

```text
同时统计 10 万 / 30 万 / 50 万 / 100 万 / 300 万
后续按成交额、市值、股价分层校准
```

---

## 24. V1 验收指标

### 24.1 数据层验收

```text
能稳定接收全市场 L1 全推行情
能在 09:25 后生成今日观察池
能确认 Level2 提前订阅数据是否覆盖 09:24:50—09:25:00
能输出完整 raw / summary / schema 文件
```

### 24.2 策略层验收

```text
每只观察票都有 auction_speed_score
每只观察票都有 auction_attitude_score
每只观察票都有 auction_label
09:35 前能给出 open_verify_path
```

### 24.3 复盘层验收

```text
能区分 DIRECT_PULL
能区分 WASH_THEN_PULL
能区分 FAKE_BREAKDOWN
能输出理论买点
能统计理论收益和失败原因
```

---

## 25. 最终实施顺序

推荐开发顺序：

```text
1. 实现 OpeningAuctionL2Probe
2. 跑 1—3 个交易日，确认 Level2 数据形态
3. 实现 AuctionSpeedScanner
4. 生成 09:25 今日观察池
5. 实现 L1 + Level2 打分模型
6. 实现 OpeningAuctionAttitudeStrategy
7. 干跑 5—20 个交易日
8. 根据复盘结果校准参数
9. 再讨论是否接入真实交易执行
```

---

## 26. V1 固定结论

本策略 V1 的核心不是直接交易，而是建立一套可靠的早盘竞价数据采集与验证系统。

最终固定原则：

```text
先用 L1 全市场扫描找到最后 10 秒竞价异动票；
再用提前订阅的 Level2 数据确认是否有大单成交 / 大单委托；
最后用 09:30 后走势验证是真抢筹、洗盘后拉升，还是假抢筹。
```

V1 不追求马上盈利，优先追求：

```text
数据真实
字段可靠
标签稳定
复盘可解释
风险可控
```

只有当干跑数据证明该信号具备统计价值后，才进入小仓位实盘验证阶段。

---

## 27. 资料来源说明

本文档结合了当前 cytrade 项目结构、策略讨论内容，以及 miniQMT 全市场全推行情资料整理。上传资料中关于 `subscribe_whole_quote(['SH', 'SZ'])`、全推行情回调 `{stock: tick}` 格式、以及回调轻处理/队列异步处理的建议，已吸收到本方案的 L1 扫描模块设计中。

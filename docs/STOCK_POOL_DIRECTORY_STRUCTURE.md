# 股票池目录结构

股票池运行产物和策略股票池配置不再放在公共 `config/` 下。需要股票池的策略在自己的策略目录下维护配置和默认池文件。

## 目录

- `strategies/main_seal_follow/config/main_seal_pool_sources.json`
  - MainSealFollow 的股票池来源配置。

- `strategies/main_seal_follow/data/main_seal_follow_pool.csv`
  - MainSealFollow 自动选股生成的当前最终股票池。
  - 策略自动流程默认读取这个文件。

- `strategies/main_seal_follow/data/main_seal_follow_manual_pool.csv`
  - MainSealFollow 手工测试股票池。

- `strategies/opening_auction_attitude/config/opening_auction_pool_sources.json`
  - OpeningAuctionAttitude 的股票池来源配置。

- `strategies/opening_auction_attitude/data/opening_auction_universe.csv`
  - OpeningAuctionAttitude 竞价扫描大池。
  - 临时指定一只或几只股票时使用。

- `data/stock_pools/runs/YYYY-MM-DD/HHMMSS/`
  - 每次生成股票池的留痕目录。
  - `sources/` 保存每个来源或节点的原始候选结果。
  - `merge/raw_before_unified_filter.csv` 保存统一过滤、去重前的候选。
  - `merge/final_pool.csv` 保存本次最终结果快照。
  - `manifest.json` 保存本次运行参数、来源文件路径、来源数量、最终数量。

- `data/stock_pools/archive/config_legacy/`
  - 从旧 `config/` 目录迁移出来的历史股票池和 backup。
  - 只用于追溯，不作为策略默认输入。

`data/stock_pools/` 是运行产物目录，默认不提交到 Git。

## 重新生成股票池

```powershell
cd C:\Users\ysun\workspace\cytrade
C:\Users\ysun\miniconda3\envs\cytrade311\python.exe -m scripts.pool.collect_main_seal_pool --once --source combined --amount 50000
```

生成后：

- 当前最终池：`strategies/main_seal_follow/data/main_seal_follow_pool.csv`
- 当次留痕：`data/stock_pools/runs/YYYY-MM-DD/HHMMSS/`

## 手工测试股票池

手工池文件：

```text
strategies/main_seal_follow/data/main_seal_follow_manual_pool.csv
```

手工启动脚本会读取这个文件，不会重新生成股票池。

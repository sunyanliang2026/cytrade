# 股票池目录结构

股票池运行产物不再放在 `config/` 下。`config/` 只保留配置文件，例如来源配置 `main_seal_pool_sources.json`。

## 目录

- `data/stock_pools/current/main_seal_follow_pool.csv`
  - 自动选股生成的当前最终股票池。
  - 策略自动流程默认读取这个文件。

- `data/stock_pools/manual/main_seal_follow_manual_pool.csv`
  - 手工测试股票池。
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

- 当前最终池：`data/stock_pools/current/main_seal_follow_pool.csv`
- 当次留痕：`data/stock_pools/runs/YYYY-MM-DD/HHMMSS/`

## 手工测试股票池

手工池文件：

```text
data/stock_pools/manual/main_seal_follow_manual_pool.csv
```

手工启动脚本会读取这个文件，不会重新生成股票池。

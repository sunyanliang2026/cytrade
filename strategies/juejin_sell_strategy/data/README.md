# Data

Strategy-owned input data for `JuejinSellStrategy`.

- `sell_10.csv`: default Juejin sell list. Columns: `symbol`, `exp`, `sellvol`, `nick`.

Runtime callers can override the CSV path with `StrategyConfig.params["csv_path"]`.

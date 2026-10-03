# czsc 桥接约束

本项目在 Python 3.12.13、czsc **1.0.1** 上验证；这是测试版本，不是“当前最新版”声明。依赖暂限定 `>=1.0.1,<2`，范围不等于对每个版本都已验证。算法来源见 [czsc 项目](https://github.com/waditu/czsc)，本地桥接见 [core/chan.py](../core/chan.py)。

## 输入与输出

- `format_standard_kline` 需要 `symbol,dt,open,close,high,low,vol,amount`；项目用 `volume * close` 近似 amount，并未拿到真实逐 bar 成交额。
- czsc 不替调用者排序。桥接层先按时间排序、去重并排除缺失 OHLC。
- 默认保留笔数可能截断结构，桥接按输入规模提高 `max_bi_num`。
- `FX.mark`、`BI.direction` 是枚举，使用 `.name`；不要直接与 `"G"` 等字符串比较。
- 1.0.1 桥接暴露分型、笔、中枢，没有承诺旧文档中的 `xd_list` 线段 API。
- 图形下标按时间映射到输入；不能用 czsc 内部裁剪后的 `bars_raw` 下标直接索引全量行情。

## 可观察事件

`build` 用于某个给定历史截面的几何。几何端点不等于产生该结构的观察时刻，不能从完整历史的下一笔 `edt` 推算当时已经知道的成交时刻。

`iter_structures` 用同一个 CZSC 对象逐 bar `update`，每步只暴露当前结构。`core/causal_signals.py` 在此上记录首次出现与撤销，不用最终几何删除已观察到的入场。流式结构的数组索引固定为未定义值，调用者只能使用时间锚。

修改桥接前阅读模块 docstring。依赖升级后至少验证桥接、真实前缀、共享策略合同与 HTML 输出；测试见 `tests/test_chan.py`、`tests/test_triple_buy.py`、`tests/test_chan_viz.py`，版本快照见 [VALIDATION](VALIDATION.md)。

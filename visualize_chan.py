"""生成缠论标注 K 线图（单文件 HTML，离线可看，不需要实时刷新）。

    python visualize_chan.py sh688981
    python visualize_chan.py 600519 --period 30 --out outputs/chan_600519.html

图层：中枢 / 笔 / 分型 / 一~三买卖点 / 日线一买二买生效日 / 策略买卖点 /
日线 MA5·MA10。详见 `core/chan_viz.py` 的模块说明。
"""

import sys

from core.chan_viz import main

if __name__ == "__main__":
    sys.exit(main())

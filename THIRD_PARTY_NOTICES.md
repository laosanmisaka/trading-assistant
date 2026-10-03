# 第三方资源与项目授权状态

项目自有代码尚未指定项目级许可证。本文件只记录第三方来源，不替代维护者对项目自有代码作授权决定，也不把依赖许可证扩大到整个仓库。

## ECharts 静态资源

`resources/echarts.min.js` 实际自报版本为 **5.5.1**，不是旧文档写的 5.6.0。2026-10-03 将本地文件与 Apache ECharts `5.5.1` 标签中的 dist 文件逐字节 SHA-256 对照，两者一致：

```text
e84270bd0cd5bdf60fefc26d00c2a391cb2e81f4d26a7a9ee16185a54773a3cf
```

- [上游文件](https://github.com/apache/echarts/blob/5.5.1/dist/echarts.min.js)。
- [上游 LICENSE](https://github.com/apache/echarts/blob/5.5.1/LICENSE)，[本仓副本](resources/licenses/echarts-5.5.1-LICENSE.txt)。
- [上游 NOTICE](https://github.com/apache/echarts/blob/5.5.1/NOTICE)，[本仓副本](resources/licenses/echarts-5.5.1-NOTICE.txt)。

压缩文件保留上游版权头。许可证材料包含上游列出的附属代码说明；本轮未替换或升级 JS 资源。

## Python 依赖与日历

运行/开发依赖由 [requirements.txt](requirements.txt) 声明，实际测试安装版本见 [验证快照](docs/reviews/requirements-macos-py312-2026-10-03.txt)。这些包通过包管理器安装，不是将全部源码 vendoring 到本仓库；各包的许可材料随其发行包提供。

交易所日历来自 [exchange_calendars](https://github.com/gerrymanoim/exchange_calendars)，当前使用 XSHG，其 2026 年更新列在 [发布记录](https://github.com/gerrymanoim/exchange_calendars/releases)。程序读取该版本支持的年份范围，不向未知年份外推工作日。日历包是工程数据来源，不代替交易所临时休市通知。

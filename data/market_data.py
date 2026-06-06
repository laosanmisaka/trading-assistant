"""AKShare数据接口封装 - 异步获取通过QThread + pyqtSignal，集成缓存防封IP"""

import pandas as pd
from datetime import datetime, timedelta
from typing import Optional

from PyQt5.QtCore import QThread, pyqtSignal

import traceback

from data.models import RealtimeQuote, KLineData
from data.database import (
    get_stock_name, search_stock_names, get_stock_names_count,
    save_stock_names_batch, update_stock_name,
)
from utils.logger import get_logger
from utils.cache import (
    get_kline_cache, get_search_cache, get_30min_cache,
)

logger = get_logger(__name__)


# ============================================================
# 数据安全转换工具
# ============================================================

def _safe_float(val, default=0.0) -> float:
    try:
        return float(val) if pd.notna(val) else default
    except (ValueError, TypeError):
        return default


def _safe_int(val, default=0) -> int:
    try:
        return int(float(val)) if pd.notna(val) else default
    except (ValueError, TypeError):
        return default


# ============================================================
# 同步数据获取 (在工作线程中调用)
# ============================================================

def _add_market_prefix(code: str) -> str:
    """给纯数字代码加市场前缀 (新浪格式): '000001' → 'sz000001'"""
    if code.startswith(("sz", "sh", "bj")):
        return code
    if code.startswith(("0", "3", "2")):
        return f"sz{code}"
    if code.startswith(("6", "9")):
        return f"sh{code}"
    if code.startswith(("8", "4")):
        return f"bj{code}"
    return code


def fetch_kline(
    code: str,
    period: str = "daily",
    days: int = 250,
) -> list[KLineData]:
    """
    获取历史K线数据 (带缓存，新浪源)
    period: 'daily' | 'weekly' | 'monthly'
    周线/月线从日线聚合生成
    返回: list[KLineData] (按日期升序)
    """
    import pandas as pd
    import numpy as np
    cache = get_kline_cache()
    cache_key = f"kline:{code}:{period}:{days}"

    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        import akshare as ak

        if period == "daily":
            logger.debug(f"从新浪源获取日K线: {code}")
            sina_code = _add_market_prefix(code)
            df = ak.stock_zh_a_daily(symbol=sina_code, adjust="qfq")
            if df is None or df.empty:
                return []
            # 新浪列名已是英文: date, open, high, low, close, volume
            df = df.tail(days)
            result = _df_to_klines(df, code, "daily")

        elif period in ("weekly", "monthly"):
            # 从日线聚合
            logger.debug(f"从日线聚合{period}K线: {code}")
            sina_code = _add_market_prefix(code)
            df = ak.stock_zh_a_daily(symbol=sina_code, adjust="qfq")
            if df is None or df.empty:
                return []

            # 聚合
            df["date"] = pd.to_datetime(df["date"])
            df.set_index("date", inplace=True)
            freq = "W" if period == "weekly" else "ME"
            agg = df.resample(freq).agg({
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }).dropna()
            agg = agg.tail(days)
            agg["date"] = agg.index.strftime("%Y-%m-%d")
            result = _df_to_klines(agg.reset_index(drop=True), code, period)

        else:
            logger.warning(f"不支持的K线周期: {period}")
            return []

        cache.set(cache_key, result, ttl=300.0)
        logger.info(f"获取 {code} {period} K线 {len(result)} 条")
        return result

    except Exception as e:
        logger.error(f"获取K线失败 ({code}, {period}): {e}")
        return []


def _df_to_klines(df, code: str, period: str) -> list[KLineData]:
    """DataFrame 转 KLineData 列表"""
    result = []
    for _, row in df.iterrows():
        result.append(KLineData(
            code=code,
            date=str(row.get("date", ""))[:10],
            open=_safe_float(row.get("open")),
            high=_safe_float(row.get("high")),
            low=_safe_float(row.get("low")),
            close=_safe_float(row.get("close")),
            volume=_safe_int(row.get("volume")),
            period=period,
        ))
    return result


def fetch_30min_kline(code: str, days: int = 60) -> list[KLineData]:
    """
    获取短周期K线数据 (用于30分钟级别分析)
    优先使用东方财富源60分钟线，失败时返回空
    返回: list[KLineData]
    """
    import time
    cache = get_30min_cache()
    cache_key = f"kline_60min:{code}:{days}"

    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    for attempt in range(2):
        try:
            import akshare as ak
            logger.debug(f"获取60分钟K线: {code} (第{attempt+1}次)")

            df = ak.stock_zh_a_hist_min_em(symbol=code, period="60", adjust="qfq")
            if df is None or df.empty:
                return []

            col_map = {
                "时间": "date", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume",
            }
            df = df.rename(columns=col_map)
            limit = days * 8
            if len(df) > limit:
                df = df.tail(limit)

            result = _df_to_klines(df, code, "60min")
            cache.set(cache_key, result, ttl=120.0)
            logger.info(f"获取 {code} 60min K线 {len(result)} 条")
            return result

        except Exception as e:
            logger.warning(f"获取分钟K线失败 ({code}, 第{attempt+1}次): {e}")
            if attempt < 1:
                time.sleep(2)

    return []


def fetch_1min_kline_history(code: str) -> list[dict]:
    """
    获取历史 1min K 线数据（通达信 MOOTDX，分页拉取）
    覆盖约 4~5 个月（~21,000 条），远超东方财富的 5 天
    返回: [{code, timestamp, open, high, low, close, volume, period}, ...]
    """
    import time
    cache = get_kline_cache()
    cache_key = f"kline_1min_tdx:{code}"

    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    for attempt in range(2):
        try:
            from mootdx.quotes import Quotes
            from config import TDX_HOST, TDX_PORT, TDX_TIMEOUT

            logger.debug(f"TDX获取1分钟K线: {code} (第{attempt+1}次)")
            client = Quotes.factory(
                market="std", host=TDX_HOST, port=TDX_PORT,
                timeout=TDX_TIMEOUT,
            )

            # freq=8 → 1分钟K线, 分页拉取全部历史
            result = []
            start = 0
            while True:
                df = client.bars(symbol=code, frequency=8, start=start, offset=800)
                if df is None or df.empty:
                    break

                for _, row in df.iterrows():
                    ts = str(row.get("datetime", ""))
                    result.append({
                        "code": code,
                        "timestamp": ts,
                        "open": _safe_float(row.get("open")),
                        "high": _safe_float(row.get("high")),
                        "low": _safe_float(row.get("low")),
                        "close": _safe_float(row.get("close")),
                        "volume": _safe_int(row.get("volume", row.get("vol"))),
                        "period": "1min",
                    })

                start += len(df)
                if len(df) < 800:
                    break

            if result:
                # 分页拉取得到的是从新到旧，反转为时间升序
                result.reverse()
                cache.set(cache_key, result, ttl=600.0)
                dates = sorted(set(r["timestamp"][:10] for r in result))
                logger.info(f"TDX获取 {code} 1min 历史K线 {len(result)} 条 "
                           f"({dates[0]} ~ {dates[-1]}, {len(dates)}天)")
            return result

        except ImportError:
            logger.warning("mootdx 未安装，回退到东方财富源")
            return _fetch_1min_kline_em_fallback(code)

        except Exception as e:
            logger.warning(f"TDX 1分钟K线失败 ({code}, 第{attempt+1}次): {e}")
            if attempt < 1:
                time.sleep(3)

    # TDX 完全失败时回退到东方财富
    logger.info(f"TDX 不可用，回退东方财富 1min 源 ({code})")
    return _fetch_1min_kline_em_fallback(code)


def _fetch_1min_kline_em_fallback(code: str) -> list[dict]:
    """东方财富 1min K线 (回退方案, ~5天数据)"""
    import time
    cache = get_30min_cache()
    cache_key = f"kline_1min_em:{code}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    for attempt in range(2):
        try:
            import akshare as ak
            df = ak.stock_zh_a_hist_min_em(symbol=code, period="1", adjust="qfq")
            if df is None or df.empty:
                return []

            col_map = {
                "时间": "timestamp", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume",
            }
            df = df.rename(columns=col_map)

            result = []
            for _, row in df.iterrows():
                result.append({
                    "code": code,
                    "timestamp": str(row.get("timestamp", "")),
                    "open": _safe_float(row.get("open")),
                    "high": _safe_float(row.get("high")),
                    "low": _safe_float(row.get("low")),
                    "close": _safe_float(row.get("close")),
                    "volume": _safe_int(row.get("volume")),
                    "period": "1min",
                })

            cache.set(cache_key, result, ttl=300.0)
            logger.info(f"东方财富 1min 回退: {code} {len(result)} 条")
            return result
        except Exception as e:
            if attempt < 1:
                time.sleep(2)

    return []


def fetch_60min_kline_history(code: str) -> list[dict]:
    """
    获取历史 60min K 线数据（东方财富源），覆盖时间远长于 1min
    返回: [{code, timestamp, open, high, low, close, volume, period='60min'}, ...]
    """
    import time
    cache = get_kline_cache()
    cache_key = f"kline_60min_history:{code}"

    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    for attempt in range(2):
        try:
            import akshare as ak
            logger.debug(f"获取历史60分钟K线: {code} (第{attempt+1}次)")

            df = ak.stock_zh_a_hist_min_em(symbol=code, period="60", adjust="qfq")
            if df is None or df.empty:
                return []

            col_map = {
                "时间": "timestamp", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume",
            }
            df = df.rename(columns=col_map)

            result = []
            for _, row in df.iterrows():
                ts = str(row.get("timestamp", ""))
                result.append({
                    "code": code,
                    "timestamp": ts,
                    "open": _safe_float(row.get("open")),
                    "high": _safe_float(row.get("high")),
                    "low": _safe_float(row.get("low")),
                    "close": _safe_float(row.get("close")),
                    "volume": _safe_int(row.get("volume")),
                    "period": "60min",
                })

            cache.set(cache_key, result, ttl=600.0)
            logger.info(f"获取 {code} 60min 历史K线 {len(result)} 条")
            return result

        except Exception as e:
            logger.warning(f"获取60分钟K线失败 ({code}, 第{attempt+1}次): {e}")
            if attempt < 1:
                time.sleep(3)

    return []


def fetch_today_1min_bars(code: str) -> list[dict]:
    """
    获取当日 1min K线 (TDX源，含完整 OHLCV)
    返回: [{code, timestamp, open, high, low, close, volume, period}, ...]
    用于盘中增量刷新，一次调用替代日线+分时两次调用
    """
    import time
    cache = get_30min_cache()
    cache_key = f"today_1min_tdx:{code}"

    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    for attempt in range(2):
        try:
            from mootdx.quotes import Quotes
            from config import TDX_HOST, TDX_PORT, TDX_TIMEOUT

            client = Quotes.factory(
                market="std", host=TDX_HOST, port=TDX_PORT,
                timeout=TDX_TIMEOUT,
            )
            # 拉最近 300 条 1min (覆盖当天的全部分钟 + 前一天的尾盘)
            df = client.bars(symbol=code, frequency=8, start=0, offset=300)
            if df is None or df.empty:
                return []

            today_str = datetime.now().strftime("%Y-%m-%d")
            result = []
            for _, row in df.iterrows():
                ts = str(row.get("datetime", ""))
                if not ts.startswith(today_str):
                    continue  # 只要当天数据
                result.append({
                    "code": code,
                    "timestamp": ts,
                    "open": _safe_float(row.get("open")),
                    "high": _safe_float(row.get("high")),
                    "low": _safe_float(row.get("low")),
                    "close": _safe_float(row.get("close")),
                    "volume": _safe_int(row.get("volume", row.get("vol"))),
                    "period": "1min",
                })

            if result:
                result.reverse()  # TDX 返回从新到旧，反转为时间升序
                cache.set(cache_key, result, ttl=30.0)

            return result

        except ImportError:
            break
        except Exception as e:
            logger.warning(f"TDX 今日1min失败 ({code}, 第{attempt+1}次): {e}")
            if attempt < 1:
                time.sleep(2)

    # TDX 不可用时回退到 Sina 分时
    return _fetch_today_1min_sina_fallback(code)


def _fetch_today_1min_sina_fallback(code: str) -> list[dict]:
    """Sina 分时数据回退 (只有 price+volume，无完整 OHLC)"""
    bars = fetch_intraday_data(code)
    return [{
        "code": code,
        "timestamp": b["time"],
        "open": b["price"],
        "high": b["price"],
        "low": b["price"],
        "close": b["price"],
        "volume": b.get("volume", 0),
        "period": "1min",
    } for b in bars]


def fetch_intraday_data(code: str) -> list[dict]:
    """
    获取当日分时数据 (Sina源, 缓存1分钟)
    返回: [{time, price, volume, avg_price}, ...]
    注意: 仅用于图表绘制，盘中增量刷新请用 fetch_today_1min_bars
    """
    cache = get_30min_cache()
    cache_key = f"intraday:{code}"

    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        import akshare as ak
        sina_code = _add_market_prefix(code)
        logger.debug(f"获取分时数据: {code} ({sina_code})")

        df = ak.stock_zh_a_minute(symbol=sina_code, period="1")

        if df is None or df.empty:
            return []

        result = []
        cum_vol = 0
        cum_amt = 0.0
        for _, row in df.iterrows():
            price = _safe_float(row.get("close"))
            vol = _safe_int(row.get("volume"))
            cum_vol += vol
            cum_amt += price * vol
            avg_price = cum_amt / cum_vol if cum_vol > 0 else price

            time_str = str(row.get("day", "")) if "day" in df.columns else str(row.name)
            # 保留完整 datetime，格式 "2026-06-03 14:30:00"
            if len(time_str) > 16:
                time_str = time_str[:19]

            result.append({
                "time": time_str,
                "date": time_str[:10],  # YYYY-MM-DD 用于分组
                "price": price,
                "volume": vol,
                "avg_price": round(avg_price, 2),
            })

        cache.set(cache_key, result, ttl=60.0)
        return result

    except Exception as e:
        logger.error(f"获取分时数据失败 ({code}): {e}")
        return []


def sync_stock_names_from_api() -> int:
    """从 AKShare 同步全市场名称映射到本地数据库 (首次启动或手动刷新时调用)"""
    import time
    cache = get_search_cache()

    for attempt in range(3):
        try:
            import akshare as ak
            logger.info(f"从AKShare同步全市场股票名称到本地库... (第{attempt+1}次)")
            df = ak.stock_info_a_code_name()
            records = [
                {"code": str(row["code"]), "name": str(row["name"])}
                for _, row in df.iterrows()
            ]
            count = save_stock_names_batch(records)
            cache.set("names_synced", True, ttl=86400)  # 标记已同步24h
            logger.info(f"股票名称同步完成: {count} 条")
            return count
        except Exception as e:
            logger.warning(f"名称同步失败 (第{attempt+1}/3次): {e}")
            if attempt < 2:
                time.sleep((attempt + 1) * 2)
    return 0


def _ensure_stock_names_table():
    """兼容旧版DB：如果 stock_names 表不存在则创建"""
    from data.database import _connect
    try:
        conn = _connect()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_names (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.commit()
        conn.close()
    except Exception:
        pass


def search_stock(keyword: str) -> list[dict]:
    """搜索股票代码或名称 (本地DB优先，首次启动自动同步)

    - 首次使用：后台静默同步全市场名称到本地库
    - 后续搜索：直接从本地 SQLite 查，毫秒级响应
    - 本地库异常时：回退到 API 实时查询
    """
    import time
    cache = get_search_cache()

    # 确保 stock_names 表存在 (兼容旧版DB)
    _ensure_stock_names_table()

    # 首次启动或超过24h → 触发一次后台同步
    if cache.get("names_synced") is None:
        try:
            count = get_stock_names_count()
            if count < 100:  # 本地库太少，说明还没同步过
                logger.info("本地名称库为空，触发首次同步...")
                try:
                    sync_stock_names_from_api()
                except Exception as e:
                    logger.warning(f"首次同步失败，使用在线搜索: {e}")
        except Exception:
            logger.warning("stock_names 表不可用，回退到 API 搜索")

    # 优先查本地库
    try:
        local_results = search_stock_names(keyword, limit=20)
        if local_results:
            logger.info(
                f"本地DB搜索 '{keyword}': {len(local_results)} 条 "
                f"(共 {get_stock_names_count()} 条缓存)"
            )
            return [{"code": r["code"], "name": r["name"], "price": 0.0}
                    for r in local_results]
    except Exception as e:
        logger.warning(f"本地搜索失败 ({e})，回退到API搜索")

    # 本地库无结果或异常 → 回退到 API
    logger.info(f"本地未找到 '{keyword}'，回退到API搜索")
    return _search_stock_from_api(keyword)


def _search_stock_from_api(keyword: str) -> list[dict]:
    """API 实时搜索 (回退方案)"""
    import time
    cache = get_search_cache()

    names_map = cache.get("all_stock_names")
    if names_map is None:
        last_error = None
        for attempt in range(3):
            try:
                import akshare as ak
                df = ak.stock_info_a_code_name()
                names_map = {}
                for _, row in df.iterrows():
                    names_map[str(row["code"])] = str(row["name"])
                cache.set("all_stock_names", names_map, ttl=3600.0)
                break
            except Exception as e:
                last_error = e
                if attempt < 2:
                    time.sleep((attempt + 1) * 2)
        else:
            raise RuntimeError(
                f"无法连接行情服务器，请检查网络后重试。\n"
                f"详情: {last_error}"
            )

    keyword_lower = keyword.lower()
    filtered = []
    for code, name in names_map.items():
        if keyword_lower in code.lower() or keyword_lower in name.lower():
            filtered.append({"code": code, "name": name, "price": 0.0})
        if len(filtered) >= 20:
            break
    return filtered


# ============================================================
# 异步数据工作线程
# ============================================================

class KLineWorker(QThread):
    """K线数据获取工作线程"""
    data_ready = pyqtSignal(str, str, list)  # (code, period, list[KLineData])
    error_occurred = pyqtSignal(str)

    def __init__(self, code: str, period: str = "daily", days: int = 250, parent=None):
        super().__init__(parent)
        self.code = code
        self.period = period
        self.days = days

    def run(self):
        try:
            if self.period == "30min":
                result = fetch_30min_kline(self.code, self.days)
            else:
                # 优先走 Manager (DB+内存)，没有则回退到 API
                from data.market_data_manager import get_data_manager
                manager = get_data_manager()
                result = manager.get_klines(self.code, self.period, self.days)
                if not result:
                    # DB 无数据时回退到直接 API
                    result = fetch_kline(self.code, self.period, self.days)
            self.data_ready.emit(self.code, self.period, result)
        except Exception:
            logger.error(f"KLineWorker异常:\n{traceback.format_exc()}")
            self.error_occurred.emit(traceback.format_exc())


class IntradayWorker(QThread):
    """分时数据获取工作线程"""
    data_ready = pyqtSignal(str, list)  # (code, [{time, price, volume, avg_price}])
    error_occurred = pyqtSignal(str)

    def __init__(self, code: str, parent=None):
        super().__init__(parent)
        self.code = code

    def run(self):
        try:
            result = fetch_intraday_data(self.code)
            self.data_ready.emit(self.code, result)
        except Exception:
            logger.error(f"IntradayWorker异常:\n{traceback.format_exc()}")
            self.error_occurred.emit(traceback.format_exc())


class StockSearchWorker(QThread):
    """股票搜索工作线程"""
    data_ready = pyqtSignal(list)  # [{code, name, price}]
    error_occurred = pyqtSignal(str)

    def __init__(self, keyword: str, parent=None):
        super().__init__(parent)
        self.keyword = keyword

    def run(self):
        try:
            result = search_stock(self.keyword)
            self.data_ready.emit(result)
        except Exception:
            logger.error(f"StockSearchWorker异常:\n{traceback.format_exc()}")
            self.error_occurred.emit(traceback.format_exc())


# ============================================================
# 单股行情获取 (增量更新用)
# ============================================================

def fetch_single_stock_quote(code: str) -> Optional[RealtimeQuote]:
    """获取单只股票的最新行情 (从日K线最后一条提取)
    只拉取最近几天的数据（通过 start_date 参数），避免获取全量历史
    """
    import akshare as ak
    from datetime import datetime, timedelta
    sina_code = _add_market_prefix(code)
    try:
        # 只拉最近2天，取最后一行即可
        start = (datetime.now() - timedelta(days=2)).strftime("%Y%m%d")
        df = ak.stock_zh_a_daily(symbol=sina_code, adjust="qfq", start_date=start)
        if df is None or df.empty:
            return None
        last = df.iloc[-1]
        # 计算涨跌幅 (相对于前一日收盘)
        pre_close = _safe_float(df.iloc[-2]["close"]) if len(df) >= 2 else _safe_float(last["close"])
        price = _safe_float(last["close"])
        change_pct = ((price - pre_close) / pre_close * 100) if pre_close > 0 else 0.0
        return RealtimeQuote(
            code=code,
            name="",  # 名称由DB提供
            price=price,
            change_pct=round(change_pct, 2),
            change_amt=round(price - pre_close, 2),
            volume=_safe_int(last.get("volume")),
            turnover=_safe_float(last.get("amount")) if "amount" in df.columns else 0.0,
            high=_safe_float(last.get("high")),
            low=_safe_float(last.get("low")),
            open=_safe_float(last.get("open")),
            pre_close=round(pre_close, 2),
            timestamp=datetime.now().strftime("%H:%M:%S"),
        )
    except Exception as e:
        logger.warning(f"获取单股行情失败 ({code}): {type(e).__name__}")
        return None


# ============================================================
# 并行增量刷新 Worker (每30s, 所有已追踪股票)
# ============================================================

class IncrementalRefreshWorker(QThread):
    """并行增量刷新 — 通过 MarketDataManager 获取最新行情并更新内存层
    manager.refresh_quotes_batch 内部并行拉取 + 更新 today_bars + quotes
    """
    data_ready = pyqtSignal(dict)       # {code: RealtimeQuote}
    stock_done = pyqtSignal(str, object) # (code, RealtimeQuote or None)

    def __init__(self, codes: list[str], parent=None):
        super().__init__(parent)
        self.codes = codes

    def run(self):
        from data.market_data_manager import get_data_manager
        manager = get_data_manager()
        pending = manager.get_pending_codes()

        # 跳过正在初始获取中的股票
        active_codes = [c for c in self.codes if c not in pending]

        if active_codes:
            try:
                # 一次 TDX 1min 调用 = 日线 OHLCV + 现价 + 分钟线存储
                manager.refresh_minute_bars_batch(active_codes)
            except Exception:
                from utils.logger import get_logger
                get_logger(__name__).exception("增量刷新异常")

        # 返回当前缓存中的所有现价（供 UI 刷新表格）
        results = manager.get_all_quotes() if active_codes else {}
        self.data_ready.emit(results)


# ============================================================
# 新股全量数据 Worker (添加新股时独立运行)
# ============================================================

class InitialFetchWorker(QThread):
    """新股全量数据获取 — 通过 MarketDataManager 获取半年日线/周线/月线，写DB
    完成后通过信号通知UI更新
    """
    kline_ready = pyqtSignal(str, str, list)  # (code, period, list[KLineData])
    all_done = pyqtSignal(str)                # code — 全部周期获取完成
    error_occurred = pyqtSignal(str)

    def __init__(self, code: str, parent=None):
        super().__init__(parent)
        self.code = code

    def run(self):
        from data.market_data_manager import get_data_manager
        manager = get_data_manager()

        try:
            results = manager.fetch_and_store_initial(self.code)
            # 从返回结果发各周期信号（测试用例使用）
            for key, data in results.items():
                if data and key in ("daily", "weekly", "monthly"):
                    self.kline_ready.emit(self.code, key, data)
            self.all_done.emit(self.code)
        except Exception:
            logger.error(f"InitialFetchWorker异常 ({self.code}):\n{traceback.format_exc()}")
            self.error_occurred.emit(traceback.format_exc())

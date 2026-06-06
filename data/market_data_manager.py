"""市场数据管理器 — 内存缓存 + DB 双层架构

内存层（热路径，毫秒级）:
  - _quotes: 现价/涨跌幅，每60s刷新
  - _today_bars: 今日日线bar，每60s更新
  - _kline_cache: DB历史 + 今日bar 拼接，懒加载

DB 层（持久化）:
  - 完整日线/周线/月线历史（仅终值）
  - 每5分钟从内存 flush 一次
  - 启动时加载到内存
"""

import threading
from datetime import datetime, timedelta

from data.models import RealtimeQuote, KLineData
from data.market_data import (
    fetch_kline, fetch_1min_kline_history,
    fetch_60min_kline_history, fetch_today_1min_bars,
)
from data.database import (
    save_klines_batch, save_klines_minute_batch,
    get_klines as db_get_klines, get_latest_kline_date,
    get_klines_minute as db_get_klines_minute,
)
from utils.logger import get_logger

logger = get_logger(__name__)


def _dict_to_kline(d: dict) -> KLineData:
    """将DB返回的dict转为KLineData"""
    return KLineData(
        code=d.get("code", ""),
        date=d.get("date", ""),
        open=float(d.get("open", 0)),
        high=float(d.get("high", 0)),
        low=float(d.get("low", 0)),
        close=float(d.get("close", 0)),
        volume=int(d.get("volume", 0)),
        period=d.get("period", "daily"),
    )


def _dicts_to_klines(dicts: list[dict]) -> list[KLineData]:
    """批量转换"""
    return [_dict_to_kline(d) for d in dicts]


class MarketDataManager:
    """股票市场数据的统一入口

    所有计算模块（买点扫描、预警、图表）通过此管理器获取数据，
    不直接调用 API 或 DB。
    """

    def __init__(self):
        # 现价快照: {code: RealtimeQuote}
        self._quotes: dict[str, RealtimeQuote] = {}
        self._quotes_lock = threading.Lock()

        # 今日日线 bar: {(code, period): dict}
        self._today_bars: dict[tuple[str, str], dict] = {}
        self._today_bars_lock = threading.Lock()

        # 分钟K线内存缓存: {code: list[dict]} — 1min 日内数据
        self._minute_bars: dict[str, list[dict]] = {}
        self._minute_bars_lock = threading.Lock()

        # K线缓存（历史+今日拼接结果）: {cache_key: list[KLineData]}
        self._kline_cache: dict[str, list[KLineData]] = {}
        self._kline_cache_lock = threading.Lock()

        # 正在初始获取中的股票代码
        self._pending_codes: set[str] = set()
        self._pending_lock = threading.Lock()

        # 上次 flush 时间
        self._last_flush_time = datetime.now()

        # K线缓存TTL (秒)
        self._cache_ttl = 60.0
        self._cache_timestamps: dict[str, float] = {}

    # ================================================================
    # 现价相关
    # ================================================================

    def update_quotes(self, quotes: dict[str, RealtimeQuote]) -> None:
        """批量更新现价快照"""
        with self._quotes_lock:
            self._quotes.update(quotes)

    def get_quote(self, code: str) -> RealtimeQuote | None:
        """获取单只股票的现价快照"""
        with self._quotes_lock:
            return self._quotes.get(code)

    def get_all_quotes(self) -> dict[str, RealtimeQuote]:
        """获取全部现价快照"""
        with self._quotes_lock:
            return dict(self._quotes)

    # ================================================================
    # 启动初始化
    # ================================================================

    def startup_load_quotes(self, codes: list[str]) -> int:
        """启动时从DB的日线末尾加载现价到内存缓存（冷启动，无API调用）
        返回成功加载的股票数量
        """
        loaded = 0
        for code in codes:
            try:
                db_dicts = db_get_klines(code, "daily", days=2)
                if not db_dicts:
                    continue

                last = db_dicts[-1]
                price = last["close"]
                pre_close = price
                if len(db_dicts) >= 2:
                    pre_close = db_dicts[-2]["close"]

                change_pct = ((price - pre_close) / pre_close * 100) if pre_close > 0 else 0.0
                quote = RealtimeQuote(
                    code=code,
                    name="",
                    price=round(price, 2),
                    change_pct=round(change_pct, 2),
                    change_amt=round(price - pre_close, 2),
                    volume=last.get("volume", 0),
                    high=last.get("high", 0),
                    low=last.get("low", 0),
                    open=last.get("open", 0),
                    pre_close=round(pre_close, 2),
                    timestamp=last["date"],
                )
                with self._quotes_lock:
                    self._quotes[code] = quote
                loaded += 1
            except Exception as e:
                logger.debug(f"启动加载 {code} 现价失败: {e}")

        if loaded > 0:
            logger.info(f"启动加载: {loaded}/{len(codes)} 只股票现价从DB恢复")
        return loaded

    # ================================================================
    # 今日 bar
    # ================================================================

    def update_today_bar(self, code: str, period: str, bar: dict) -> None:
        """更新今日K线bar（盘中每次刷新覆盖）"""
        key = (code, period)
        with self._today_bars_lock:
            self._today_bars[key] = bar
        # 使K线缓存失效
        self._invalidate_kline_cache(code, period)

    def get_today_bar(self, code: str, period: str) -> dict | None:
        """获取今日bar"""
        with self._today_bars_lock:
            return self._today_bars.get((code, period))

    def _invalidate_kline_cache(self, code: str, period: str | None = None) -> None:
        """使指定股票的K线缓存失效"""
        periods = [period] if period else ["daily", "weekly", "monthly"]
        with self._kline_cache_lock:
            for p in periods:
                prefix = f"{code}:{p}:"
                stale = [k for k in self._kline_cache if k.startswith(prefix)]
                for k in stale:
                    self._kline_cache.pop(k, None)
                    self._cache_timestamps.pop(k, None)

    # ================================================================
    # K线数据 — 计算模块的主入口
    # ================================================================

    def get_klines(
        self,
        code: str,
        period: str = "daily",
        days: int | None = None,
        force_refresh: bool = False,
    ) -> list[KLineData]:
        """
        获取K线数据 = DB历史 + 内存中的今日bar
        优先从内存缓存返回，缓存过期则从DB重建

        Returns:
            list[KLineData] 按日期升序
        """
        cache_key = f"{code}:{period}:{days or 'all'}"
        import time

        # 检查缓存
        if not force_refresh:
            with self._kline_cache_lock:
                cached = self._kline_cache.get(cache_key)
                ts = self._cache_timestamps.get(cache_key, 0)
                if cached is not None and (time.time() - ts) < self._cache_ttl:
                    return cached

        # 从DB加载 + 拼接今日bar
        result = self._build_klines(code, period, days)

        # 写入缓存
        with self._kline_cache_lock:
            self._kline_cache[cache_key] = result
            self._cache_timestamps[cache_key] = time.time()

        return result

    def _build_klines(
        self, code: str, period: str, days: int | None
    ) -> list[KLineData]:
        """从DB加载历史K线，拼接内存中的今日bar"""
        db_dicts = db_get_klines(code=code, period=period, days=days)
        klines = _dicts_to_klines(db_dicts)
        today = self.get_today_bar(code, period)

        if not today:
            return klines

        today_kline = _dict_to_kline(today)
        today_date = today["date"]

        # 如果DB已有今日数据，用内存中的替换（盘中更新）
        if klines and klines[-1].date == today_date:
            klines[-1] = today_kline
        else:
            klines.append(today_kline)

        # 如果指定了 days，截断
        if days is not None and len(klines) > days:
            klines = klines[-days:]

        return klines

    # ================================================================
    # 新股初始获取 (添加股票时调用)
    # ================================================================

    def is_pending(self, code: str) -> bool:
        """检查股票是否正在初始获取中"""
        with self._pending_lock:
            return code in self._pending_codes

    def mark_pending(self, code: str) -> None:
        """标记为初始获取中"""
        with self._pending_lock:
            self._pending_codes.add(code)

    def unmark_pending(self, code: str) -> None:
        """移除初始获取标记"""
        with self._pending_lock:
            self._pending_codes.discard(code)

    def fetch_and_store_initial(self, code: str) -> dict:
        """
        新股初始获取：拉取半年日线 + 历史1min/60min数据，存DB，加载到内存
        返回: {"daily": list[dict], "weekly": list[dict], "monthly": list[dict],
                "1min": list[dict], "60min": list[dict]}
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        periods = ["daily", "weekly", "monthly"]
        days_map = {"daily": 126, "weekly": 52, "monthly": 12}
        results = {}

        self.mark_pending(code)

        try:
            # 第一阶段: 并行拉取日/周/月 + 1min + 60min
            klines_by_period = {}
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {
                    executor.submit(fetch_kline, code, p, days_map[p]): p
                    for p in periods
                }
                # 同时拉取分钟级数据
                future_1min = executor.submit(fetch_1min_kline_history, code)
                future_60min = executor.submit(fetch_60min_kline_history, code)
                futures[future_1min] = "1min"
                futures[future_60min] = "60min"

                for future in as_completed(futures):
                    key = futures[future]
                    try:
                        result_data = future.result()
                        klines_by_period[key] = result_data
                    except Exception as e:
                        logger.error(f"初始获取 {code} {key} 数据失败: {e}")
                        klines_by_period[key] = []

            # 第二阶段: 串行写 DB (避免并发写冲突)
            all_dicts = []
            daily_klines = klines_by_period.get("daily", [])
            for period in periods:
                klines = klines_by_period.get(period, [])
                kline_dicts = [
                    {
                        "code": k.code, "date": k.date,
                        "open": k.open, "high": k.high,
                        "low": k.low, "close": k.close,
                        "volume": k.volume, "period": k.period,
                    }
                    for k in klines
                ]
                results[period] = kline_dicts
                all_dicts.extend(kline_dicts)

            # 写入日/周/月
            if all_dicts:
                try:
                    save_klines_batch(all_dicts)
                    logger.info(f"已存储 {code} K线 {len(all_dicts)} 条 (日/周/月)")
                except Exception as e:
                    logger.error(f"存储 {code} K线到DB失败: {e}")

            # 写入分钟级数据 (1min + 60min)
            for min_period in ["1min", "60min"]:
                minute_bars = klines_by_period.get(min_period, [])
                if minute_bars:
                    try:
                        saved = save_klines_minute_batch(minute_bars)
                        pct = len(minute_bars)
                        logger.info(f"已存储 {code} {min_period} K线 {saved}/{pct} 条")
                        results[min_period] = minute_bars
                    except Exception as e:
                        logger.error(f"存储 {code} {min_period} K线到DB失败: {e}")

            # 用最新 1min 数据初始化现价缓存（比日线更精确）
            minute_bars_1min = klines_by_period.get("1min", [])
            if minute_bars_1min:
                # 从 1min 数据计算当日 OHLC
                prices = [b.get("close", b.get("price", 0)) for b in minute_bars_1min]
                volumes = [b.get("volume", 0) for b in minute_bars_1min]
                day_open = minute_bars_1min[0].get("open", prices[0])
                day_high = max(b.get("high", p) for b, p in zip(minute_bars_1min, prices))
                day_low = min(b.get("low", p) for b, p in zip(minute_bars_1min, prices))
                price = prices[-1]
                total_vol = sum(volumes)

                # 前收盘：找日线中早于 1min 数据日期的最后一根
                last_1min_date = minute_bars_1min[-1]["timestamp"][:10]
                pre_close = price  # fallback
                for k in reversed(daily_klines):
                    if k.date < last_1min_date:
                        pre_close = k.close
                        break
            elif daily_klines and len(daily_klines) >= 2:
                # 1min 不可用时回退到日线：最新日线收盘作为现价
                last = daily_klines[-1]
                prev = daily_klines[-2]
                price = last.close
                pre_close = prev.close  # 日线价格对应昨天，前收是前天
                day_open = last.open
                day_high = last.high
                day_low = last.low
                total_vol = last.volume
            else:
                price, pre_close = 0.0, 0.0
                day_open = day_high = day_low = 0.0
                total_vol = 0

            if price > 0:
                change_pct = ((price - pre_close) / pre_close * 100) if pre_close > 0 else 0.0
                quote = RealtimeQuote(
                    code=code, name="",
                    price=price,
                    change_pct=round(change_pct, 2),
                    change_amt=round(price - pre_close, 2),
                    volume=total_vol,
                    high=day_high, low=day_low,
                    open=day_open,
                    pre_close=round(pre_close, 2),
                    timestamp=datetime.now().strftime("%H:%M:%S"),
                )
                with self._quotes_lock:
                    self._quotes[code] = quote

            # 清除缓存让下次读取走DB
            self._invalidate_kline_cache(code)

        finally:
            self.unmark_pending(code)

        return results

    # ================================================================
    # 定期 flush 到 DB
    # ================================================================

    # ================================================================
    # 分钟K线管理
    # ================================================================

    def refresh_minute_bars(self, code: str) -> int:
        """拉取今日 1min K线 (TDX)，同时更新日线 OHLCV 和现价
        一次 API 调用替代 refresh_quote + 分时两次调用
        返回写入 DB 的分钟线条数
        """
        bars = fetch_today_1min_bars(code)
        if not bars:
            return 0

        # --- 从分钟线聚合当日 OHLCV → 更新 _today_bars ---
        today_str = bars[0]["timestamp"][:10]
        prices = [b["close"] for b in bars]
        volumes = [b["volume"] for b in bars]

        today_bar = {
            "code": code,
            "date": today_str,
            "open": bars[0]["open"],
            "high": max(b["high"] for b in bars),
            "low": min(b["low"] for b in bars),
            "close": bars[-1]["close"],
            "volume": sum(volumes),
            "period": "daily",
        }
        self.update_today_bar(code, "daily", today_bar)

        # --- 更新现价缓存 ---
        daily = self.get_klines(code, "daily", days=3)
        last_1min_date = bars[-1]["timestamp"][:10]
        pre_close = 0.0
        for k in reversed(daily):
            if k.date < last_1min_date:
                pre_close = k.close
                break
        if pre_close == 0.0 and prices:
            pre_close = prices[0]

        last_price = bars[-1]["close"]
        change_pct = ((last_price - pre_close) / pre_close * 100) if pre_close > 0 else 0.0
        quote = RealtimeQuote(
            code=code, name="",
            price=last_price,
            change_pct=round(change_pct, 2),
            change_amt=round(last_price - pre_close, 2),
            volume=sum(volumes),
            high=max(b["high"] for b in bars),
            low=min(b["low"] for b in bars),
            open=bars[0]["open"],
            pre_close=round(pre_close, 2),
            timestamp=bars[-1]["timestamp"],
        )
        with self._quotes_lock:
            self._quotes[code] = quote

        # --- 写入 klines_minute ---
        saved = save_klines_minute_batch(bars)

        # 更新内存缓存
        with self._minute_bars_lock:
            self._minute_bars[code] = bars

        return saved

    def refresh_minute_bars_batch(self, codes: list[str]) -> dict[str, int]:
        """批量拉取分钟数据（并行）"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        results = {}
        if not codes:
            return results

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(self.refresh_minute_bars, c): c for c in codes}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    count = future.result()
                    results[code] = count
                except Exception as e:
                    logger.warning(f"刷新分钟数据失败 {code}: {e}")
                    results[code] = 0

        return results

    def get_minute_bars(self, code: str) -> list[dict]:
        """获取内存中的分钟数据"""
        with self._minute_bars_lock:
            return list(self._minute_bars.get(code, []))

    def get_minute_klines_from_db(
        self, code: str, period: str = "1min", minutes: int | None = None
    ) -> list[dict]:
        """从 DB 读取分钟K线"""
        return db_get_klines_minute(code, period, minutes=minutes)

    # ================================================================
    # 定期 flush 到 DB
    # ================================================================

    def flush_today_bars(self) -> int:
        """
        将内存中的今日bar + 分钟K线批量flush到DB
        返回写入条数
        """
        # 日线 bar
        with self._today_bars_lock:
            bars = list(self._today_bars.values())

        count = 0
        if bars:
            count += save_klines_batch(bars)
            if count > 0:
                logger.debug(f"Flush {count} 条今日bar到DB")

        # 分钟K线 (从内存 flush)
        with self._minute_bars_lock:
            minute_list = list(self._minute_bars.items())

        for code, bars in minute_list:
            kline_dicts = []
            for b in bars:
                price = b.get("price", 0)
                kline_dicts.append({
                    "code": code,
                    "timestamp": b["time"],
                    "open": price, "high": price,
                    "low": price, "close": price,
                    "volume": b.get("volume", 0),
                    "period": "1min",
                })
            if kline_dicts:
                count += save_klines_minute_batch(kline_dicts)

        self._last_flush_time = datetime.now()
        return count

    def should_flush(self, interval_seconds: float = 300.0) -> bool:
        """判断是否需要flush（默认每5分钟）"""
        return (datetime.now() - self._last_flush_time).total_seconds() >= interval_seconds

    def get_pending_codes(self) -> set[str]:
        """获取正在初始获取中的代码集合"""
        with self._pending_lock:
            return set(self._pending_codes)


# 全局单例
_manager: MarketDataManager | None = None
_manager_lock = threading.Lock()


def get_data_manager() -> MarketDataManager:
    """获取全局 MarketDataManager 单例"""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = MarketDataManager()
    return _manager

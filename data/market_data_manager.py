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
    DataSourceError,
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
        self._history_checked = {}
        self._hourly_checked = {}
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

        by_date = {bar.date: bar for bar in klines}
        if not klines or today_date >= klines[-1].date:
            by_date[today_date] = today_kline
        klines = [by_date[key] for key in sorted(by_date)]

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

    def needs_history_refresh(self, code):
        from core.trading_calendar import local_now
        return self._history_checked.get(code) != local_now().date()

    def _fetch_history(self, code):
        from concurrent.futures import ThreadPoolExecutor
        from data.history import aggregate_daily
        with ThreadPoolExecutor(max_workers=3) as executor:
            daily_task = executor.submit(fetch_kline, code, "daily", 100000)
            minute_task = executor.submit(fetch_1min_kline_history, code)
            hourly_task = executor.submit(fetch_60min_kline_history, code)
            daily, minute, hourly = daily_task.result(), minute_task.result(), hourly_task.result()
        if not daily:
            raise DataSourceError(f"{code} 无日线，不能完成历史补齐")
        daily = sorted(daily, key=lambda bar: bar.date)
        return {"daily": daily, "weekly": aggregate_daily(daily, "weekly"),
                "monthly": aggregate_daily(daily, "monthly"), "1min": minute, "60min": hourly}

    def fetch_and_store_initial(self, code: str) -> dict:
        """Rebuild complete available snapshots on first access and each new day."""
        from dataclasses import asdict
        from data.database import replace_history_snapshot
        from data.history import quote_from_history
        from core.trading_calendar import local_now
        self.mark_pending(code)
        try:
            fetched = self._fetch_history(code)
            results = {period: ([asdict(bar) for bar in bars] if not period.endswith("min") else bars)
                       for period, bars in fetched.items()}
            replace_history_snapshot(code, results)
            quote = quote_from_history(code, fetched["daily"], fetched["1min"])
            if quote:
                self.update_quotes({code: quote})
            with self._today_bars_lock:
                for period in ("daily", "weekly", "monthly"):
                    self._today_bars.pop((code, period), None)
            self._invalidate_kline_cache(code)
            if fetched["1min"] and fetched["60min"]:
                self._history_checked[code] = local_now().date()
            else:
                logger.warning("%s 分钟历史为空，保留旧数据并等待后续补齐", code)
            return results
        finally:
            self.unmark_pending(code)

    def refresh_history_if_needed(self, code):
        from core.trading_calendar import local_now
        from data.database import replace_history_snapshot
        if self.needs_history_refresh(code):
            self.fetch_and_store_initial(code)
        hour = local_now().strftime("%Y-%m-%d %H")
        if self._hourly_checked.get(code) != hour:
            bars = fetch_60min_kline_history(code)
            if bars:
                replace_history_snapshot(code, {"60min": bars})
                self._hourly_checked[code] = hour

    def _refresh_aggregated_periods(self, code):
        from dataclasses import asdict
        from data.history import aggregate_daily
        from data.database import replace_history_snapshot
        daily = self.get_klines(code, "daily", force_refresh=True)
        derived = {period: [asdict(bar) for bar in aggregate_daily(daily, period)]
                   for period in ("weekly", "monthly")}
        replace_history_snapshot(code, derived)
        self._invalidate_kline_cache(code)

    def refresh_minute_bars(self, code: str) -> int:
        """拉取今日 1min K线 (TDX)，同时更新日线 OHLCV 和现价
        一次 API 调用替代 refresh_quote + 分时两次调用
        返回写入 DB 的分钟线条数
        """
        from data.history import latest_session
        bars = latest_session(fetch_today_1min_bars(code))
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
        self._refresh_aggregated_periods(code)

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
        将内存中的今日日线bar批量flush到DB
        返回写入条数

        分钟K线不在此flush：refresh_minute_bars 拉取时已实时写入 klines_minute。
        """
        # 日线 bar
        with self._today_bars_lock:
            bars = list(self._today_bars.values())

        count = 0
        if bars:
            count += save_klines_batch(bars)
            if count > 0:
                logger.debug(f"Flush {count} 条今日bar到DB")

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

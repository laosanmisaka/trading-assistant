"""数据库层 - SQLite CRUD操作 (Repository Pattern)"""

import sqlite3
import os
from typing import Optional

from config import DB_PATH, PRESET_GROUPS
from data.models import (
    Group, Stock, Trade, AlertDisabled, DisciplineRule,
    GroupType, TradeType, AlertType,
)


def _get_path() -> str:
    """获取数据库路径 (相对于项目根目录)"""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, DB_PATH)


def _connect() -> sqlite3.Connection:
    """获取数据库连接"""
    conn = sqlite3.connect(_get_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")       # 写不阻塞读，减少竞争
    conn.execute("PRAGMA busy_timeout = 5000")      # 5秒超时，等锁不立即报错
    return conn


# ============================================================
# 初始化
# ============================================================

def init_db():
    """初始化数据库表结构和预设数据"""
    conn = _connect()
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('holding','cleared','tracking','custom')),
            sort_order INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS stocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            name TEXT DEFAULT '',
            group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            added_date TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(code, group_id)
        );

        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_code TEXT NOT NULL,
            trade_type TEXT NOT NULL CHECK(trade_type IN ('buy','sell')),
            price REAL NOT NULL,
            quantity INTEGER NOT NULL,
            fee REAL DEFAULT 0.0,
            trade_date TEXT NOT NULL,
            notes TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS alerts_disabled (
            stock_code TEXT PRIMARY KEY,
            alert_type TEXT DEFAULT 'all',
            disabled_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS discipline_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_code TEXT DEFAULT '',
            rule_text TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS stock_names (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS klines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume INTEGER DEFAULT 0,
            period TEXT NOT NULL DEFAULT 'daily',
            UNIQUE(code, date, period)
        );
        CREATE INDEX IF NOT EXISTS idx_klines_code_date
            ON klines(code, period, date);

        CREATE TABLE IF NOT EXISTS klines_minute (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            open REAL DEFAULT 0,
            high REAL DEFAULT 0,
            low REAL DEFAULT 0,
            close REAL DEFAULT 0,
            volume INTEGER DEFAULT 0,
            period TEXT NOT NULL DEFAULT '1min',
            UNIQUE(code, timestamp, period)
        );
        CREATE INDEX IF NOT EXISTS idx_klines_minute_code_period
            ON klines_minute(code, period, timestamp);
    """)

    # 预设分组 — 先建唯一索引防止重复
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_groups_name_type
        ON groups(name, type)
    """)
    for i, (name, gtype) in enumerate(PRESET_GROUPS):
        cur.execute(
            "INSERT OR IGNORE INTO groups (name, type, sort_order) VALUES (?, ?, ?)",
            (name, gtype, i),
        )

    conn.commit()
    conn.close()


# ============================================================
# 分组 (Group) CRUD
# ============================================================

def get_all_groups() -> list[Group]:
    conn = _connect()
    rows = conn.execute(
        "SELECT id, name, type, sort_order FROM groups ORDER BY sort_order, id"
    ).fetchall()
    conn.close()
    return [Group(id=r["id"], name=r["name"], type=r["type"], sort_order=r["sort_order"]) for r in rows]


def add_group(name: str, gtype: str = GroupType.CUSTOM.value) -> Group:
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO groups (name, type, sort_order) VALUES (?, ?, (SELECT COALESCE(MAX(sort_order),0)+1 FROM groups))",
        (name, gtype),
    )
    conn.commit()
    gid = cur.lastrowid
    conn.close()
    return Group(id=gid, name=name, type=gtype)


def update_group(group_id: int, name: str) -> None:
    conn = _connect()
    conn.execute("UPDATE groups SET name=? WHERE id=?", (name, group_id))
    conn.commit()
    conn.close()


def delete_group(group_id: int) -> None:
    conn = _connect()
    conn.execute("DELETE FROM groups WHERE id=? AND type='custom'", (group_id,))
    conn.commit()
    conn.close()


# ============================================================
# 股票 (Stock) CRUD
# ============================================================

def get_stocks_by_group(group_id: int) -> list[Stock]:
    conn = _connect()
    rows = conn.execute(
        "SELECT id, code, name, group_id, added_date FROM stocks WHERE group_id=? ORDER BY added_date",
        (group_id,),
    ).fetchall()
    conn.close()
    return [Stock(id=r["id"], code=r["code"], name=r["name"],
                  group_id=r["group_id"], added_date=r["added_date"]) for r in rows]


def get_all_stocks() -> list[Stock]:
    conn = _connect()
    rows = conn.execute(
        "SELECT id, code, name, group_id, added_date FROM stocks ORDER BY added_date"
    ).fetchall()
    conn.close()
    return [Stock(id=r["id"], code=r["code"], name=r["name"],
                  group_id=r["group_id"], added_date=r["added_date"]) for r in rows]


def add_stock(code: str, name: str, group_id: int) -> Optional[Stock]:
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO stocks (code, name, group_id) VALUES (?, ?, ?)",
            (code, name, group_id),
        )
        conn.commit()
        sid = cur.lastrowid
        if sid == 0:
            return None  # 已存在
        return Stock(id=sid, code=code, name=name, group_id=group_id)
    finally:
        conn.close()


def remove_stock(stock_id: int) -> None:
    conn = _connect()
    conn.execute("DELETE FROM stocks WHERE id=?", (stock_id,))
    conn.commit()
    conn.close()


def move_stock(stock_id: int, new_group_id: int) -> None:
    conn = _connect()
    # 删除目标分组中同代码的股票 (如果存在)
    cur = conn.execute("SELECT code FROM stocks WHERE id=?", (stock_id,))
    row = cur.fetchone()
    if row:
        conn.execute(
            "DELETE FROM stocks WHERE code=? AND group_id=?",
            (row["code"], new_group_id),
        )
        conn.execute(
            "UPDATE stocks SET group_id=? WHERE id=?",
            (new_group_id, stock_id),
        )
    conn.commit()
    conn.close()


def get_stock_by_code_group(code: str, group_id: int) -> Optional[Stock]:
    conn = _connect()
    row = conn.execute(
        "SELECT id, code, name, group_id, added_date FROM stocks WHERE code=? AND group_id=?",
        (code, group_id),
    ).fetchone()
    conn.close()
    if row:
        return Stock(id=row["id"], code=row["code"], name=row["name"],
                     group_id=row["group_id"], added_date=row["added_date"])
    return None


# ============================================================
# 交易记录 (Trade) CRUD
# ============================================================

def get_trades(stock_code: str) -> list[Trade]:
    conn = _connect()
    rows = conn.execute(
        "SELECT id, stock_code, trade_type, price, quantity, fee, trade_date, notes "
        "FROM trades WHERE stock_code=? ORDER BY trade_date",
        (stock_code,),
    ).fetchall()
    conn.close()
    return [Trade(id=r["id"], stock_code=r["stock_code"], trade_type=r["trade_type"],
                  price=r["price"], quantity=r["quantity"], fee=r["fee"],
                  trade_date=r["trade_date"], notes=r["notes"]) for r in rows]


def get_all_trades() -> list[Trade]:
    conn = _connect()
    rows = conn.execute(
        "SELECT id, stock_code, trade_type, price, quantity, fee, trade_date, notes "
        "FROM trades ORDER BY trade_date"
    ).fetchall()
    conn.close()
    return [Trade(id=r["id"], stock_code=r["stock_code"], trade_type=r["trade_type"],
                  price=r["price"], quantity=r["quantity"], fee=r["fee"],
                  trade_date=r["trade_date"], notes=r["notes"]) for r in rows]


def add_trade(trade: Trade) -> Trade:
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO trades (stock_code, trade_type, price, quantity, fee, trade_date, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (trade.stock_code, trade.trade_type, trade.price, trade.quantity,
         trade.fee, trade.trade_date, trade.notes),
    )
    conn.commit()
    trade.id = cur.lastrowid
    conn.close()
    return trade


def update_trade(trade: Trade) -> None:
    conn = _connect()
    conn.execute(
        "UPDATE trades SET trade_type=?, price=?, quantity=?, fee=?, trade_date=?, notes=? "
        "WHERE id=?",
        (trade.trade_type, trade.price, trade.quantity, trade.fee,
         trade.trade_date, trade.notes, trade.id),
    )
    conn.commit()
    conn.close()


def delete_trade(trade_id: int) -> None:
    conn = _connect()
    conn.execute("DELETE FROM trades WHERE id=?", (trade_id,))
    conn.commit()
    conn.close()


def get_position_summary(stock_code: str) -> dict:
    """计算某股票的持仓摘要: 持仓量, 持仓成本, 总买入额, 总卖出额"""
    trades = get_trades(stock_code)
    total_buy_qty = 0
    total_buy_amt = 0.0
    total_sell_qty = 0
    total_sell_amt = 0.0

    for t in trades:
        if t.trade_type == TradeType.BUY.value:
            total_buy_qty += t.quantity
            total_buy_amt += t.price * t.quantity + t.fee
        else:
            total_sell_qty += t.quantity
            total_sell_amt += t.price * t.quantity - t.fee

    hold_qty = total_buy_qty - total_sell_qty
    if total_buy_qty > 0:
        avg_cost = total_buy_amt / total_buy_qty
    else:
        avg_cost = 0.0

    return {
        "hold_qty": hold_qty,
        "avg_cost": round(avg_cost, 3),
        "total_buy_amt": round(total_buy_amt, 2),
        "total_sell_amt": round(total_sell_amt, 2),
        "total_buy_qty": total_buy_qty,
        "total_sell_qty": total_sell_qty,
    }


def get_first_buy_date(stock_code: str) -> Optional[str]:
    """获取首次买入日期"""
    conn = _connect()
    row = conn.execute(
        "SELECT MIN(trade_date) as first_date FROM trades WHERE stock_code=? AND trade_type='buy'",
        (stock_code,),
    ).fetchone()
    conn.close()
    return row["first_date"] if row else None


# ============================================================
# 提醒禁用 (AlertDisabled) CRUD
# ============================================================

def is_alert_disabled(stock_code: str, alert_type: str = AlertType.ALL.value) -> bool:
    conn = _connect()
    row = conn.execute(
        "SELECT 1 FROM alerts_disabled WHERE stock_code=? AND alert_type IN (?, 'all')",
        (stock_code, alert_type),
    ).fetchone()
    conn.close()
    return row is not None


def disable_alert(stock_code: str, alert_type: str = AlertType.ALL.value) -> None:
    conn = _connect()
    conn.execute(
        "INSERT OR REPLACE INTO alerts_disabled (stock_code, alert_type, disabled_at) "
        "VALUES (?, ?, datetime('now','localtime'))",
        (stock_code, alert_type),
    )
    conn.commit()
    conn.close()


def enable_alert(stock_code: str, alert_type: str = AlertType.ALL.value) -> None:
    conn = _connect()
    conn.execute(
        "DELETE FROM alerts_disabled WHERE stock_code=? AND alert_type IN (?, 'all')",
        (stock_code, alert_type),
    )
    conn.commit()
    conn.close()


# ============================================================
# 交易纪律 (DisciplineRule) CRUD
# ============================================================

def get_discipline_rule(stock_code: str = "") -> Optional[DisciplineRule]:
    conn = _connect()
    row = conn.execute(
        "SELECT id, stock_code, rule_text FROM discipline_rules "
        "WHERE stock_code=? OR (stock_code='' AND ?='') "
        "ORDER BY CASE WHEN stock_code='' THEN 1 ELSE 0 END LIMIT 1",
        (stock_code, stock_code),
    ).fetchone()
    conn.close()
    if row:
        return DisciplineRule(id=row["id"], stock_code=row["stock_code"],
                              rule_text=row["rule_text"])
    return None


def save_discipline_rule(stock_code: str, rule_text: str) -> DisciplineRule:
    conn = _connect()
    # upsert: delete existing, then insert
    conn.execute(
        "DELETE FROM discipline_rules WHERE stock_code=?",
        (stock_code,),
    )
    cur = conn.execute(
        "INSERT INTO discipline_rules (stock_code, rule_text) VALUES (?, ?)",
        (stock_code, rule_text),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return DisciplineRule(id=rid, stock_code=stock_code, rule_text=rule_text)


# ============================================================
# 设置 (Settings) CRUD
# ============================================================

def get_setting(key: str, default: str = "") -> str:
    conn = _connect()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    conn = _connect()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, value),
    )
    conn.commit()
    conn.close()


# ============================================================
# 手动止盈止损设置 (Manual Alert Price)
# ============================================================

def get_manual_alert(code: str) -> dict:
    """
    获取某股票的手动止盈止损设置
    返回: {
        sl_active: bool, sl_price: float,
        tp_active: bool, tp_price: float,
    }
    """
    sl_active = get_setting(f"manual_sl_active_{code}", "0") == "1"
    sl_price_str = get_setting(f"manual_sl_{code}", "0")
    tp_active = get_setting(f"manual_tp_active_{code}", "0") == "1"
    tp_price_str = get_setting(f"manual_tp_{code}", "0")

    return {
        "sl_active": sl_active,
        "sl_price": float(sl_price_str) if sl_price_str else 0.0,
        "tp_active": tp_active,
        "tp_price": float(tp_price_str) if tp_price_str else 0.0,
    }


def set_manual_alert(
    code: str,
    sl_active: bool = False,
    sl_price: float = 0.0,
    tp_active: bool = False,
    tp_price: float = 0.0,
) -> None:
    """设置手动止盈止损（写入数据库，持久化）"""
    set_setting(f"manual_sl_active_{code}", "1" if sl_active else "0")
    set_setting(f"manual_sl_{code}", str(sl_price))
    set_setting(f"manual_tp_active_{code}", "1" if tp_active else "0")
    set_setting(f"manual_tp_{code}", str(tp_price))


def clear_manual_alert(code: str, field: str = "all") -> None:
    """清除手动止盈止损设置
    field: 'sl' | 'tp' | 'all'
    """
    if field in ("sl", "all"):
        set_setting(f"manual_sl_active_{code}", "0")
        set_setting(f"manual_sl_{code}", "0")
    if field in ("tp", "all"):
        set_setting(f"manual_tp_active_{code}", "0")
        set_setting(f"manual_tp_{code}", "0")


# ============================================================
# 股票名称映射 (stock_names) — 持久化全市场名称，减少API调用
# ============================================================

def get_stock_name(code: str) -> Optional[str]:
    """根据代码查名称，不存在返回 None"""
    conn = _connect()
    row = conn.execute(
        "SELECT name FROM stock_names WHERE code=?", (code,)
    ).fetchone()
    conn.close()
    return row["name"] if row else None


def search_stock_names(keyword: str, limit: int = 20) -> list[dict]:
    """在本地库中按代码或名称模糊搜索"""
    conn = _connect()
    pattern = f"%{keyword}%"
    rows = conn.execute(
        "SELECT code, name FROM stock_names "
        "WHERE code LIKE ? OR name LIKE ? "
        "LIMIT ?",
        (pattern, pattern, limit),
    ).fetchall()
    conn.close()
    return [{"code": r["code"], "name": r["name"]} for r in rows]


def get_stock_names_count() -> int:
    """获取已缓存的名称数量"""
    conn = _connect()
    row = conn.execute("SELECT COUNT(*) as cnt FROM stock_names").fetchone()
    conn.close()
    return row["cnt"] if row else 0


def save_stock_names_batch(names: list[dict]) -> int:
    """批量保存名称映射 [{code, name}, ...]，返回新增/更新数量"""
    conn = _connect()
    count = 0
    for item in names:
        cur = conn.execute(
            "INSERT OR REPLACE INTO stock_names (code, name, updated_at) "
            "VALUES (?, ?, datetime('now','localtime'))",
            (item["code"], item["name"]),
        )
        count += cur.rowcount
    conn.commit()
    conn.close()
    return count


def update_stock_name(code: str, name: str) -> None:
    """更新单条名称"""
    conn = _connect()
    conn.execute(
        "INSERT OR REPLACE INTO stock_names (code, name, updated_at) "
        "VALUES (?, ?, datetime('now','localtime'))",
        (code, name),
    )
    conn.commit()
    conn.close()


# ============================================================
# K线数据 (klines) — 持久化历史K线，减少API调用
# ============================================================

def save_klines_batch(klines: list) -> int:
    """批量 upsert K线数据 [{code, date, open, high, low, close, volume, period}, ...]
    返回实际写入行数
    UNIQUE(code, date, period) 保证同一天同一周期只有一条记录
    """
    if not klines:
        return 0
    conn = _connect()
    count = 0
    for k in klines:
        cur = conn.execute(
            "INSERT OR REPLACE INTO klines (code, date, open, high, low, close, volume, period) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (k["code"], k["date"], k["open"], k["high"], k["low"],
             k["close"], k.get("volume", 0), k.get("period", "daily")),
        )
        count += cur.rowcount
    conn.commit()
    conn.close()
    return count


def get_klines(
    code: str,
    period: str = "daily",
    days: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    """从DB获取K线数据，按日期升序返回
    返回: [{code, date, open, high, low, close, volume, period}, ...]
    """
    conn = _connect()
    query = (
        "SELECT code, date, open, high, low, close, volume, period "
        "FROM klines WHERE code=? AND period=? "
    )
    params = [code, period]

    if start_date:
        query += "AND date >= ? "
        params.append(start_date)
    if end_date:
        query += "AND date <= ? "
        params.append(end_date)

    query += "ORDER BY date ASC"

    if days is not None:
        # 用子查询取最后N条
        query = (
            "SELECT code, date, open, high, low, close, volume, period "
            "FROM (SELECT * FROM klines WHERE code=? AND period=? "
        )
        if start_date:
            query += "AND date >= ? "
        if end_date:
            query += "AND date <= ? "
        query += "ORDER BY date DESC LIMIT ?) ORDER BY date ASC"
        params.append(days)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [{
        "code": r["code"], "date": r["date"],
        "open": r["open"], "high": r["high"],
        "low": r["low"], "close": r["close"],
        "volume": r["volume"], "period": r["period"],
    } for r in rows]


def get_latest_kline_date(code: str, period: str = "daily") -> str | None:
    """获取某股票某周期最新的K线日期"""
    conn = _connect()
    row = conn.execute(
        "SELECT MAX(date) as latest FROM klines WHERE code=? AND period=?",
        (code, period),
    ).fetchone()
    conn.close()
    return row["latest"] if row else None


def get_kline_count(code: str = "", period: str = "daily") -> int:
    """获取K线数量，可指定股票和周期"""
    conn = _connect()
    if code:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM klines WHERE code=? AND period=?",
            (code, period),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM klines"
        ).fetchone()
    conn.close()
    return row["cnt"] if row else 0


# ============================================================
# 分钟K线 (klines_minute) — 1min / 60min 级别
# ============================================================

def save_klines_minute_batch(klines: list) -> int:
    """批量 upsert 分钟K线 [{code, timestamp, open, high, low, close, volume, period}, ...]
    UNIQUE(code, timestamp, period) 去重
    返回写入行数
    """
    if not klines:
        return 0
    conn = _connect()
    count = 0
    for k in klines:
        cur = conn.execute(
            "INSERT OR REPLACE INTO klines_minute "
            "(code, timestamp, open, high, low, close, volume, period) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (k["code"], k["timestamp"], k.get("open", k.get("price", 0)),
             k.get("high", k.get("price", 0)), k.get("low", k.get("price", 0)),
             k.get("close", k.get("price", 0)), k.get("volume", 0),
             k.get("period", "1min")),
        )
        count += cur.rowcount
    conn.commit()
    conn.close()
    return count


def get_klines_minute(
    code: str,
    period: str = "1min",
    minutes: int | None = None,
    start: str | None = None,
    end: str | None = None,
) -> list[dict]:
    """从DB获取分钟K线，按时间升序返回
    返回: [{code, timestamp, open, high, low, close, volume, period}, ...]
    """
    conn = _connect()
    query = (
        "SELECT code, timestamp, open, high, low, close, volume, period "
        "FROM klines_minute WHERE code=? AND period=? "
    )
    params = [code, period]

    if start:
        query += "AND timestamp >= ? "
        params.append(start)
    if end:
        query += "AND timestamp <= ? "
        params.append(end)

    query += "ORDER BY timestamp ASC"

    if minutes is not None:
        query += " LIMIT ?"
        params.append(minutes)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [{
        "code": r["code"], "timestamp": r["timestamp"],
        "open": r["open"], "high": r["high"],
        "low": r["low"], "close": r["close"],
        "volume": r["volume"], "period": r["period"],
    } for r in rows]


def get_latest_minute_timestamp(code: str, period: str = "1min") -> str | None:
    """获取某股票分钟K线的最新时间戳"""
    conn = _connect()
    row = conn.execute(
        "SELECT MAX(timestamp) as latest FROM klines_minute WHERE code=? AND period=?",
        (code, period),
    ).fetchone()
    conn.close()
    return row["latest"] if row else None


def delete_old_minute_klines(code: str, before_date: str) -> int:
    """删除指定日期之前的分钟K线（数据清理用）"""
    conn = _connect()
    cur = conn.execute(
        "DELETE FROM klines_minute WHERE code=? AND timestamp < ?",
        (code, before_date),
    )
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    return deleted

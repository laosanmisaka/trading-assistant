"""测试共用 fixtures"""

import os
import sys
import tempfile
import pytest

# 确保项目根在 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def temp_db(monkeypatch):
    """使用临时数据库，测试后自动清理（不影响项目真实数据库）"""
    from data.database import init_db

    # 创建临时数据库文件
    fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="test_trading_")
    os.close(fd)

    # 猴子补丁 _get_path，让所有 DB 操作指向临时文件
    def _tmp_get_path():
        return tmp_path

    monkeypatch.setattr("data.database._get_path", _tmp_get_path)

    init_db()
    yield tmp_path

    # 清理临时文件
    if os.path.exists(tmp_path):
        os.remove(tmp_path)


@pytest.fixture
def db_conn(temp_db):
    """提供已初始化的数据库连接"""
    from data.database import _connect
    conn = _connect()
    yield conn
    conn.close()

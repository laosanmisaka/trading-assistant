"""测试共用 fixtures"""

import os
import sys
import tempfile
import pytest

# 确保项目根在 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============================================================
# 网络测试开关
# ------------------------------------------------------------
# 依赖真实外网（AKShare / 通达信）的测试统一打 @pytest.mark.network，
# 默认跳过，保证测试套件在离线与 CI 环境可稳定运行。
# 需要验证真实数据源时: pytest --run-network
# ============================================================

def pytest_addoption(parser):
    parser.addoption(
        "--run-network",
        action="store_true",
        default=False,
        help="运行标记为 network 的测试（需要真实外网访问）",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "network: 需要真实网络访问（默认跳过，用 --run-network 启用）",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-network"):
        return
    skip_network = pytest.mark.skip(
        reason="需要真实网络访问；用 --run-network 启用")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip_network)


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


@pytest.fixture
def no_network(monkeypatch):
    """切断网络名称同步，使测试不依赖外网

    用于「测试意图与网络无关、但间接触发全市场名称同步」的用例，
    避免 3 次重试 + 递增 sleep 把测试拖到超时。
    """
    import data.market_data as md
    monkeypatch.setattr(md, "sync_stock_names_from_api", lambda: 0)
    return 0

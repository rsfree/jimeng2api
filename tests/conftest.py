import os
import tempfile

# 必须在导入 app.* 之前设置：settings 是模块级单例
_TMP = tempfile.mkdtemp(prefix="jimeng2api-test-")
os.environ["JIMENG2API_DATA"] = _TMP
os.environ["JIMENG2API_DB"] = os.path.join(_TMP, "test.db")
os.environ["JIMENG2API_COORDINATOR"] = "0"
os.environ["JIMENG2API_ADMIN_KEY"] = "sk-test-admin"

# 音色表是运营数据，放在 data/ 下（受 JIMENG2API_DATA 控制）。测试用临时目录时
# 也要带上一份，否则 tts 只能加载内置的 2 个音色 —— 那样等于没测到真实路径。
import shutil  # noqa: E402
from pathlib import Path  # noqa: E402

_REPO_DATA = Path(__file__).resolve().parents[1] / "data"
for _name in ("voices.json", "voice_meta.json"):
    _src = _REPO_DATA / _name
    if _src.exists():
        shutil.copy(_src, Path(_TMP) / _name)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import create_app  # noqa: E402

ADMIN = "sk-test-admin"


@pytest.fixture(scope="session")
def client():
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_tables():
    db.init()
    for table in ("accounts", "tasks", "request_logs", "upstream_logs",
                  "model_redirects"):
        db.execute(f"DELETE FROM {table}")
    yield


@pytest.fixture
def auth():
    return {"Authorization": f"Bearer {ADMIN}"}

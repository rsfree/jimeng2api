import os
import tempfile

# 必须在导入 app.* 之前设置：settings 是模块级单例
_TMP = tempfile.mkdtemp(prefix="jimeng2api-test-")
os.environ["JIMENG2API_DATA"] = _TMP
os.environ["JIMENG2API_DB"] = os.path.join(_TMP, "test.db")
os.environ["JIMENG2API_COORDINATOR"] = "0"
os.environ["JIMENG2API_ADMIN_KEY"] = "sk-test-admin"

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

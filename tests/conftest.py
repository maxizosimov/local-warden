import os

os.environ["WARDEN_LOCAL_URL"] = "http://127.0.0.1:9/v1"  # force rule parser in tests

import pytest

from warden.hub import Hub
from warden.merchants import REGISTRY


@pytest.fixture
def hub(tmp_path):
    for p in REGISTRY["demo-store"].products.values():
        p.price = p.list_price
    return Hub(tmp_path)

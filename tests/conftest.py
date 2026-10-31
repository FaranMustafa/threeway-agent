import pytest

from threeway.db import Store
from threeway.seed import seed


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path / "test.db")
    seed(s)
    return s

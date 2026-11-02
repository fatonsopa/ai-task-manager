import pytest

from tests.domains.helpers import Rig


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path / "core.sqlite", host_limit=2, project_limit=2)

import pytest

from .helpers import ExistingProvider, ExistingRig


@pytest.fixture
def provider():
    value = ExistingProvider()
    try:
        yield value
    finally:
        value.close()


@pytest.fixture
def rig(tmp_path, provider):
    return ExistingRig(tmp_path / "owned", provider)

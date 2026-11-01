from pathlib import Path

import pytest

from bimforge.devtools.sample_plan import build_sample_plan


@pytest.fixture(scope="session")
def sample_dxf(tmp_path_factory) -> Path:
    return build_sample_plan(tmp_path_factory.mktemp("plans") / "two_rooms.dxf")

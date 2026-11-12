import pytest

from govee_win11 import i18n, protocol


@pytest.fixture(autouse=True)
def _identity_color_cal():
    """颜色校准 is module state (protocol.calibrate): every test starts and ends at 100 %."""
    protocol.set_color_cal(100, 100, 100)
    yield
    protocol.set_color_cal(100, 100, 100)


@pytest.fixture(autouse=True)
def _chinese_system_language(monkeypatch):
    """language "auto" follows the Windows display language: pin it to Chinese so the
    suite does not depend on the machine (language tests override this)."""
    monkeypatch.setattr(i18n, "system_language", lambda: "zh")

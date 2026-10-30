import os
import subprocess
import sys


def test_desktop_smoke_without_credentials(tmp_path):
    environment = dict(os.environ, FPTM_DATA_DIR=str(tmp_path / "must-not-be-used"))
    result = subprocess.run(
        [sys.executable, "-m", "funpay_manager", "--smoke-test"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "must-not-be-used").exists()

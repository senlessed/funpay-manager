from PySide6.QtCore import QCoreApplication, QEventLoop, QLockFile, QTimer

from funpay_manager.app.worker import BackendThread
from funpay_manager.utils.diagnostics import DiagnosticError, FailureCode


def test_worker_failure_isolation_and_idempotent_shutdown():
    app = QCoreApplication.instance() or QCoreApplication([])
    worker = BackendThread()
    wait = QEventLoop()
    errors, results, details = [], [], []
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(wait.quit)

    async def fail():
        raise DiagnosticError(
            "sensitive exception body must not appear in diagnostics", code=FailureCode.FP_HTTP_403
        )

    async def succeed():
        return 42

    async def untrusted_failure():
        raise ValueError("sensitive arbitrary error body")

    def ready():
        worker.submit("failure", fail)
        worker.submit("untrusted", untrusted_failure)
        worker.submit("success", succeed)

    def completed(name, result):
        results.append(result)
        worker.stop()

    worker.ready.connect(ready)
    worker.failed.connect(errors.append)
    worker.operation_failed.connect(lambda name, code: details.append((name, code)))
    worker.completed.connect(completed)
    worker.finished.connect(wait.quit)
    deadline.start(5000)
    worker.start()
    wait.exec()
    worker.stop()
    assert worker.wait(5000)
    worker.stop()
    app.processEvents()
    assert results == [42]
    assert len(errors) == 2
    assert all("sensitive" not in error for error in errors)
    assert details == [("failure", "fp_http_403"), ("untrusted", "unexpected")]


def test_stop_before_backend_ready():
    worker = BackendThread()
    worker.stop()
    worker.start()
    assert worker.wait(5000)
    worker.stop()


def test_single_instance_lock_released(tmp_path):
    path = str(tmp_path / "app.lock")
    first, second = QLockFile(path), QLockFile(path)
    first.setStaleLockTime(0)
    second.setStaleLockTime(0)
    assert first.tryLock(0)
    assert not second.tryLock(0)
    first.unlock()
    assert second.tryLock(0)
    second.unlock()

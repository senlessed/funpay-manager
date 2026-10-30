from PySide6.QtCore import QCoreApplication, QEventLoop, QLockFile, QTimer

from funpay_manager.app.worker import BackendThread


def test_worker_failure_isolation_and_idempotent_shutdown():
    app = QCoreApplication.instance() or QCoreApplication([])
    worker = BackendThread()
    wait = QEventLoop()
    errors, results = [], []
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(wait.quit)

    async def fail():
        raise ValueError("sensitive exception body must not appear in diagnostics")

    async def succeed():
        return 42

    def ready():
        worker.submit("failure", fail)
        worker.submit("success", succeed)

    def completed(name, result):
        results.append(result)
        worker.stop()

    worker.ready.connect(ready)
    worker.failed.connect(errors.append)
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
    assert len(errors) == 1
    assert "sensitive" not in errors[0]


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

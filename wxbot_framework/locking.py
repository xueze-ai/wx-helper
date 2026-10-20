"""进程锁：同一个框架目录同时只运行一个 main.py。"""
from pathlib import Path


class InstanceLock:
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def acquire(self):
        import msvcrt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            handle.close()
            raise RuntimeError("这个目录的机器人已在运行，请先停止另一个 main.py") from error
        self.file = handle
        return self

    def close(self):
        if self.file is not None:
            import msvcrt
            self.file.seek(0)
            try:
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self.file.close()
                self.file = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.close()

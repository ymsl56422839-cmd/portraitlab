"""Explicit, verified downloads for the two classic processing models."""
import hashlib
from pathlib import Path
import threading
import urllib.request


class ModelSetup:
    def __init__(self, engine, specs, checked):
        self.engine, self.specs, self.checked = engine, specs, checked
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.state = {"status": "idle", "message": "", "done": 0, "total": len(specs)}

    def status(self):
        with self.lock:
            return {"models": self.engine.status(), "installation": dict(self.state)}

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return dict(self.state)
            self.stop.clear()
            self.state = {"status": "running", "message": "连接官方模型来源…", "done": 0, "total": len(self.specs)}
            self.thread = threading.Thread(target=self._run, name="portraitlab-models", daemon=True)
            self.thread.start()
            return dict(self.state)

    def _run(self):
        try:
            for index, (key, spec) in enumerate(self.specs.items()):
                if self.stop.is_set():
                    raise InterruptedError("下载已停止。")
                path = self.checked(self.engine.models_dir / key / spec["filename"])
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == spec["sha256"]:
                    with self.lock:
                        self.state.update(done=index + 1)
                    continue
                partial = self.checked(path.with_suffix(path.suffix + ".part"))
                try:
                    sha = hashlib.sha256()
                    request = urllib.request.Request(spec["url"], headers={"User-Agent": "PortraitLab-model-setup"})
                    with urllib.request.urlopen(request, timeout=45) as response, partial.open("wb") as stream:
                        total = int(response.headers.get("Content-Length") or 0)
                        current = 0
                        while chunk := response.read(512 * 1024):
                            if self.stop.is_set():
                                raise InterruptedError("下载已停止。")
                            stream.write(chunk)
                            sha.update(chunk)
                            current += len(chunk)
                            with self.lock:
                                self.state.update(message=f"正在下载 {key}", bytes=current, total_bytes=total)
                    if sha.hexdigest() != spec["sha256"]:
                        raise ValueError(f"{key} 校验失败，请重新下载。")
                    partial.replace(path)
                finally:
                    partial.unlink(missing_ok=True)
                with self.lock:
                    self.state.update(done=index + 1)
            with self.lock:
                self.state.update(status="completed", message="经典处理模型已下载并通过 SHA-256 校验。")
        except Exception as exc:
            with self.lock:
                self.state.update(status="cancelled" if self.stop.is_set() else "failed", message=str(exc))

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)

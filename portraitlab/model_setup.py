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
        self.verified = {}
        self.state = {"status": "idle", "phase": "idle", "message": "", "done": 0, "total": len(specs)}

    @staticmethod
    def _signature(path):
        try:
            stat = path.stat()
            return (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        except OSError:
            return None

    def status(self):
        with self.lock:
            models = self.engine.status()
            records = models.get("models", {}) or {}
            for key, record in records.items():
                path = self.engine.models_dir / key / self.specs[key]["filename"]
                signature = self._signature(path)
                # Adopt an engine verification once, then invalidate it if the file changes.
                if key not in self.verified and record.get("verified") and signature:
                    self.verified[key] = signature
                record["verified"] = bool(record.get("exists") and signature and self.verified.get(key) == signature)
            verified = all(record.get("verified") for record in records.values()) if records else len(self.verified) == len(self.specs)
            models["preparation_state"] = "missing" if not models.get("ready") else "ready" if verified else "downloaded"
            return {"models": models, "installation": dict(self.state)}

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return dict(self.state)
            self.stop.clear()
            self.state = {"status": "running", "phase": "checking", "message": "检查本地模型…", "done": 0, "total": len(self.specs)}
            self.thread = threading.Thread(target=self._run, name="portraitlab-models", daemon=True)
            self.thread.start()
            return dict(self.state)

    def _run(self):
        try:
            for index, (key, spec) in enumerate(self.specs.items()):
                if self.stop.is_set():
                    raise InterruptedError("模型准备已停止。")
                path = self.checked(self.engine.models_dir / key / spec["filename"])
                path.parent.mkdir(parents=True, exist_ok=True)
                with self.lock:
                    self.state.update(phase="verifying", message=f"正在校验 {key}", bytes=0, total_bytes=0)
                digest = hashlib.sha256()
                before = self._signature(path)
                if path.is_file():
                    with path.open("rb") as stream:
                        while chunk := stream.read(1024 * 1024):
                            if self.stop.is_set():
                                raise InterruptedError("模型准备已停止。")
                            digest.update(chunk)
                if before and before == self._signature(path) and digest.hexdigest() == spec["sha256"]:
                    with self.lock:
                        self.verified[key] = before
                        self.state.update(done=index + 1)
                    continue
                partial = self.checked(path.with_suffix(path.suffix + ".part"))
                try:
                    sha = hashlib.sha256()
                    with self.lock:
                        self.state.update(phase="downloading", message=f"正在下载 {key}", bytes=0, total_bytes=0)
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
                    self.verified[key] = self._signature(path)
                    self.state.update(done=index + 1)
            with self.lock:
                self.state.update(status="completed", phase="ready", message="经典处理模型已就绪，SHA-256 校验通过。", bytes=0, total_bytes=0)
        except Exception as exc:
            with self.lock:
                phase = "cancelled" if self.stop.is_set() else "failed"
                self.state.update(status=phase, phase=phase, message=str(exc), bytes=0, total_bytes=0)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)

from __future__ import annotations
import json
import os
import secrets
import threading
import contextlib
import fcntl
from pathlib import Path
from cryptography.fernet import Fernet
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='ORE_', extra='ignore')
    state_dir: Path = Path('.ore')
    database_url: str | None = None
    auth_token: str | None = None
    codex_bin: str | None = None
    browser_executable: str | None = None
    browser_backend: str = "playwright"
    desktop_image: str = "ore-desktop:0.2.0rc1"
    desktop_host_state_dir: Path | None = None
    desktop_resource_mode: str = "cgroup"
    desktop_memory: str = "4g"
    browser_headless: bool = True
    browser_proxy: str | None = None
    host: str = '127.0.0.1'
    port: int = 8765
    max_workers: int = 5
    scheduler_global_limit: int = 64
    scheduler_interval_seconds: float = 15.0
    pool_token: str | None = None
    pool_max_executors: int = 64
    pool_idle_seconds: float = 120.0
    execution_backend: str = 'local'
    executor_enrollment_token: str | None = None
    executor_network_zone: str = 'default'
    executor_lease_seconds: int = 120
    def prepare(self):
        self.state_dir = self.state_dir.resolve()
        self.state_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
        if not self.database_url:
            self.database_url = f'sqlite:///{self.state_dir / "ore.db"}'
        path = self.state_dir/'operator.token'
        if not self.auth_token:
            if path.exists(): self.auth_token = path.read_text().strip()
            else:
                self.auth_token = secrets.token_urlsafe(32)
                fd = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'w') as stream: stream.write(self.auth_token)
        for name in ['vault','staging','profiles','agent-workspaces','reports']:
            (self.state_dir/name).mkdir(exist_ok=True,mode=0o700)
        return self

class SecretStore:
    """Encrypted local secrets; key supplied separately in managed deployments."""
    def __init__(self,state_dir: Path):
        self.path = state_dir/'secrets.enc'
        self.lock=threading.RLock()
        self.lock_path=state_dir/'secrets.lock'
        key_path = state_dir/'secret.key'
        key = os.environ.get('ORE_SECRET_KEY')
        if key: key = key.encode()
        elif key_path.exists(): key = key_path.read_bytes()
        else:
            key = Fernet.generate_key()
            fd = os.open(key_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as stream: stream.write(key)
        self.cipher = Fernet(key)
    def _read(self):
        return json.loads(self.cipher.decrypt(self.path.read_bytes())) if self.path.exists() else {}
    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            fd=os.open(self.lock_path,os.O_RDWR|os.O_CREAT,0o600)
            try:
                fcntl.flock(fd,fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)
    def set(self,name: str,value: str):
        with self.transaction():self._set(name,value)
    def _set(self,name: str,value: str):
        values = self._read(); values[name] = value
        data = self.cipher.encrypt(json.dumps(values).encode())
        tmp = self.path.with_suffix('.tmp')
        fd = os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'wb') as stream: stream.write(data)
        os.replace(tmp,self.path)
    def get(self,name: str):
        if name.startswith('env:'): return os.environ.get(name[4:])
        return self._read().get(name)
    def names(self): return sorted(self._read())

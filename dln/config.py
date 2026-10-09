import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db: str = "data/dln.sqlite3"
    ip_secret: str = ""
    admin_user: str = "operator"
    admin_password: str = ""
    void_max_posts: int = 1000
    void_max_bytes: int = 1048576
    archive_days: int = 30
    thread_days: int = 30
    security_days: int = 7
    human_days: int = 30
    rate_per_minute: int = 30
    eth_address: str = ""

    def __post_init__(self):
        if len(self.ip_secret) < 32:
            raise ValueError("DLN_IP_SECRET must contain at least 32 characters")
        if self.admin_password and len(self.admin_password) < 16:
            raise ValueError("DLN_ADMIN_PASSWORD must contain at least 16 characters")
        for name in ("void_max_posts", "void_max_bytes", "archive_days", "thread_days",
                     "security_days", "human_days", "rate_per_minute"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.eth_address and not re.fullmatch(r"0x[0-9a-fA-F]{40}", self.eth_address):
            raise ValueError("DLN_ETH_ADDRESS must be a 20-byte hexadecimal address")

    @classmethod
    def from_env(cls):
        values = {}
        for name, field in cls.__dataclass_fields__.items():
            value = os.getenv("DLN_" + name.upper())
            if value is not None:
                values[name] = int(value) if field.type is int else value
        return cls(**values)

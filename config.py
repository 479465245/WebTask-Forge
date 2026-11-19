"""Runtime configuration supplied through environment variables."""

import os
from pathlib import Path


DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    env_file = Path(__file__).with_name(".env")
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "DATABASE_URL":
                DATABASE_URL = value.strip().strip("\"'")
                break
if not DATABASE_URL:
    raise RuntimeError("请设置 DATABASE_URL 环境变量后再启动应用。")

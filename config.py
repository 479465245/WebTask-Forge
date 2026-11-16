"""Runtime configuration supplied through environment variables."""

import os


DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("请设置 DATABASE_URL 环境变量后再启动应用。")
"""AppContext — 集中管理 Web 应用全局状态，替代散落的模块级变量。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class AppContext:
    """Web 应用全局上下文。单例模式，所有路由通过 ctx 访问共享状态。"""

    # ── 路径 ──
    root: str = ROOT
    data_dir: str = os.environ.get('CROSSPILOT_DATA_DIR') or os.path.join(ROOT, 'data')
    keys_path: str = os.environ.get('CROSSPILOT_KEYS_PATH') or os.path.join(ROOT, 'keys.json')
    keys_example: str | None = (
        os.path.join(ROOT, 'keys.example.json')
        if os.path.exists(os.path.join(ROOT, 'keys.example.json'))
        else None
    )
    upload_dir: str = ''

    def __post_init__(self):
        self.upload_dir = os.path.join(self.data_dir, 'uploads')
        os.makedirs(self.upload_dir, exist_ok=True)

    # ── 运行时状态 ──
    public_upload_times: dict[str, list[float]] = field(default_factory=dict)
    admin_sessions: set[str] = field(default_factory=set)

    # ── 限制配置 ──
    max_upload_mb: int = max(1, min(1024, int(os.environ.get('CROSSPILOT_MAX_UPLOAD_MB', '50'))))
    max_batch_files: int = max(1, min(100, int(os.environ.get('CROSSPILOT_MAX_BATCH_FILES', '20'))))
    public_upload_window_s: int = 3600
    public_upload_max_per_ip: int = 10

    def is_admin(self, token: str | None) -> bool:
        return bool(token and token in self.admin_sessions)


# 全局单例
ctx = AppContext()

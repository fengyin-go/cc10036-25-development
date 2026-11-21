"""业务路由汇总：固定路径的版本化路由在前，动态资源路由在后。"""
from __future__ import annotations

from app.routers import resource, versioning

ROUTERS = [versioning, resource]

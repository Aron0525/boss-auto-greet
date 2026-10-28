"""bossgreet —— 面向 BOSS 直聘的岗位扫描与打招呼自动化工具。

设计前提（与常见浏览器自动化项目的根本差异）：
  BOSS 前端能检测 Playwright 开启的调试域并在数秒内销毁页面，
  因此本包**不使用 Playwright**，读写页面全部走自研的极简 CDP 客户端
  （``bossgreet.core.cdp``）：只调 ``Runtime.evaluate`` 一类无副作用方法，
  绝不调用任何 ``*.enable``。详见 ``docs/ARCHITECTURE.md``。
"""
from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]

"""允许 `python -m bossgreet <命令>` 直接跑。"""
from __future__ import annotations

import sys

from bossgreet.cli import main

if __name__ == "__main__":
    sys.exit(main())

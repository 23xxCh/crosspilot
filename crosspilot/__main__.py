#!/usr/bin/env python3
"""CrossPilot 统一入口 — 全功能 CLI。"""
import sys

if __name__ == '__main__':
    from crosspilot.cli import main
    sys.exit(main())

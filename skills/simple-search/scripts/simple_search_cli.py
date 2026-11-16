#!/usr/bin/env python3
"""simple-search 命令行入口，可通过 --help 查看用法。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from simple_search.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

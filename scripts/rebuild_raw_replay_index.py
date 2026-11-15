"""停录、停止回放读取后，从 v2 原始 MCAP 原子重建必要索引；不改写原始文件。"""

import argparse
from pathlib import Path

from axile.server._native import _core


def main():
    """通过本机扩展获得互斥锁，再发布新的索引代次。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--account", required=True, type=int)
    args = parser.parse_args()
    print(_core.execution_replay_rebuild(str(args.root.resolve()), args.account))


if __name__ == "__main__":
    main()

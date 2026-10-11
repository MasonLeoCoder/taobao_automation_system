"""源码检出的统一入口，可从任意工作目录启动。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from ecommerce.cli.app import main

if __name__ == "__main__":
    raise SystemExit(main())

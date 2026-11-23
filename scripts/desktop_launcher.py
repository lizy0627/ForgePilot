"""Compatibility entry point for existing desktop commands and Windows bundles."""
from pathlib import Path
import sys

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent'))

from desktop import run

if __name__ == '__main__':
    raise SystemExit(run())

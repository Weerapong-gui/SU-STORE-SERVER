"""Put the migrated DB, slips and product images in place (runs in a throwaway order-api container
as root, because the data files belong to the container's root).

usage: python - <data_root> <stamp> < import_files.py
  <data_root>/incoming/orders.db            → <data_root>/su-order-api/orders.db
  <data_root>/incoming/su-order-api/{slips,product-images}/  → merged in, never overwriting
An existing orders.db (and its -wal/-shm) is kept beside the new one as *.replaced-<stamp>.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

MERGED_DIRS = ("slips", "product-images")


def import_files(data_root: Path, stamp: str) -> None:
    incoming, live = data_root / "incoming", data_root / "su-order-api"
    live.mkdir(parents=True, exist_ok=True)
    for name in ("orders.db", "orders.db-wal", "orders.db-shm"):
        current = live / name
        if current.exists():
            current.rename(live / f"{name}.replaced-{stamp}")
    shutil.copy2(incoming / "orders.db", live / "orders.db")
    for directory in MERGED_DIRS:
        source = incoming / "su-order-api" / directory
        if not source.is_dir():
            continue
        for path in source.rglob("*"):
            target = live / directory / path.relative_to(source)
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)


if __name__ == "__main__":
    import_files(Path(sys.argv[1]), sys.argv[2])
    print("[import] วาง DB สลิป และรูปสินค้าแล้ว")

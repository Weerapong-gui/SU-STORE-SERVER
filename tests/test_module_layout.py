"""Guards for how server modules share settings.

Settings that tests (and only tests) swap at runtime must be read as `config.NAME` /
`slips.trigger_ocr_async`. A `from core.config import DB_PATH` would copy the value at
import time, so swapping it would silently not reach that module.
"""
import ast
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
SWAPPABLE = {
    "config": {"DB_PATH", "SLIPS_DIR", "PRODUCT_IMAGES_DIR", "ORDER_API_TOKEN", "KHANTOK_QUOTA_100",
               "KHANTOK_QUOTA_50", "SLIDES_DIR", "ASSETS_DIR"},
    "slips": {"trigger_ocr_async"},
}


def _sources() -> list[Path]:
    return [SERVER / "order_api.py", *sorted((SERVER / "core").rglob("*.py")), *sorted((SERVER / "legacy_fp28").rglob("*.py")), *sorted((SERVER / "store").glob("*.py"))]


def test_swappable_settings_are_never_imported_by_name():
    offenders = []
    for path in _sources():
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            module = node.module.rsplit(".", 1)[-1]
            for alias in node.names:
                if alias.name in SWAPPABLE.get(module, set()):
                    offenders.append(f"{path.relative_to(SERVER)}:{node.lineno} imports {module}.{alias.name}")
    assert not offenders, "read these through the module instead:\n" + "\n".join(offenders)


def test_store_never_imports_legacy_fp28():
    offenders = []
    for path in sorted((SERVER / "store").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [node.module or ""] if isinstance(node, ast.ImportFrom) else (
                [a.name for a in node.names] if isinstance(node, ast.Import) else [])
            offenders += [f"{path.relative_to(SERVER)}:{node.lineno} imports {n}" for n in names if n.split(".")[0] == "legacy_fp28"]
    assert not offenders, "move what the store needs into core/ instead:\n" + "\n".join(offenders)

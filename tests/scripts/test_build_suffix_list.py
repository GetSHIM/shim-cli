from pathlib import Path
from runpy import run_path

ROOT = Path(__file__).resolve().parents[2]


def test_render_reproduces_the_committed_suffix_module() -> None:
    generator = run_path(str(ROOT / "scripts" / "build_suffix_list.py"))
    committed = run_path(str(ROOT / "src" / "shim_cli" / "guard" / "suffixes.py"))

    assert generator["render"](sorted(committed["_SUFFIXES"]), committed["SOURCE"]) == (
        ROOT / "src" / "shim_cli" / "guard" / "suffixes.py"
    ).read_text(encoding="utf-8")

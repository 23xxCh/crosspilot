"""Package-import and legacy entrypoint regression tests."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
LEGACY_TOP_LEVEL_MODULES = {
    "adapters",
    "concurrency",
    "dmx_client",
    "export_amazon_cn_review",
    "model_provider",
    "pipeline_log",
    "pipelines",
    "process_amazon",
    "process_ebay_tk",
    "providers",
    "services",
}


def test_only_legacy_bootstrap_may_modify_sys_path() -> None:
    allowed = ROOT / "scripts" / "_bootstrap.py"
    candidates = [
        *ROOT.joinpath("crosspilot").rglob("*.py"),
        *ROOT.joinpath("scripts").rglob("*.py"),
        *ROOT.joinpath("web").rglob("*.py"),
        ROOT / "main_cli.py",
    ]
    offenders = [
        str(path.relative_to(ROOT))
        for path in candidates
        if path != allowed
        and "sys.path" in path.read_text(
            encoding="utf-8-sig",
            errors="replace",
        )
    ]

    assert offenders == []


def test_production_uses_package_qualified_internal_imports() -> None:
    offenders = []
    for folder in ("crosspilot", "scripts", "web"):
        for path in ROOT.joinpath(folder).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                imported = []
                if isinstance(node, ast.Import):
                    imported = [alias.name for alias in node.names]
                elif (
                    isinstance(node, ast.ImportFrom)
                    and node.level == 0
                    and node.module
                ):
                    imported = [node.module]
                for module in imported:
                    if module.split(".", 1)[0] in LEGACY_TOP_LEVEL_MODULES:
                        offenders.append(
                            f"{path.relative_to(ROOT)}:{node.lineno} "
                            f"{module}"
                        )

    assert offenders == []


def test_package_imports_work_without_scripts_on_pythonpath(
    tmp_path,
) -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys;"
                "import scripts.process_amazon;"
                "import scripts.process_ebay_tk;"
                "import web.app;"
                "import crosspilot.pipeline;"
                "assert 'process_amazon' not in sys.modules;"
                "assert 'pipelines' not in sys.modules"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_legacy_file_entrypoints_share_bootstrap(tmp_path) -> None:
    scripts = {
        "audit_amazon_image_safety.py",
        "export_amazon_cn_review.py",
        "apply_amazon_review_decisions.py",
        "build_amazon_delivery_report.py",
        "normalize_amazon_brand_titles.py",
        "remove_amazon_products.py",
        "rerun_amazon_titles.py",
    }

    for filename in sorted(scripts):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / filename),
                "--help",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"{filename} failed:\n{result.stdout}\n{result.stderr}"
        )

#!/usr/bin/env python3
"""Fail a build when release metadata or required runtime files are inconsistent."""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / 'crosspilot' / 'version.py'
REQUIRED_RUNTIME_FILES = (
    'Dockerfile',
    'docker-compose.yml',
    'CrossPilot.spec',
    'keys.example.json',
    'pyproject.toml',
    'uv.lock',
    '.github/workflows/canary.yml',
    '.github/workflows/release.yml',
    '.github/workflows/test.yml',
    'main_cli.py',
    'crosspilot/__init__.py',
    'crosspilot/__main__.py',
    'crosspilot/version.py',
    'scripts/__init__.py',
    'scripts/_bootstrap.py',
    'scripts/adapters/__init__.py',
    'scripts/adapters/amazon_tk.py',
    'scripts/adapters/base.py',
    'scripts/adapters/ebay_tk.py',
    'scripts/dmx_client.py',
    'scripts/pipeline_log.py',
    'scripts/process_amazon.py',
    'scripts/process_ebay_tk.py',
    'scripts/review_package/__init__.py',
    'scripts/review_package/assets.py',
    'scripts/review_package/exporter.py',
    'scripts/review_package/html_renderer.py',
    'scripts/review_package/rows.py',
    'scripts/review_package/storage.py',
    'scripts/review_package/translation.py',
    'scripts/pipelines/amazon_constants.py',
    'scripts/pipelines/amazon_delivery.py',
    'scripts/pipelines/amazon_io.py',
    'scripts/pipelines/amazon_image_safety/__init__.py',
    'scripts/pipelines/amazon_image_safety/assessment.py',
    'scripts/pipelines/amazon_image_safety/cache.py',
    'scripts/pipelines/amazon_image_safety/gate.py',
    'scripts/pipelines/amazon_image_safety/remediation.py',
    'scripts/pipelines/amazon_quality/__init__.py',
    'scripts/pipelines/amazon_quality/audit.py',
    'scripts/pipelines/amazon_quality/listing.py',
    'scripts/pipelines/amazon_quality/rules.py',
    'scripts/pipelines/amazon_quality/validation.py',
    'scripts/pipelines/amazon_runtime.py',
    'scripts/pipelines/amazon_stages.py',
    'scripts/pipelines/amazon_text/__init__.py',
    'scripts/pipelines/amazon_text/descriptions.py',
    'scripts/pipelines/amazon_text/listing_content.py',
    'scripts/pipelines/amazon_text/titles.py',
    'scripts/pipelines/ebay_shared.py',
    'scripts/pipelines/ebay_stages.py',
    'scripts/release_preflight.py',
    'scripts/services/__init__.py',
    'scripts/services/amazon_json.py',
    'scripts/services/constants.py',
    'scripts/services/review.py',
    'scripts/services/translate.py',
    'web/__init__.py',
    'web/app.py',
    'web/jobs.py',
    'web/runner.py',
    'web/store.py',
    'web/updater.py',
    'web/static/analytics.js',
    'web/static/app.js',
    'web/static/dashboard.js',
    'web/static/helpers.js',
    'web/static/index.html',
    'web/static/quality.js',
    'web/static/runtime.js',
    'web/static/settings.js',
    'web/static/styles.css',
    'web/static/tasks.js',
    'web/static/templates.js',
    'web/static/upload.js',
)
SEMVER_RE = re.compile(r'^\d+\.\d+\.\d+$')


def read_version() -> str:
    tree = ast.parse(VERSION_FILE.read_text(encoding='utf-8'), filename=str(VERSION_FILE))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == '__version__' for target in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    raise RuntimeError(f'{VERSION_FILE.relative_to(ROOT)} 未定义字符串 __version__')


def validate_release(tag: str = '', require_tracked: bool = False) -> list[str]:
    errors = []
    try:
        version = read_version()
    except Exception as exc:
        return [str(exc)]
    if not SEMVER_RE.fullmatch(version):
        errors.append(f'版本号必须是 x.y.z：{version!r}')

    pyproject = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    project = pyproject.get('project', {})
    dynamic = project.get('dynamic', [])
    hatch_path = pyproject.get('tool', {}).get('hatch', {}).get('version', {}).get('path')
    if 'version' not in dynamic or hatch_path != 'crosspilot/version.py':
        errors.append('pyproject.toml 必须从 crosspilot/version.py 动态读取版本')
    wheel_packages = (
        pyproject.get('tool', {})
        .get('hatch', {})
        .get('build', {})
        .get('targets', {})
        .get('wheel', {})
        .get('packages', [])
    )
    if set(wheel_packages) != {'crosspilot', 'scripts', 'web'}:
        errors.append('wheel 必须包含 crosspilot、scripts 和 web 三个运行包')

    normalized_tag = tag.removeprefix('v')
    if tag and normalized_tag != version:
        errors.append(f'发布标签 {tag!r} 与代码版本 {version!r} 不一致')

    missing = [path for path in REQUIRED_RUNTIME_FILES if not (ROOT / path).is_file()]
    if missing:
        errors.append('缺少关键运行文件：' + ', '.join(missing))

    if require_tracked:
        result = subprocess.run(
            ['git', 'ls-files', '--error-unmatch', '--', *REQUIRED_RUNTIME_FILES],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            tracked = set(
                subprocess.run(
                    ['git', 'ls-files'],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.splitlines()
            )
            untracked = [path for path in REQUIRED_RUNTIME_FILES if path not in tracked]
            errors.append('关键运行文件尚未被 Git 跟踪：' + ', '.join(untracked))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--tag', default='', help='Expected release tag, such as v2.0.0')
    parser.add_argument('--require-tracked', action='store_true')
    args = parser.parse_args()
    errors = validate_release(args.tag, args.require_tracked)
    if errors:
        print('Release preflight failed:', file=sys.stderr)
        for error in errors:
            print(f'- {error}', file=sys.stderr)
        return 1
    print(f'Release preflight passed: v{read_version()}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

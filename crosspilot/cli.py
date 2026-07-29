#!/usr/bin/env python3
"""CrossPilot 统一 CLI — 一键清洗，自动检测格式。"""
from __future__ import annotations

import argparse
import json
import sys
import os
import time

# Force UTF-8 output
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
if hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _load_json_object(path: str) -> dict:
    with open(path, encoding='utf-8-sig') as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f'JSON 顶层必须是对象: {path}')
    return value


def cmd_run(args: argparse.Namespace) -> int:
    """一键运行：自动检测格式，全流程清洗。"""
    from crosspilot.config import load_config, print_config
    from crosspilot.health import (
        print_health_report,
        run_configured_health_check,
    )
    from crosspilot.pipeline import PipelineRunner, detect_input

    # Load config
    cfg = load_config()
    if args.show_config:
        print_config()
        return 0

    # Detect input
    print(f'\nCrossPilot v{_get_version()}')
    info = detect_input(args.input)
    print(f'  File: {info["name"]}')
    print(f'  Type: {info["platform"].upper()} | {"JSON" if info["is_json"] else "XLSX"}')
    if info.get('row_count'):
        print(f'  Rows: {info["row_count"]}')

    # Health check
    if not args.skip_health:
        results = run_configured_health_check(cfg)
        all_ok = print_health_report(results)
        if not all_ok and not args.force:
            print('  Use --force to run anyway with degraded quality.')
            print('  Use --skip-health to skip health check.')
            return 1

    # Run pipeline
    t0 = time.time()
    runner = PipelineRunner()
    output = runner.run(
        args.input,
        text_only=args.text_only,
        image_only=args.image_only,
        max_rows=args.max_rows or 0,
    )
    elapsed = time.time() - t0

    print(f'\n  Done in {elapsed:.0f}s')
    print(f'  Output: {output}')

    # Auto-generate report
    if args.report or not args.no_report:
        _generate_report(output)

    return 0


def cmd_health(args: argparse.Namespace) -> int:
    """API 预检。"""
    from crosspilot.config import load_config
    from crosspilot.health import (
        print_health_report,
        run_configured_health_check,
    )

    cfg = load_config()
    results = run_configured_health_check(cfg)
    print_health_report(results)
    return 0 if all(r.ok for r in results) else 1


def cmd_audit(args: argparse.Namespace) -> int:
    """只读审计 Amazon 图片，并默认生成终审包。"""
    from scripts.audit_amazon_image_safety import audit_file

    product_ids = (
        {
            item.strip()
            for item in str(args.ids or '').split(',')
            if item.strip()
        }
        or None
    )
    result = audit_file(
        args.input,
        output_root=args.output_root,
        cache_path=args.cache,
        product_ids=product_ids,
        create_package=not args.no_package,
    )
    _print_json({
        'summary': result['summary'],
        'report_path': result['report_path'],
        'package_summary': result['package_summary'],
    })
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    """从 Amazon 回填表生成中文文案与全图片终审包。"""
    from scripts.review_package import export_review

    audit_by_product = {}
    if args.audit_data:
        audit_payload = _load_json_object(args.audit_data)
        candidate = audit_payload.get('audit_by_product')
        if candidate is None and isinstance(
            audit_payload.get('products'), dict
        ):
            candidate = audit_payload['products']
        if candidate is not None and not isinstance(candidate, dict):
            raise ValueError(
                '--audit-data 必须包含 audit_by_product 对象'
            )
        audit_by_product = candidate or {}

    quarantine_products = []
    if args.quarantine_manifest:
        quarantine_payload = _load_json_object(
            args.quarantine_manifest
        )
        candidate = quarantine_payload.get('products') or []
        if not isinstance(candidate, list):
            raise ValueError(
                '--quarantine-manifest 的 products 必须是数组'
            )
        quarantine_products = candidate

    summary = export_review(
        args.input,
        args.output_dir,
        translate_workers=args.translate_workers,
        download_workers=args.download_workers,
        audit_by_product=audit_by_product,
        quarantine_products=quarantine_products,
        shared_cache_dir=args.shared_cache_dir,
        translation_cache_path=args.translation_cache,
        run_id=args.run_id,
    )
    _print_json(summary)
    return 2 if (
        summary['translation_failures']
        or summary['image_failures']
    ) else 0


def cmd_apply(args: argparse.Namespace) -> int:
    """校验并应用终审包导出的人工审核决定。"""
    from scripts.apply_amazon_review_decisions import apply_decisions

    result = apply_decisions(
        args.formal_json,
        args.decisions_json,
        review_package=args.review_package,
        dry_run=args.dry_run,
    )
    _print_json(result)
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    """配置管理。"""
    from crosspilot.config import (
        load_config, reload_config, print_config, ROOT
    )

    if args.init:
        # Create .env from keys.json or template
        env_path = os.path.join(ROOT, '.env')
        if os.path.exists(env_path) and not args.force:
            print(f'  .env already exists: {env_path}')
            print('  Use --force to overwrite.')
            return 1

        # Try to migrate from keys.json
        keys_path = os.path.join(ROOT, 'keys.json')
        template = []
        template.append('# CrossPilot Configuration')
        template.append('# Generated by: crosspilot config --init')
        template.append('')
        template.append('# === API Keys ===')
        template.append(f'DEEPSEEK_KEY=')
        template.append(f'AGNES_KEY=')
        template.append('')
        template.append('# === Provider Selection ===')
        template.append('# deepseek | agnes')
        template.append('TEXT_PROVIDER=deepseek')
        template.append('# agnes | none')
        template.append('IMAGE_PROVIDER=agnes')
        template.append('')
        template.append('# === Performance ===')
        template.append('IMAGE_GEN_CONCURRENCY=5')
        template.append('TEXT_CONCURRENCY=100')
        template.append('REVIEW_CONCURRENCY=30')
        template.append('')
        template.append('# === Pipeline ===')
        template.append('# SKIP_IMAGE_GEN=false')
        template.append('# MAX_ROWS=0')
        template.append('')
        template.append('# === Output ===')
        template.append('# OUTPUT_REPORT=true')
        template.append('# REPORT_LANGUAGE=zh')

        with open(env_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(template) + '\n')
        print(f'  Created: {env_path}')
        print('  Edit this file to add your API keys and settings.')
        return 0

    # Show current config
    reload_config()
    print_config()
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """启动文件夹监听。"""
    from crosspilot.watcher import FolderWatcher
    print('CrossPilot Folder Watch Mode')
    print(f'  Drop files into watch/input/')
    print(f'  Results appear in watch/output/')
    print(f'  Press Ctrl+C to stop.\n')
    w = FolderWatcher(args.dir)
    try:
        w.start()
    except KeyboardInterrupt:
        w.stop()
        print('\nStopped.')
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """启动 Web 平台。"""
    import uvicorn
    port = args.port or 8765
    host = args.host or '127.0.0.1'
    print(f'CrossPilot Web: http://{host}:{port}')
    uvicorn.run('web.app:app', host=host, port=port, reload=args.dev)
    return 0


def _get_version() -> str:
    try:
        from crosspilot.version import __version__
        return __version__
    except Exception:
        return '0.0.0'


def _generate_report(output_path: str) -> None:
    """生成审核报告。"""
    try:
        from crosspilot.report import generate_report
        generate_report(output_path)
    except Exception as e:
        print(f'  [WARN] Report generation failed: {e}')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='crosspilot',
        description='CrossPilot - E-commerce Listing Auto-Cleaning Platform',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  crosspilot run "input.xlsx"              Full pipeline
  crosspilot run "input.xlsx" --text-only  Text cleaning only
  crosspilot run "input.xlsx" --image-only Image review+generation only
  crosspilot run "input.json" --max-rows 50  Test mode (first 50 rows)
  crosspilot run "input.xlsx" --report     Generate review report
  crosspilot audit "output.json"            Read-only image audit + review package
  crosspilot review "output.json" "review"  Build review package only
  crosspilot apply "output.json" "decisions.json" --dry-run
  crosspilot health                        API health check
  crosspilot config --init                 Create .env config file
  crosspilot web                           Start web dashboard
        ''',
    )
    sub = parser.add_subparsers(dest='command')

    # ── run ──
    p_run = sub.add_parser('run', help='Run pipeline on input file')
    p_run.add_argument('input', help='Input file (.xlsx or .json)')
    p_run.add_argument('--text-only', action='store_true', help='Skip image generation')
    p_run.add_argument('--image-only', action='store_true', help='Only review + generate images')
    p_run.add_argument('--max-rows', type=int, default=0, help='Process first N rows only')
    p_run.add_argument('--report', action='store_true', help='Generate review report after')
    p_run.add_argument('--no-report', action='store_true', help='Skip report generation')
    p_run.add_argument('--skip-health', action='store_true', help='Skip API health check')
    p_run.add_argument('--force', action='store_true', help='Force run even if health check fails')
    p_run.add_argument('--show-config', action='store_true', help='Show config and exit')
    p_run.set_defaults(func=cmd_run)

    # ── audit ──
    p_audit = sub.add_parser(
        'audit',
        help='Read-only Amazon image audit and review package',
    )
    p_audit.add_argument('input', help='Amazon JSON delivery')
    p_audit.add_argument('--output-root')
    p_audit.add_argument('--cache')
    p_audit.add_argument(
        '--ids',
        help='Comma-separated product IDs to audit',
    )
    p_audit.add_argument(
        '--no-package',
        action='store_true',
        help='Write audit report without the offline review package',
    )
    p_audit.set_defaults(func=cmd_audit)

    # ── review ──
    p_review = sub.add_parser(
        'review',
        help='Build Amazon Chinese-copy and all-image review package',
    )
    p_review.add_argument('input', help='Amazon JSON delivery')
    p_review.add_argument('output_dir', help='Review package directory')
    p_review.add_argument(
        '--translate-workers',
        type=int,
        default=30,
    )
    p_review.add_argument(
        '--download-workers',
        type=int,
        default=32,
    )
    p_review.add_argument('--audit-data')
    p_review.add_argument('--quarantine-manifest')
    p_review.add_argument('--shared-cache-dir')
    p_review.add_argument('--translation-cache')
    p_review.add_argument('--run-id')
    p_review.set_defaults(func=cmd_review)

    # ── apply ──
    p_apply = sub.add_parser(
        'apply',
        help='Validate and apply exported Amazon review decisions',
    )
    p_apply.add_argument('formal_json')
    p_apply.add_argument('decisions_json')
    p_apply.add_argument('--review-package')
    p_apply.add_argument('--dry-run', action='store_true')
    p_apply.set_defaults(func=cmd_apply)

    # ── health ──
    p_health = sub.add_parser('health', help='Check API availability')
    p_health.set_defaults(func=cmd_health)

    # ── config ──
    p_config = sub.add_parser('config', help='Manage configuration')
    p_config.add_argument('--init', action='store_true', help='Create .env config file')
    p_config.add_argument('--force', action='store_true', help='Overwrite existing .env')
    p_config.set_defaults(func=cmd_config)

    # ── watch ──
    p_watch = sub.add_parser('watch', help='Watch folder for new files (RPA mode)')
    p_watch.add_argument('--dir', default=None, help='Watch directory (default: ./watch)')
    p_watch.set_defaults(func=cmd_watch)

    # ── web ──
    p_web = sub.add_parser('web', help='Start web dashboard')
    p_web.add_argument('-p', '--port', type=int, default=8765)
    p_web.add_argument('--host', default='127.0.0.1')
    p_web.add_argument('--dev', action='store_true', help='Dev mode (auto-reload)')
    p_web.set_defaults(func=cmd_web)

    # ── legacy shortcuts ──
    p_ebay = sub.add_parser('ebay', help='[Legacy] eBay pipeline')
    p_ebay.add_argument('input')
    p_ebay.set_defaults(func=lambda a: cmd_run(argparse.Namespace(
        input=a.input, text_only=False, image_only=False, max_rows=0,
        report=False, no_report=True, skip_health=False, force=False,
        show_config=False,
    )))

    p_amazon = sub.add_parser('amazon', help='[Legacy] Amazon pipeline')
    p_amazon.add_argument('input')
    p_amazon.set_defaults(func=lambda a: cmd_run(argparse.Namespace(
        input=a.input, text_only=False, image_only=False, max_rows=0,
        report=False, no_report=True, skip_health=False, force=False,
        show_config=False,
    )))

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)

    # No subcommand = run mode for positional input
    if arguments and not arguments[0].startswith('-') and arguments[0] not in {
        'run', 'audit', 'review', 'apply', 'health', 'config', 'watch',
        'web', 'ebay', 'amazon',
    }:
        # Treat first arg as input file, insert 'run'
        arguments.insert(0, 'run')

    args = parser.parse_args(arguments)
    if not hasattr(args, 'func'):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())

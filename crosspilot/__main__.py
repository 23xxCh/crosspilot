#!/usr/bin/env python3
"""CrossPilot 统一 CLI — 所有功能一个入口。"""
from __future__ import annotations

import argparse, sys, os

# Ensure scripts/ is on path for imports
_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts')
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)


def cmd_ebay(args: argparse.Namespace) -> int:
    """eBay → TikTok Shop 表格清洗管道。"""
    from process_ebay_tk import _main
    print(f"CrossPilot eBay 管道: {args.input}", flush=True)
    output = _main(args.input)
    print(f"\n输出: {output}", flush=True)
    return 0


def cmd_amazon(args: argparse.Namespace) -> int:
    """Amazon 采集表 → 回填表管道。"""
    import process_amazon
    print(f"CrossPilot Amazon 管道: {args.input}", flush=True)
    output = process_amazon._main(args.input)
    print(f"\n输出: {output}", flush=True)
    return 0


def cmd_gen(args: argparse.Namespace) -> int:
    """纯图生图（Agnes，全局限速）。"""
    from gen_only import _main as gen_main
    gen_main(args.input, args.concurrency)
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """启动 Web 管理平台。"""
    import uvicorn
    port = args.port or 8765
    host = args.host or '127.0.0.1'
    print(f"CrossPilot Web: http://{host}:{port}", flush=True)
    uvicorn.run('web.app:app', host=host, port=port, reload=args.dev)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog='crosspilot',
        description='CrossPilot — 跨境电商 Listing 全自动清洗平台',
    )
    sub = parser.add_subparsers(dest='command', required=True)

    # ebay
    p_ebay = sub.add_parser('ebay', help='eBay → TikTok Shop 表格清洗')
    p_ebay.add_argument('input', help='输入 xlsx 文件路径')
    p_ebay.set_defaults(func=cmd_ebay)

    # amazon
    p_amazon = sub.add_parser('amazon', help='Amazon 采集表 → 回填表')
    p_amazon.add_argument('input', help='输入 xlsx 或 json 文件路径')
    p_amazon.set_defaults(func=cmd_amazon)

    # gen (standalone image gen)
    p_gen = sub.add_parser('gen', help='纯图生图（Agnes，去水印和人物）')
    p_gen.add_argument('input', help='输入 JSON 文件路径（含产品图片链接）')
    p_gen.add_argument('-c', '--concurrency', type=int, default=20,
                       help='并发数（默认 20，已验证最佳）')
    p_gen.set_defaults(func=cmd_gen)

    # web
    p_web = sub.add_parser('web', help='启动 Web 管理平台')
    p_web.add_argument('-p', '--port', type=int, default=8765, help='端口（默认 8765）')
    p_web.add_argument('--host', default='127.0.0.1', help='绑定地址（默认 127.0.0.1）')
    p_web.add_argument('--dev', action='store_true', help='开发模式（自动重载）')
    p_web.set_defaults(func=cmd_web)

    args = parser.parse_args()
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())

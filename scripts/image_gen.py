#!/usr/bin/env python3
"""
图生图去水印脚本 — 独立可运行 (使用 model_provider)

功能:
  读取本地图片文件 → base64 编码 → 调用配置的图生图 API
  → 下载生成的去水印图片到 output/ 目录
  → 支持并发处理多张图片

用法:
  python image_gen.py test.jpg                          # 单张
  python image_gen.py test.jpg -o clean.jpg             # 指定输出文件名
  python image_gen.py images/                           # 处理整个目录
  python image_gen.py images/ -c 5                      # 并发 5 张

依赖:
  pip install requests
  # 需要 keys.json 配置 image_gen_provider

API 说明:
  使用 model_provider 配置的 image_gen_provider（默认 Agnes）
"""

import os
import sys
import json
import time
import base64
import shutil
import traceback
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model_provider import get_provider

# ============================================================
# 1. 图片转 base64
# ============================================================

def image_to_base64(filepath):
    """
    读取本地图片 → base64 data URI
    返回: "data:image/jpeg;base64,/9j/4AAQ..."
    失败返回 None
    """
    try:
        ext = Path(filepath).suffix.lower().lstrip('.')
        # 映射 MIME 类型
        mime_map = {'jpg': 'jpeg', 'jpeg': 'jpeg', 'png': 'png', 'webp': 'webp', 'bmp': 'bmp'}
        mime = mime_map.get(ext, 'jpeg')

        with open(filepath, 'rb') as f:
            data = base64.b64encode(f.read()).decode('ascii')
        return f'data:image/{mime};base64,{data}'
    except Exception:
        traceback.print_exc()
        return None


# ============================================================
# 2. 图生图函数 (通过 model_provider)
# ============================================================

def generate_single(filepath):
    """
    使用 model_provider 进行图生图
    filepath: 本地图片路径
    返回: 生成图片的 URL, 失败返回 None
    """
    print(f"  [编码] {os.path.basename(filepath)}")
    b64 = image_to_base64(filepath)
    if not b64:
        print(f"  [失败] 无法编码: {filepath}")
        return None

    print(f"  [生图] 调用 model_provider...")
    try:
        provider = get_provider()
        # 使用 data URI 直接传
        url = provider.call_image_gen(b64, size='1600x1600')
        if url:
            print(f"  [成功] {url[:60]}...")
            return url
    except Exception as e:
        print(f"  [失败] {e}")

    print(f"  [失败] 生图失败: {filepath}")
    return None


# ============================================================
# 3. 下载图片
# ============================================================

def download_image(url, output_path):
    """
    从 URL 下载图片到本地
    """
    import requests
    try:
        r = requests.get(url, timeout=60, stream=True)
        if r.ok:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, 'wb') as f:
                shutil.copyfileobj(r.raw, f)
            return output_path
    except Exception:
        traceback.print_exc()
    return None


# ============================================================
# 4. 并发处理 + 结果下载
# ============================================================

def process_batch(file_list, concurrency=5, output_dir='output'):
    """
    并发处理一批图片
    """
    results = {}

    print(f"\n{'='*60}")
    print(f"图生图批量处理 (model_provider)")
    print(f"图片数: {len(file_list)} | 并发: {concurrency}")
    print(f"输出目录: {os.path.abspath(output_dir)}")
    print(f"{'='*60}\n")

    t0 = time.time()

    # 第一阶段: 并发调 API 生成
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(generate_single, fp): fp for fp in file_list}
        for i, future in enumerate(as_completed(futures), 1):
            fp = futures[future]
            try:
                url = future.result()
                if url:
                    results[fp] = url
            except Exception:
                traceback.print_exc()
            print(f"  进度: {i}/{len(file_list)}")

    # 第二阶段: 下载到本地
    print(f"\n  生成完成: {len(results)}/{len(file_list)}, 下载中...")
    ok = fail = 0
    for fp, url in results.items():
        out_name = f"clean_{Path(fp).stem}.png"
        out_path = os.path.join(output_dir, out_name)
        saved = download_image(url, out_path)
        if saved:
            print(f"  [OK] {out_name}")
            ok += 1
        else:
            print(f"  [FAIL] {out_name} (下载失败)")
            fail += 1

    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"完成: {ok} 成功, {fail} 失败 | 耗时 {elapsed:.1f}s")
    print(f"{'='*60}\n")
    return ok, fail


# ============================================================
# 5. 命令行入口
# ============================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description='图生图去水印 — 使用 model_provider')
    parser.add_argument('input', help='图片文件或目录')
    parser.add_argument('-o', '--output', default='output', help='输出目录 (默认 output/)')
    parser.add_argument('-c', '--concurrency', type=int, default=4,
                        help='并发数 (默认 4)')
    args = parser.parse_args()

    # 检查配置
    try:
        provider = get_provider()
        print(f"配置检查通过")
    except ValueError as e:
        print(f"错误: {e}")
        sys.exit(1)

    # 收集图片文件
    input_path = Path(args.input)
    if input_path.is_dir():
        IMG_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif'}
        files = sorted([str(p) for p in input_path.iterdir()
                        if p.suffix.lower() in IMG_EXTS and not p.name.startswith('.')])
        if not files:
            print(f"目录 {args.input} 中没有图片文件")
            sys.exit(1)
    elif input_path.is_file():
        files = [str(input_path)]
    else:
        print(f"文件或目录不存在: {args.input}")
        sys.exit(1)

    process_batch(files, concurrency=args.concurrency, output_dir=args.output)


if __name__ == '__main__':
    main()

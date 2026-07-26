#!/usr/bin/env python3
"""只生图——使用 model_provider。用法: uv run python scripts/gen_only.py "亚马逊表/xxx.json" """
import sys
import os
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model_provider import get_provider, reload_provider

RATE_LIMIT = 100  # RPM


def _main(tp: str, concurrency: int = 20) -> str:
    """纯图生图入口：接收 JSON 路径，返回 None。CLI 和 Web 均可调用。"""
    reload_provider()
    provider = get_provider()

    with open(tp, encoding='utf-8') as f:
        raw = json.load(f)

    img_urls_list = raw.get('产品图片链接', [])
    var_urls_list = raw.get('变种图片链接', [])

    all_urls = set()
    for i in range(len(raw.get('产品标题', []))):
        imgs = img_urls_list[i] if i < len(img_urls_list) else []
        vars_ = var_urls_list[i] if i < len(var_urls_list) else []
        if isinstance(imgs, list) and imgs:
            all_urls.add(imgs[0])
        if isinstance(vars_, list) and vars_:
            all_urls.add(vars_[0])
        elif isinstance(imgs, list) and len(imgs) > 1:
            all_urls.add(imgs[1])
    all_urls = list(all_urls)

    print(f'主图+变种: {len(all_urls)} 张 | {RATE_LIMIT} RPM 限速 | {concurrency}并发', flush=True)

    results = {}
    done = [0]
    plock = threading.Lock()

    def _gen(u):
        """使用 model_provider 进行图生图。"""
        try:
            new_url = provider.call_image_gen(u)
        except Exception as e:
            print(f'生图失败: {u} - {e}', flush=True)
            new_url = None
        with plock:
            done[0] += 1
            if done[0] % 50 == 0:
                print(f'  进度: {done[0]}/{len(all_urls)} (成功{len(results)})', flush=True)
        return u, new_url

    print(f'生图 {len(all_urls)} 张...', flush=True)
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_gen, u) for u in all_urls]
        for future in as_completed(futures):
            u, new_url = future.result()
            if new_url:
                results[u] = new_url

    total_ok = len(results)
    print(f'\n总计: {total_ok}/{len(all_urls)} ({total_ok/len(all_urls)*100:.0f}%)', flush=True)
    return None


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法: uv run python scripts/gen_only.py <输入.json> [并发数]")
        sys.exit(1)
    tp = sys.argv[1]
    concurrency = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    _main(tp, concurrency)

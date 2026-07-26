#!/usr/bin/env python3
"""
图生图去水印脚本 — 独立可运行

功能:
  读取本地图片文件 → base64 编码 → 调用 DMXAPI 图生图 API
  (三级 fallback: 万相 → 豆包 → gpt-image-2)
  → 下载生成的去水印图片到 output/ 目录
  → 支持并发处理多张图片

用法:
  python image_gen.py test.jpg                          # 单张
  python image_gen.py test.jpg -o clean.jpg             # 指定输出文件名
  python image_gen.py images/                           # 处理整个目录
  python image_gen.py images/ -c 5                      # 并发 5 张

依赖:
  pip install requests
  # 需要 keys.json 在同目录或上级目录，格式: {"dmx_key": "sk-..."}

API 说明 (三级 fallback):
  万相   wan2.7-image          ~8s/张  主力, 并发~10 超了会 429
  豆包   doubao-seedream-5.0-lite  ~22s/张  备1
  gpt-image-2  gpt-image-2      ~15s/张  备2

DMXAPI 中转站: https://www.dmxapi.cn (不限 RPM/TPM, 按用量计费)
"""

import os, sys, json, time, base64, threading, shutil, traceback
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dmx_client import (_post_json as _dmx_post, _extract_image_url,
                        gen_wan, gen_doubao, WAN_MODEL, DOUBAO_MODEL, DMX_BASE)

# ============================================================
# 0. 加载 API 密钥
# ============================================================

def load_keys():
    """从 keys.json 读取 dmx_key。查找顺序: 脚本同目录 → 上级目录"""
    for d in [Path(__file__).parent, Path(__file__).parent.parent]:
        kf = d / 'keys.json'
        if kf.exists():
            try:
                keys = json.loads(kf.read_text(encoding='utf-8'))
                if keys.get('dmx_key'):
                    return keys['dmx_key']
            except Exception:  # keys.json 解析失败
                pass
    # 环境变量兜底
    return os.environ.get('DMX_KEY', '')

DMX_KEY = load_keys()
DMX_BASE = "https://www.dmxapi.cn"
TEMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.gen_temp')

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
    except Exception:  # 图片编码失败
        traceback.print_exc()
        return None


# 创建共享 Session（复用 dmx_client 的工具函数）
import requests as _requests
_session = _requests.Session()
_session.headers.update({'Authorization': f'Bearer {DMX_KEY}', 'Content-Type': 'application/json'})

def _post_json(endpoint, payload, timeout=180):
    """POST JSON 到 DMXAPI（委托 dmx_client）。"""
    return _dmx_post(_session, endpoint, payload, timeout)


def download_image(url, output_path):
    """
    从 URL 下载图片到本地
    output_path: 保存路径 (如 output/clean_001.jpg)
    返回: 成功返回 output_path, 失败返回 None
    """
    import requests
    try:
        r = requests.get(url, timeout=60, stream=True)
        if r.ok:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, 'wb') as f:
                shutil.copyfileobj(r.raw, f)
            return output_path
    except Exception:  # 图片下载失败
        traceback.print_exc()
    return None


# ============================================================
# 3. 三级图生图函数
# ============================================================

GEN_PROMPT = (
    "This is a product photo for e-commerce. Remove ALL watermarks, store IDs, "
    "brand names (Toyota, Honda, BMW, Mercedes, etc.), logos, and overlaid text "
    "from the image. These are NOT part of the product. Keep the product itself "
    "(car parts, accessories, etc.) completely unchanged - color, shape, texture, "
    "background, lighting all stay exactly the same. Only erase the watermark text "
    "and brand logos that are superimposed on top of the photo. "
    "Do NOT modify the product. Do NOT add anything. Do NOT change the composition."
)


def _gen_wan(b64_uri, retries=3):
    """
    万相图生图 (主力, ~8s/张)
    端点: /v1/responses, 模型: wan2.7-image
    b64_uri: data:image/jpeg;base64,xxx 格式的图片数据
    返回: 图片 URL，失败返回空字符串
    注意: 万相有并发限制, 超过 ~10 并发出 429, 这里用递增退避重试
    """
    payload = {
        "model": "wan2.7-image",
        "input": {"messages": [{"role": "user", "content": [
            {"image": b64_uri},    # base64 data URI 直接传
            {"text": GEN_PROMPT}
        ]}]},
        "parameters": {"size": "1024*1024", "watermark": False}
    }

    for attempt in range(retries):
        resp = _post_json("/v1/responses", payload, timeout=180)
        if resp is None:
            time.sleep(3 * (attempt + 1))
            continue
        url = _extract_image_url(resp)
        if url:
            return url
        if attempt < retries - 1:
            time.sleep(3 * (attempt + 1))
    return ''


def _gen_doubao(b64_uri, retries=2):
    """
    豆包图生图 (备 1, ~22s/张)
    端点: /v1/responses, 模型: doubao-seedream-5.0-lite
    b64_uri: base64 data URI
    """
    payload = {
        "model": "doubao-seedream-5.0-lite",
        "input": GEN_PROMPT,      # 顶层传 prompt 文本
        "image": b64_uri,          # 顶层传图片 data URI
        "size": "2K",
        "response_format": "url",
        "watermark": False
    }

    for attempt in range(retries):
        resp = _post_json("/v1/responses", payload, timeout=180)
        if resp is None:
            time.sleep(3)
            continue
        url = _extract_image_url(resp)
        if url:
            return url
    return ''


def _gen_gptimage2(b64_uri, retries=2):
    """
    gpt-image-2 图生图 (备 2, ~15s/张)
    端点: /v1/images/generations, 模型: gpt-image-2
    image 参数通过 extra_body 传入 base64 data URI
    """
    payload = {
        "model": "gpt-image-2",
        "prompt": GEN_PROMPT,
        "size": "1024x1024",
        "n": 1,
        "extra_body": {
            "image": [b64_uri],
            "response_format": "url"
        }
    }

    for attempt in range(retries):
        resp = _post_json("/v1/images/generations", payload, timeout=180)
        if resp is None:
            time.sleep(3)
            continue

        url = _extract_image_url(resp)
        if url:
            return url

    return ''


def generate_single(filepath):
    """
    三级 fallback 图生图: 万相 → 豆包 → gpt-image-2
    filepath: 本地图片路径
    返回: 生成图片的 URL, 全失败返回 None
    """
    print(f"  [编码] {os.path.basename(filepath)}")
    b64 = image_to_base64(filepath)
    if not b64:
        print(f"  [失败] 无法编码: {filepath}")
        return None

    # 1. 万相 (主力)
    print(f"  [万相] 生成中...")
    url = _gen_wan(b64)
    if url:
        print(f"  [万相] 成功 {url[:60]}...")
        return url

    # 2. 豆包 (备 1)
    print(f"  [豆包] 万相失败, 尝试豆包...")
    url = _gen_doubao(b64)
    if url:
        print(f"  [豆包] 成功 {url[:60]}...")
        return url

    # 3. gpt-image-2 (备 2)
    print(f"  [gpt-image-2] 豆包失败, 尝试 gpt-image-2...")
    url = _gen_gptimage2(b64)
    if url:
        print(f"  [gpt-image-2] 成功 {url[:60]}...")
        return url

    print(f"  [失败] 三级模型全部失败: {filepath}")
    return None


# ============================================================
# 4. 并发处理 + 结果下载
# ============================================================

def process_batch(file_list, concurrency=5, output_dir='output'):
    """
    并发处理一批图片
    file_list: 图片文件路径列表
    concurrency: 并发数 (建议 3-5, 万相上限 ~10)
    output_dir: 输出目录
    返回: (成功数, 失败数)
    """
    results = {}  # filepath -> generated_url

    print(f"\n{'='*60}")
    print(f"图生图批量处理")
    print(f"图片数: {len(file_list)} | 并发: {concurrency} | 模型: 万相→豆包→gpt-image-2")
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
            except Exception:  # 生成任务异常
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
        description='图生图去水印 — DMXAPI 三级 fallback (万相→豆包→gpt-image-2)')
    parser.add_argument('input', help='图片文件或目录')
    parser.add_argument('-o', '--output', default='output', help='输出目录 (默认 output/)')
    parser.add_argument('-c', '--concurrency', type=int, default=4,
                        help='并发数 (默认 4, 建议 3-5, 万相超 10 会限流)')
    args = parser.parse_args()

    # 检查密钥
    if not DMX_KEY:
        print("错误: 未找到 DMXAPI 密钥。请在脚本同目录创建 keys.json:")
        print('  {"dmx_key": "sk-..."}')
        print("或设置环境变量 DMX_KEY")
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

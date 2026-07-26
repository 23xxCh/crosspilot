#!/usr/bin/env python3
"""二次图审：对已生成的 Agnes 图再过一遍审图，检出问题重新生成。"""
import json, sys, time, glob, os
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model_provider import get_provider

provider = get_provider()

# 找最新输出文件
files = glob.glob(r'亚马逊表\跨境电商自动化回填表2_*.json')
files = [f for f in files if 'metrics' not in f and '模板' not in f]
if not files:
    print("No output files found")
    sys.exit(1)
output_path = max(files, key=os.path.getmtime)
print(f"Processing: {output_path}")

with open(output_path, encoding='utf-8') as f:
    out = json.load(f)

# 收集所有 Agnes 生成的主图 URL（去重）
fields = list(out.keys())
img_f = fields[3]  # 产品图片链接
all_agnes_urls = set()
for i in range(len(out[fields[1]])):
    imgs = out[img_f][i]
    if imgs and 'agnes-ai.space' in imgs[0]:
        all_agnes_urls.add(imgs[0])

print(f"Unique Agnes main images to review: {len(all_agnes_urls)}")

# 逐张审图
failed = []
ok = 0
done = 0
lock = __import__('threading').Lock()

def review_one(url):
    global ok, done, failed
    result = provider.call_vision(url)
    with lock:
        done += 1
        if result is True:
            failed.append(url)
            print(f"[FAIL] {done}/{len(all_agnes_urls)}: {url[:80]}")
        elif result is False:
            ok += 1
            if done % 50 == 0:
                print(f"[OK] {done}/{len(all_agnes_urls)} (ok={ok}, fail={len(failed)})")
        else:
            print(f"[ERR] {done}/{len(all_agnes_urls)}: review returned None - {url[:60]}")
    return url, result

print(f"Reviewing {len(all_agnes_urls)} images (20 concurrent)...")
t0 = time.time()
with ThreadPoolExecutor(max_workers=20) as pool:
    futures = [pool.submit(review_one, u) for u in all_agnes_urls]
    for _ in as_completed(futures):
        pass

elapsed = time.time() - t0
print(f"\nReview done: {elapsed:.0f}s | OK={ok} | FAIL={len(failed)}")

if not failed:
    print("🎉 All images clean! No re-generation needed.")
else:
    print(f"\n{len(failed)} images need re-generation. Starting...")
    # 重新生成
    url_map = {}
    regen_ok = 0
    regen_done = 0
    lock2 = __import__('threading').Lock()

    def regen_one(old_url):
        global regen_ok, regen_done
        new_url = provider.call_image_gen(old_url, retries=10, is_variant=False)
        with lock2:
            regen_done += 1
            if new_url:
                url_map[old_url] = new_url
                regen_ok += 1
                print(f"[REGEN OK] {regen_done}/{len(failed)}: {old_url[:60]} -> {new_url[:60]}")
            else:
                print(f"[REGEN FAIL] {regen_done}/{len(failed)}: {old_url[:60]}")
        return old_url, new_url

    t1 = time.time()
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(regen_one, u) for u in failed]
        for _ in as_completed(futures):
            pass

    print(f"\nRegen done: {time.time()-t1:.0f}s | OK={regen_ok}/{len(failed)}")

    # 替换 URL
    replaced = 0
    for i in range(len(out[fields[1]])):
        imgs = out[img_f][i]
        if imgs and imgs[0] in url_map:
            out[img_f][i][0] = url_map[imgs[0]]
            replaced += 1

    print(f"Replaced {replaced} URLs in output")

    # 保存新文件
    new_path = output_path.replace('.json', '_doublecheck.json')
    with open(new_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"Saved: {new_path}")

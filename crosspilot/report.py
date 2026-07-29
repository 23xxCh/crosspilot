#!/usr/bin/env python3
"""CrossPilot 审核报告 — 自动生成中英对照审核报告和质量摘要。"""
from __future__ import annotations

import os
import re
import json
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

_SCRIPTS = str(Path(__file__).resolve().parent.parent / 'scripts')
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import requests
from crosspilot.config import get, get_int


def generate_report(output_path: str) -> str:
    """从回填 JSON 生成审核报告到同目录 output/ 文件夹。"""
    p = Path(output_path)
    if not p.exists():
        print(f'  [WARN] Output file not found: {output_path}')
        return ''

    # Create output directory
    out_dir = p.parent / f'{p.stem}_review'
    out_dir.mkdir(parents=True, exist_ok=True)
    img_dir = out_dir / 'images'
    img_dir.mkdir(parents=True, exist_ok=True)

    # Load data
    if p.suffix.lower() == '.json':
        with open(p, encoding='utf-8-sig') as f:
            data = json.load(f)
    else:
        print(f'  [WARN] Only JSON reports supported currently')
        return ''

    n = len(data.get('产品标题', []))
    if n == 0:
        print('  [WARN] Empty output, skipping report')
        return ''

    print(f'\n  Generating review report for {n} rows...')

    # Collect image URLs
    image_urls = set()
    for i in range(n):
        for url in data.get('产品图片链接', [])[i] or []:
            if url and url.startswith('http'):
                image_urls.add(url)
        for url in data.get('变种图片链接', [])[i] or []:
            if url and url.startswith('http'):
                image_urls.add(url)

    # Download images (up to 3 per unique URL, 20 concurrent)
    if image_urls:
        print(f'  Downloading {len(image_urls)} images...')
        _download_images(image_urls, img_dir)

    # Build report
    lines = []
    lines.append('=' * 80)
    lines.append('CrossPilot Review Report / 审核报告')
    lines.append(f'Total rows: {n}')
    lines.append('')
    lines.append('*** 生图质量提醒 ***')
    lines.append('质量门禁已关闭（人工检查替代自动审核）。')
    lines.append('请抽检 images/ 目录下的生图，确认：')
    lines.append('  1. 产品主体未变形')
    lines.append('  2. 无水印/品牌 Logo 残留')
    lines.append('  3. 白底干净、无多余文字')
    lines.append('如有问题，手动替换该行主图后即可使用。')
    lines.append('')
    lines.append(f'Generated: {_now()}')
    lines.append('=' * 80)

    quality_issues = []
    for i in range(n):
        lines.append('')
        lines.append(f'--- Row {i + 1} / 第 {i + 1} 行 ---')
        row_issues = _write_row(lines, data, i, img_dir)

        # Quality checks
        title = str(data['产品标题'][i] or '')
        desc = str(data['产品描述'][i] or '')
        bullets = [str(data.get(f'Bullet Point{j}', []) and (data[f'Bullet Point{j}'][i] or '')) for j in range(1, 6)]
        kw = str(data.get('关键词信息', [''])[i] or '')

        if len(title) > 75:
            quality_issues.append(f'Row {i+1}: Title exceeds 75 chars ({len(title)} chars)')
            row_issues.append('  [!] Title too long')
        if any(w in desc for w in ['Welcome to', 'Payment', 'Shipping', 'Copyright', 'Negative feedback']):
            quality_issues.append(f'Row {i+1}: Description contains policy/store text')
            row_issues.append('  [!] Description has non-product content')

    # Quality summary
    lines.insert(4, f'Quality issues: {len(quality_issues)}')
    if quality_issues:
        lines.insert(5, '-' * 40)
        idx = 5
        for issue in quality_issues[:20]:
            idx += 1
            lines.insert(idx, f'  {issue}')
        if len(quality_issues) > 20:
            lines.insert(idx + 1, f'  ... and {len(quality_issues) - 20} more')

    # Write report
    report_path = out_dir / 'review_report.txt'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    # Write quality summary
    qs_path = out_dir / 'quality_summary.txt'
    with open(qs_path, 'w', encoding='utf-8') as f:
        f.write(f'CrossPilot Quality Summary\n')
        f.write(f'========================\n')
        f.write(f'Total rows: {n}\n')
        f.write(f'Rows with issues: {len(quality_issues)}\n')
        f.write(f'Rows clean: {n - len(quality_issues)}\n')
        f.write(f'\nIssues:\n')
        for issue in quality_issues:
            f.write(f'  {issue}\n')

    print(f'  Report: {report_path}')
    print(f'  Summary: {qs_path}')
    print(f'  Images: {img_dir} ({len(list(img_dir.iterdir()))} files)')
    return str(report_path)


def _write_row(lines: list[str], data: dict, i: int, img_dir: Path) -> list[str]:
    """写入一行审核数据。"""
    issues = []
    title = str(data['产品标题'][i] or '')
    desc = str(data['产品描述'][i] or '')[:300]
    lines.append(f'Title: {title}')

    lines.append(f'Description: {desc}')

    lines.append('Bullet Points:')
    for j in range(1, 6):
        key = f'Bullet Point{j}'
        if key in data:
            b = str(data[key][i] or '').strip()
            if b:
                lines.append(f'  [{j}] {b}')
            else:
                issues.append(f'  [!] Bullet {j} is empty')

    kw_key = '关键词信息' if '关键词信息' in data else 'keywords'
    if kw_key in data:
        kw = str(data[kw_key][i] or '')
        lines.append(f'Keywords: {kw}')
        terms = [t.strip() for t in kw.split(',') if t.strip()]
        if len(terms) < 10:
            issues.append(f'  [!] Only {len(terms)}/10 keywords')

    # Images
    main_imgs = data.get('产品图片链接', [])
    var_imgs = data.get('变种图片链接', [])
    if i < len(main_imgs):
        lines.append('Main images:')
        for idx, url in enumerate((main_imgs[i] or [])[:3]):
            local = _img_local_path(url, img_dir)
            lines.append(f'  img{idx+1}: {local or url}')
    if i < len(var_imgs):
        for idx, url in enumerate((var_imgs[i] or [])[:2]):
            local = _img_local_path(url, img_dir)
            lines.append(f'  var{idx+1}: {local or url}')

    for issue in issues:
        lines.append(issue)
    return issues


# ── helpers ────────────────────────────────────────────────────

def _img_local_path(url: str, img_dir: Path) -> str:
    """返回图片本地路径或空字符串。"""
    if 'task_' in url:
        m = re.search(r'task_([a-zA-Z0-9]+)', url)
        if m:
            name = f'task_{m.group(1)}.png'
            if (img_dir / name).exists():
                return str(img_dir / name)
    # Fallback
    fname = re.sub(r'[^a-zA-Z0-9_\-.]', '_', url.split('/')[-1] or 'img')[:60]
    path = img_dir / fname
    return str(path) if path.exists() else ''


def _download_images(urls: set, img_dir: Path) -> None:
    """并发下载图片。"""
    session = requests.Session()
    session.headers.update({'User-Agent': 'CrossPilot/1.0'})

    def _dl(url):
        try:
            fname = re.sub(r'[^a-zA-Z0-9_\-.]', '_', url.split('/')[-1] or 'img')[:80]
            if 'task_' in url:
                m = re.search(r'task_([a-zA-Z0-9]+)', url)
                if m:
                    fname = f'task_{m.group(1)}.png'
            path = img_dir / fname
            if path.exists() and path.stat().st_size > 100:
                return url, str(path)
            r = session.get(url, timeout=20, stream=True)
            if r.ok and 'image' in r.headers.get('content-type', ''):
                with open(path, 'wb') as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)
                return url, str(path)
            return url, None
        except Exception:
            return url, None

    done = 0
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(_dl, u): u for u in urls}
        for _ in as_completed(futures):
            done += 1
    # print is done in caller


def _now() -> str:
    import datetime
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

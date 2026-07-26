#!/usr/bin/env python3
"""批量队列处理：扫描文件夹里的 xlsx，顺序处理，结果分文件夹归档。

用法：
  uv run python -u scripts/batch_process.py "<文件夹路径>"

文件夹结构（自动创建）：
  待处理/   放要处理的 xlsx（也可直接把装 xlsx 的文件夹拖进来）
  处理中/   正在处理的文件 + status.json 进度
  已完成/   清洗好的 _cleaned.xlsx；原文件/ 放原始文件
  失败/     失败的文件 + .error.txt 错误日志
  queue_status.json  队列状态（中断重启自动跳过已完成）
"""
import os, sys, json, shutil, time, traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import process_ebay_tk


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def write_dashboard(root, files, qstatus, current=None):
    """生成自刷新 dashboard.html，非技术用户双击就能看实时进度。"""
    # 读当前文件的 status.json
    cur_stage, cur_pct, cur_eta, cur_detail = '', 0, 0, ''
    if current:
        sp = os.path.join(root, '处理中', os.path.splitext(current)[0] + '_status.json')
        try:
            st = json.load(open(sp, encoding='utf-8'))
            cur_stage = st.get('stage', '')
            cur_pct = st.get('percent', 0)
            cur_eta = st.get('eta_s', 0)
            cur_detail = f"阶段 {st.get('stage_index','?')}/{st.get('stage_total','?')} · {st.get('current','?')}/{st.get('total','?')}"
        except Exception:  # dashboard 写入失败不影响
            pass

    def row(f):
        if f in qstatus['completed']:
            return f'<tr><td>✅</td><td>{_esc(f)}</td><td>完成</td></tr>'
        if f in qstatus['failed']:
            err = os.path.join('失败', os.path.splitext(f)[0] + '.error.txt')
            return f'<tr class="fail"><td>❌</td><td>{_esc(f)}</td><td><a href="{_esc(err)}">失败原因</a></td></tr>'
        if f == current:
            return f'<tr class="cur"><td>⏳</td><td>{_esc(f)}</td><td>处理中 {cur_pct}%</td></tr>'
        return f'<tr><td>⏸</td><td>{_esc(f)}</td><td>待处理</td></tr>'

    rows = ''.join(row(f) for f in files)
    done_n, fail_n = len(qstatus['completed']), len(qstatus['failed'])
    total = len(files)
    overall = int((done_n + fail_n) / total * 100) if total else 0

    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="5"><title>批量清洗进度</title>
<style>
body{{font-family:system-ui,'Microsoft YaHei',sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#222}}
h1{{font-size:20px}} .bar{{background:#eee;border-radius:8px;overflow:hidden;height:22px;margin:6px 0}}
.bar>i{{display:block;background:#4caf50;height:100%;color:#fff;font-size:12px;line-height:22px;text-align:right;padding-right:6px;box-sizing:border-box;transition:width .3s}}
table{{width:100%;border-collapse:collapse;margin-top:14px;font-size:14px}}
td,th{{border:1px solid #ddd;padding:6px 10px;text-align:left}}
tr.cur{{background:#fff8e1;font-weight:bold}} tr.fail{{background:#ffebee}}
.stat{{display:flex;gap:18px;margin:12px 0;font-size:14px}}
.stat b{{font-size:20px}}
a{{color:#c62828}}
</style></head><body>
<h1>🛒 eBay→TikTok 批量清洗进度</h1>
<div class="stat"><span>总进度 <b>{done_n+fail_n}/{total}</b></span><span>✅ 成功 <b>{done_n}</b></span><span>❌ 失败 <b>{fail_n}</b></span><span>⏳ 当前 <b>{_esc(current or '—')}</b></span></div>
<div class="bar"><i style="width:{overall}%">{overall}%</i></div>
{f'<p>当前阶段：<b>{_esc(cur_stage)}</b>（{_esc(cur_detail)}），预计剩余 {cur_eta}s</p>' if current else '<p>队列空闲</p>'}
<table><tr><th>状态</th><th>文件</th><th>结果</th></tr>{rows}</table>
<p style="color:#999;font-size:12px">每 5 秒自动刷新 · 更新于 {time.strftime('%H:%M:%S')}</p>
</body></html>"""
    try:
        with open(os.path.join(root, 'dashboard.html'), 'w', encoding='utf-8') as f:
            f.write(html)
    except Exception:  # 队列状态 JSON 解析失败
        pass


def main():
    root = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else None
    if not root or not os.path.isdir(root):
        print("用法: uv run python -u scripts/batch_process.py \"<文件夹路径>\"")
        sys.exit(1)

    pending = os.path.join(root, '待处理')
    processing = os.path.join(root, '处理中')
    done = os.path.join(root, '已完成')
    failed = os.path.join(root, '失败')
    done_orig = os.path.join(done, '原文件')
    for d in [pending, processing, done, failed, done_orig]:
        os.makedirs(d, exist_ok=True)

    # 队列状态：记录已完成的文件名，中断重启跳过
    qstatus_path = os.path.join(root, 'queue_status.json')
    try:
        qstatus = json.load(open(qstatus_path, encoding='utf-8'))
    except Exception:  # 同上
        qstatus = {'completed': [], 'failed': []}

    # 收集待处理文件：优先 待处理/，否则 root 下的散 xlsx（排除子文件夹）
    def list_xlsx(d):
        return [f for f in os.listdir(d)
                if f.lower().endswith('.xlsx') and not f.startswith('~$')]
    files = list_xlsx(pending)
    crashed = list_xlsx(processing)  # 上次崩溃卡在处理中的文件（cache 就在旁边可续跑）
    if not files and not crashed:
        files = [f for f in list_xlsx(root)]
    # 处理中的崩溃文件优先重跑（cache 续跑），再去待处理
    files = crashed + [f for f in files if f not in crashed]
    files = [f for f in files if f not in qstatus['completed'] and f not in qstatus['failed']]

    if not files:
        log("没有待处理的 xlsx（或全部已完成）")
        return
    log(f"批量处理: {len(files)} 个文件 | 根目录 {root}" +
        (f"（含 {len(crashed)} 个断点续跑）" if crashed else ""))

    def src_of(fname):
        # 崩溃文件在 处理中/，其余在 待处理/ 或根目录
        if fname in crashed:
            return os.path.join(processing, fname)
        return os.path.join(pending if list_xlsx(pending) else root, fname)

    ok_cnt, fail_cnt = 0, 0
    write_dashboard(root, files, qstatus)  # 初始看板
    for i, fname in enumerate(files, 1):
        log(f"===== [{i}/{len(files)}] {fname} =====")
        src = src_of(fname)
        work = os.path.join(processing, fname)
        write_dashboard(root, files, qstatus, current=fname)  # 标记当前处理
        try:
            if os.path.abspath(src) != os.path.abspath(work):
                shutil.move(src, work)
                # cache 随文件一起移动（保留断点）
                src_cache = os.path.splitext(src)[0] + '_cache.json'
                if os.path.exists(src_cache):
                    shutil.move(src_cache, os.path.splitext(work)[0] + '_cache.json')
            # 注入看板 hook：处理中按节流实时刷新 dashboard
            process_ebay_tk._DASHBOARD_HOOK = lambda: write_dashboard(root, files, qstatus, current=fname)
            out = process_ebay_tk._main(work)  # 处理，返回 _cleaned 路径
            process_ebay_tk._DASHBOARD_HOOK = None
            # 移动结果和原文件
            if out and os.path.exists(out):
                shutil.move(out, os.path.join(done, os.path.basename(out)))
            shutil.move(work, os.path.join(done_orig, fname))
            # 清理处理中产生的 status/cache
            for ext in ['_status.json', '_cache.json']:
                p = os.path.splitext(work)[0] + ext
                if os.path.exists(p):
                    shutil.move(p, os.path.join(done, os.path.basename(p)))
            qstatus['completed'].append(fname)
            ok_cnt += 1
            log(f"✅ 完成: {fname}")
        except Exception as e:
            # 失败：移回并写错误日志，队列继续
            err_txt = os.path.join(failed, os.path.splitext(fname)[0] + '.error.txt')
            try:
                if os.path.exists(work):
                    shutil.move(work, os.path.join(failed, fname))
                with open(err_txt, 'w', encoding='utf-8') as f:
                    f.write(f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}")
            except Exception:  # 文件移动失败不影响
                pass
            qstatus['failed'].append(fname)
            fail_cnt += 1
            log(f"❌ 失败: {fname} — {type(e).__name__}: {e}")
        # 保存队列状态 + 刷新看板
        try:
            with open(qstatus_path, 'w', encoding='utf-8') as f:
                json.dump(qstatus, f, ensure_ascii=False, indent=2)
        except Exception:  # 队列状态写入失败不影响
            pass
        write_dashboard(root, files, qstatus)

    log(f"===== 批量结束: 成功 {ok_cnt}, 失败 {fail_cnt} =====")
    write_dashboard(root, files, qstatus)  # 最终看板
    if fail_cnt:
        log(f"失败清单见 {failed}")


if __name__ == '__main__':
    main()

"""子进程 runner：被 jobs.enqueue 用 subprocess.Popen 调起，跑单个管道任务。
独立脚本 = 独立 python 进程，全局各管各的，多任务真并发不串台。"""
import os, sys, traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

import process_ebay_tk  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print("用法: python runner.py <input.xlsx>", file=sys.stderr)
        sys.exit(2)
    input_path = sys.argv[1]
    try:
        process_ebay_tk._main(input_path)
    except Exception:  # 管道崩溃，已写 _child_error.txt
        # 崩溃写错误文件，主进程 monitor 探活会发现 exit 非0
        err = os.path.splitext(input_path)[0] + '_child_error.txt'
        with open(err, 'w', encoding='utf-8') as f:
            f.write(traceback.format_exc())
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()

"""子进程 runner：被 jobs.enqueue 用 subprocess.Popen 调起，跑单个管道任务。
独立脚本 = 独立 python 进程，全局各管各的，多任务真并发不串台。"""
import os, sys, traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_pipeline(pipeline, input_path):
    """运行一个管道并返回进程退出码。"""
    try:
        if pipeline == 'amazon':
            from scripts import process_amazon as p
            p.run_amazon_pipeline(input_path)
        elif pipeline == 'ebay':
            from scripts import process_ebay_tk as p
            p._main(input_path)
        else:
            raise ValueError(f"未知管道: {pipeline}")
        return 0
    except Exception:
        err = os.path.splitext(input_path)[0] + '_child_error.txt'
        with open(err, 'w', encoding='utf-8') as f:
            f.write(traceback.format_exc())
        traceback.print_exc()
        return 1


def main():
    if len(sys.argv) < 3:
        print(
            "用法: python -m web.runner <pipeline> <input_path>",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.exit(run_pipeline(sys.argv[1], sys.argv[2]))


if __name__ == '__main__':
    main()

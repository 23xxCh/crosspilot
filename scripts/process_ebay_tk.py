#!/usr/bin/env python3
"""eBay→TikTok Shop 表格清洗 (ebay-tk 定制版) — 统一入口。

流程:
  Agnes图审 → Agnes图生图 → 附图清空 → 标题翻译 → 描述AI清洗 →
  描述翻译 → 嵌入+注入图片 → 视频清空 → 模板清除 → 保存

管道实现已拆分至:
  scripts/pipelines/ebay_shared.py  — 共享模块（sessions, helpers, StatusReporter）
  scripts/pipelines/ebay_stages.py  — 阶段函数 + 编排（_main, _stage_*）
"""
import sys, os, json, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 导入共享模块（顶层符号保持兼容）
from pipelines.ebay_shared import (
    # keys
    reload_credentials,
    # constants
    GEN_CONCURRENCY, TEXT_CONCURRENCY, REVIEW_CONCURRENCY,
    TMP_DIR, MAX_RETRIES,
    # col mapping
    _cols, _init_col_defaults, _apply_adapter_cols,
    # status
    StatusReporter, _DASHBOARD_HOOK,
    # service
    _translate_svc,
    # helpers
    review_single, rule_strip_brands, _BRAND_PATTERN,
    clean_text_ai, translate_text,
    batch_translate_texts, batch_clean_texts,
    _strip_code_fence, _select_prompt, _parse_batch_response,
    _err_msg, _batch_process, _process_batch,
    # prompts
    TRANSLATE_PROMPT, TITLE_TRANSLATE_PROMPT, DESC_PROMPT,
    # regex
    _CHINESE_RE,
    # legacy aliases
    embed_new_images_in_desc,
)

# 导入管道编排
from pipelines.ebay_stages import _main as _pipeline_main

# === CLI 入口 ===
_RUN_DIRECTLY = __name__ == '__main__' or sys.argv[0].endswith(('process_ebay_tk.py', 'runner.py'))
TABLE_PATH = sys.argv[1] if _RUN_DIRECTLY and len(sys.argv) > 1 else None
if _RUN_DIRECTLY and not TABLE_PATH:
    print("用法: uv run python -u scripts/process_ebay_tk.py \"<输入文件.xlsx>\"")
    sys.exit(1)


def _main(table_path=None):
    """处理单个 xlsx。table_path 为 None 时用全局 TABLE_PATH（命令行/Web 兼容）。"""
    return _pipeline_main(table_path=table_path, _TABLE_PATH=TABLE_PATH)


def main():
    """异常保护：崩溃时在 status.json 写错误状态。"""
    try:
        _main()
    except SystemExit:
        raise
    except Exception as e:
        if TABLE_PATH:
            try:
                status = StatusReporter(TABLE_PATH)
                with open(status.status_path, 'w', encoding='utf-8') as f:
                    json.dump({'stage': '错误', 'error': f'{type(e).__name__}: {e}',
                               'updated_at': time.strftime('%Y-%m-%d %H:%M:%S')},
                              f, ensure_ascii=False, indent=2)
            except Exception:
                pass
        print(f"\n❌ 运行失败: {type(e).__name__}: {e}", flush=True)
        raise


if __name__ == '__main__':
    main()

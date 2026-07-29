#!/usr/bin/env python3
"""AST 调用图分析：从新路由模块反推 app.py 中的活代码。"""
import ast, sys, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / 'web'

def _extract_imports_from_app(filepath: Path) -> set[str]:
    """从路由文件里提取 `from web.app import X` 的名称。"""
    tree = ast.parse(filepath.read_text(encoding='utf-8'))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == 'web.app':
                for alias in node.names:
                    names.add(alias.name.split('.')[0])
    return names

def _extract_functions(filepath: Path) -> dict[str, tuple[int, int]]:
    """提取文件中所有顶层函数定义的名称和行号。"""
    tree = ast.parse(filepath.read_text(encoding='utf-8'))
    funcs = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef):
            funcs[node.name] = (node.lineno, node.end_lineno or node.lineno)
        elif isinstance(node, ast.AsyncFunctionDef):
            funcs[node.name] = (node.lineno, node.end_lineno or node.lineno)
    return funcs

def _extract_route_decorators(filepath: Path) -> list[tuple[int, str]]:
    """提取 @app.get/post/delete 装饰器的行号和 URL。"""
    tree = ast.parse(filepath.read_text(encoding='utf-8'))
    routes = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                    if isinstance(dec.func.value, ast.Name) and dec.func.value.id == 'app':
                        url_arg = dec.args[0] if dec.args else None
                        url = ast.literal_eval(url_arg) if url_arg else '?'
                        routes.append((node.lineno, url, node.name))
    return routes

# ── Step 1: 收集新路由模块从 app.py 导入的名称 ──
alive_names = set()
route_files = ['dashboard.py', 'settings.py', 'tasks.py', '_helpers.py']
for fname in route_files:
    path = WEB / 'routes' / fname
    if path.exists():
        names = _extract_imports_from_app(path)
        if names:
            print(f'{fname}: imports from app → {names}')
        alive_names |= names

print(f'\nTotal alive names from app.py: {sorted(alive_names)}')

# ── Step 2: 提取 app.py 中所有函数定义 ──
app_path = WEB / 'app.py'
all_funcs = _extract_functions(app_path)
print(f'\nAll functions in app.py ({len(all_funcs)}):')
for name, (start, end) in sorted(all_funcs.items(), key=lambda x: x[1][0]):
    marker = '★ ALIVE' if name in alive_names else '  dead'
    if name.startswith('_'):
        print(f'  {marker} L{start}-{end}: {name}')
    else:
        print(f'  {marker} L{start}-{end}: {name}  ← ROUTE')

# ── Step 3: 提取路由装饰器 ──
print('\nRoutes in app.py (with URL):')
routes = _extract_route_decorators(app_path)
for lineno, url, name in sorted(routes):
    print(f'  L{lineno}: @app.get/post("{url}") → {name}')

# ── Step 4: 分析死代码 ──
# 活函数 = 被新路由导入的 + 没被导入但它们被活函数调用的（闭环）
# 简化：只标记被 GET_HACKED 导入的为活，其余为死
dead_funcs = {name: (s, e) for name, (s, e) in all_funcs.items() if name not in alive_names}
dead_routes = [(lineno, url, name) for lineno, url, name in routes if name not in alive_names]

print(f'\n=== DEAD CODE ANALYSIS ===')
print(f'Dead functions (not imported by new routes): {len(dead_funcs)}')
for name, (s, e) in sorted(dead_funcs.items(), key=lambda x: x[1][0]):
    print(f'  L{s}-{e}: {name}')

print(f'\nDead routes (not covered by new routers): {len(dead_routes)}')
for lineno, url, name in sorted(dead_routes):
    print(f'  L{lineno}: {url} → {name}')

# ── Step 5: 安全删除建议 ──
print(f'\n=== SAFE TO DELETE ===')
# 被导入的 helper 不能删
# 路由函数 + 未被导入的 helper 可以删
# 但要小心：helper 之间可能有调用链
#
# 保守策略：只删除路由函数（@app.get/post 的），不删任何 helper
# 激进策略：删除路由函数 + 未被任何新路由模块导入的 helper
print('CONSERVATIVE: Delete only @app route functions')
for lineno, url, name in sorted(dead_routes):
    print(f'  DELETE route: L{lineno} {url} ({name})')

print('\nAGGRESSIVE: Also delete unused helpers')
for name, (s, e) in sorted(dead_funcs.items(), key=lambda x: x[1][0]):
    print(f'  DELETE helper: L{s}-{e} {name}')

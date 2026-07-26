"""自动更新：检测同目录或 _update 子目录下的新版本 exe，有则替换后重启。"""
import os, sys, json, shutil, time
from web import __version__


def _parse_version(v):
    """v0.2.0 → (0,2,0)"""
    try:
        return tuple(int(x) for x in v.lstrip('v').split('.')[:3])
    except Exception:  # 版本号解析失败，返回默认值
        return (0, 0, 0)


def check_for_update(exe_dir=None):
    """检测 _update/CrossPilot.exe 或同目录 new/CrossPilot.exe 是否有新版本。
    返回 {'version': str, 'path': str} 或 None。
    """
    exe_dir = exe_dir or os.path.dirname(sys.executable)
    current = _parse_version(__version__)
    search_dirs = [
        os.path.join(exe_dir, '_update'),
        os.path.join(exe_dir, 'new'),
    ]
    for d in search_dirs:
        candidate = os.path.join(d, 'CrossPilot.exe')
        if not os.path.exists(candidate):
            continue
        # 读 candidate 的版本信息（exe 启动时写 version.txt 到同目录）
        ver_file = os.path.join(d, 'version.txt')
        try:
            with open(ver_file) as f:
                candidate_ver = _parse_version(f.read().strip())
        except Exception:  # version.txt 读取失败
            candidate_ver = (0, 0, 0)
        if candidate_ver > current:
            return {'version': f"v{candidate_ver[0]}.{candidate_ver[1]}.{candidate_ver[2]}", 'path': candidate}
    return None


def apply_update(update_path):
    """替换当前 exe 为新版本，写批处理脚本在下次启动时完成替换。
    返回 True 表示已准备好（下次启动自动替换）。"""
    current_exe = sys.executable
    if not current_exe or not os.path.exists(current_exe):
        return False
    target_dir = os.path.dirname(current_exe)
    target_name = os.path.basename(current_exe)

    # 写替换脚本（Windows batch）
    bat = os.path.join(target_dir, '_update.bat')
    with open(bat, 'w') as f:
        f.write(f"""@echo off
timeout /t 2 /nobreak >nul
move /Y "{target_name}" "{target_name}.old" 2>nul
move /Y "{update_path}" "{os.path.join(target_dir, target_name)}"
if exist "{os.path.join(target_dir, target_name)}" start "" "{os.path.join(target_dir, target_name)}"
del "{target_name}.old" 2>nul
del "%~f0"
""")
    return True


def write_version_file(exe_dir=None):
    """启动时写 version.txt，供 check_for_update 比较版本号。"""
    exe_dir = exe_dir or os.path.dirname(sys.executable)
    try:
        with open(os.path.join(exe_dir, 'version.txt'), 'w') as f:
            f.write(__version__)
    except Exception:  # 版本文件写入失败不影响
        pass
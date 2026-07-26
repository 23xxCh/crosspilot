"""本地更新：校验 _update/new 目录中的新版本，再由辅助进程替换并重启。"""
import hashlib
import os
import subprocess
import sys
from web import __version__


def _parse_version(v):
    """v0.2.0 → (0,2,0)"""
    try:
        return tuple(int(x) for x in v.lstrip('v').split('.')[:3])
    except Exception:  # 版本号解析失败，返回默认值
        return (0, 0, 0)


def _signature_required():
    return os.name == 'nt' and bool(getattr(sys, 'frozen', False))


def _authenticode_thumbprint(path):
    """Return a valid Authenticode signer thumbprint, or an empty string."""
    if os.name != 'nt':
        return ''
    env = os.environ.copy()
    env['CROSSPILOT_SIGNATURE_PATH'] = path
    script = (
        "$s=Get-AuthenticodeSignature -LiteralPath $env:CROSSPILOT_SIGNATURE_PATH;"
        "if($s.Status -eq 'Valid' -and $s.SignerCertificate){"
        "$s.SignerCertificate.Thumbprint}"
    )
    try:
        result = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ''
    return result.stdout.strip().upper() if result.returncode == 0 else ''


def _has_matching_signature(candidate):
    current_thumbprint = _authenticode_thumbprint(sys.executable)
    candidate_thumbprint = _authenticode_thumbprint(candidate)
    return bool(
        current_thumbprint
        and candidate_thumbprint
        and current_thumbprint == candidate_thumbprint
    )


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
        checksum_file = candidate + '.sha256'
        try:
            with open(checksum_file, encoding='ascii') as f:
                expected = f.read().strip().split()[0].lower()
            with open(candidate, 'rb') as f:
                actual = hashlib.file_digest(f, 'sha256').hexdigest()
            with open(candidate, 'rb') as f:
                is_pe = f.read(2) == b'MZ'
        except (OSError, IndexError):
            continue
        signature_ok = not _signature_required() or _has_matching_signature(candidate)
        if candidate_ver > current and is_pe and expected == actual and signature_ok:
            return {'version': f"v{candidate_ver[0]}.{candidate_ver[1]}.{candidate_ver[2]}", 'path': candidate}
    return None


def apply_update(update_path):
    """启动受控辅助脚本，等待当前进程退出后替换 exe 并重新启动。"""
    if os.name != 'nt' or not getattr(sys, 'frozen', False):
        return False
    current_exe = sys.executable
    if not current_exe or not os.path.exists(current_exe):
        return False
    target_dir = os.path.dirname(current_exe)
    target_name = os.path.basename(current_exe)
    update_path = os.path.realpath(update_path)
    allowed_dirs = [
        os.path.realpath(os.path.join(target_dir, '_update')),
        os.path.realpath(os.path.join(target_dir, 'new')),
    ]
    if not any(os.path.commonpath([update_path, d]) == d for d in allowed_dirs):
        return False
    verified = check_for_update(target_dir)
    if not verified or os.path.realpath(verified['path']) != update_path:
        return False

    bat = os.path.join(target_dir, '_update.bat')
    old_path = os.path.join(target_dir, target_name)
    with open(bat, 'w', encoding='utf-8', newline='\r\n') as f:
        f.write(f"""@echo off
setlocal
del /Q "{old_path}.old" >nul 2>&1
for /L %%i in (1,1,30) do (
  move /Y "{old_path}" "{old_path}.old" >nul 2>&1 && goto replace
  timeout /t 1 /nobreak >nul
)
exit /b 1
:replace
move /Y "{update_path}" "{old_path}" >nul 2>&1
if not exist "{old_path}" (
  move /Y "{old_path}.old" "{old_path}" >nul 2>&1
  exit /b 1
)
start "" "{old_path}"
del /Q "{old_path}.old" >nul 2>&1
del "%~f0"
""")
    subprocess.Popen(
        ['cmd.exe', '/c', bat],
        cwd=target_dir,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        close_fds=True,
    )
    return True


def write_version_file(exe_dir=None):
    """启动时写 version.txt，供 check_for_update 比较版本号。"""
    exe_dir = exe_dir or os.path.dirname(sys.executable)
    try:
        with open(os.path.join(exe_dir, 'version.txt'), 'w') as f:
            f.write(__version__)
    except Exception:  # 版本文件写入失败不影响
        pass

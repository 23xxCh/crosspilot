import hashlib

from web import updater
from web.updater import check_for_update


def _write_candidate(root, version='9.0.0', checksum=True):
    update_dir = root / '_update'
    update_dir.mkdir()
    candidate = update_dir / 'CrossPilot.exe'
    candidate.write_bytes(b'MZ' + b'\0' * 64)
    (update_dir / 'version.txt').write_text(version, encoding='ascii')
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    if checksum:
        (update_dir / 'CrossPilot.exe.sha256').write_text(digest, encoding='ascii')
    return candidate


def test_update_candidate_requires_matching_checksum(tmp_path):
    candidate = _write_candidate(tmp_path)

    update = check_for_update(str(tmp_path))

    assert update['path'] == str(candidate)
    assert update['version'] == 'v9.0.0'


def test_update_candidate_rejects_bad_checksum(tmp_path):
    candidate = _write_candidate(tmp_path)
    candidate.with_suffix('.exe.sha256').write_text('0' * 64, encoding='ascii')

    assert check_for_update(str(tmp_path)) is None


def test_frozen_update_rejects_different_signer(tmp_path, monkeypatch):
    _write_candidate(tmp_path)
    monkeypatch.setattr(updater, '_signature_required', lambda: True)
    monkeypatch.setattr(
        updater,
        '_authenticode_thumbprint',
        lambda path: 'CURRENT' if path == updater.sys.executable else 'OTHER',
    )

    assert check_for_update(str(tmp_path)) is None


def test_frozen_update_accepts_same_signer(tmp_path, monkeypatch):
    candidate = _write_candidate(tmp_path)
    monkeypatch.setattr(updater, '_signature_required', lambda: True)
    monkeypatch.setattr(updater, '_authenticode_thumbprint', lambda _path: 'SAME')

    assert check_for_update(str(tmp_path))['path'] == str(candidate)

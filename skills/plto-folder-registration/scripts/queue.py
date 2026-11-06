"""Local queue only: no browser, network, spreadsheet mutation or product submission."""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def children(root):
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink()
                  and not p.name.startswith(('.', '_')) and p.resolve().parent == root)


def fingerprint(folder):
    h = hashlib.sha256()
    newest = 0
    for p in sorted(folder.rglob('*')):
        rel = p.relative_to(folder)
        if any(part.startswith(('.', '_', '~$')) for part in rel.parts):
            continue
        if p.is_symlink() or not p.is_file():
            continue
        if not p.resolve().is_relative_to(folder):
            raise ValueError('Linked input outside product folder')
        before = p.stat()
        newest = max(newest, before.st_mtime)
        h.update(rel.as_posix().encode('utf-8'))
        with p.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                h.update(block)
        after = p.stat()
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise ValueError('Input changed while scanning')
        if p.name == '등록준비완료.txt':
            h.update(str(after.st_mtime_ns).encode())
    return h.hexdigest(), newest


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--config', type=Path, required=True)
    ap.add_argument('command', choices=['init', 'scan', 'claim', 'checkpoint', 'release'])
    ap.add_argument('folder', nargs='?')
    ap.add_argument('receipt', nargs='?', type=Path)
    args = ap.parse_args()
    cfg = read(args.config)
    root = Path(cfg['watch_root']).resolve(strict=True)
    state_dir = Path(cfg['state_dir']).resolve()
    state_path, lock = state_dir / 'state.json', state_dir / 'run.lock'
    if args.command == 'init':
        if state_path.exists():
            raise ValueError('Already initialized; refusing to reset baseline')
        write(state_path, {'version': 1, 'watch_root': str(root), 'created_at': time.time(),
                           'baseline': [p.name for p in children(root)], 'products': {}})
        return {'initialized': True, 'excluded_existing': len(read(state_path)['baseline'])}
    state = read(state_path)
    if Path(state['watch_root']).resolve() != root:
        raise ValueError('Watch root differs from baseline')
    if args.command == 'release':
        if not lock.exists():
            raise ValueError('No active lock')
        lock.unlink()
        return {'released': True}
    if args.command == 'scan':
        if lock.exists():
            return {'candidates': [], 'locked': True, 'lock': read(lock)}
        candidates = []
        for folder in children(root):
            if folder.name in state['baseline']:
                continue
            if not (folder / '등록준비완료.txt').is_file():
                continue
            old = state['products'].get(folder.name, {})
            if old.get('phase') == 'done':
                continue
            digest, newest = fingerprint(folder)
            if time.time() - newest < cfg.get('settle_seconds', 120):
                continue
            if old.get('phase') == 'blocked' and old.get('fingerprint') == digest:
                continue
            candidates.append({'folder': str(folder), 'fingerprint': digest,
                               'input_changed': bool(old and old.get('fingerprint') != digest),
                               'checkpoint': old})
        return {'candidates': candidates, 'locked': False}
    folder = Path(args.folder or '').resolve(strict=True)
    if folder.parent != root or folder.name in state['baseline']:
        raise ValueError('Not an eligible new product folder')
    if not (folder / '등록준비완료.txt').is_file():
        raise ValueError('Readiness marker missing')
    if args.command == 'claim':
        if state['products'].get(folder.name, {}).get('phase') == 'done':
            raise ValueError('Already done')
        digest, newest = fingerprint(folder)
        if time.time() - newest < cfg.get('settle_seconds', 120):
            raise ValueError('Files are still settling')
        with lock.open('x', encoding='utf-8') as f:
            json.dump({'folder': str(folder), 'fingerprint': digest, 'started_at': time.time()}, f)
        return {'claimed': str(folder), 'checkpoint': state['products'].get(folder.name, {})}
    active = read(lock)
    if active['folder'] != str(folder):
        raise ValueError('Lock belongs to a different product')
    receipt = read(args.receipt)
    if receipt.get('phase') not in {'prepared', 'registering', 'registered', 'transmitting', 'done', 'blocked'}:
        raise ValueError('Invalid phase')
    previous = state['products'].get(folder.name, {})
    if previous.get('plto_code') and receipt.get('plto_code', previous['plto_code']) != previous['plto_code']:
        raise ValueError('Cannot replace existing PLTO identity')
    previous.update(receipt)
    previous.update(fingerprint=active['fingerprint'], updated_at=time.time())
    state['products'][folder.name] = previous
    write(state_path, state)
    return {'saved': folder.name, 'phase': previous['phase']}


if __name__ == '__main__':
    try:
        print(json.dumps(main(), ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        sys.exit(1)

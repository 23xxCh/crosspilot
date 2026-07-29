"""Folder watcher — poll watch/input/, auto-process new files."""
from __future__ import annotations

import os
import json
import time
import shutil
import threading
from pathlib import Path
from typing import Set

_ROOT = Path(__file__).resolve().parent.parent


class FolderWatcher:
    """Poll a directory for new files, run pipeline on each."""

    def __init__(self, watch_dir: str | None = None):
        d = Path(watch_dir or os.environ.get('CROSSPILOT_WATCH_DIR', str(_ROOT / 'watch')))
        self.input_dir = d / 'input'
        self.processing_dir = d / 'processing'
        self.output_dir = d / 'output'
        self.failed_dir = d / 'failed'
        for p in [self.input_dir, self.processing_dir, self.output_dir, self.failed_dir]:
            p.mkdir(parents=True, exist_ok=True)

        self._stop = threading.Event()
        self._processing: Set[str] = set()
        self._lock = threading.Lock()

    def start(self, poll_interval: float = 2.0):
        """Start watching in current thread. Non-blocking: use start_background()."""
        print(f'[watcher] Watching {self.input_dir}')
        print(f'[watcher] Output -> {self.output_dir}')
        while not self._stop.is_set():
            self._tick()
            self._stop.wait(poll_interval)

    def start_background(self, poll_interval: float = 2.0) -> threading.Thread:
        """Start watching in a background daemon thread."""
        t = threading.Thread(target=self.start, args=(poll_interval,), daemon=True)
        t.start()
        return t

    def stop(self):
        self._stop.set()

    def _tick(self):
        try:
            files = list(self.input_dir.iterdir())
        except OSError:
            return

        for f in sorted(files):
            if not f.is_file():
                continue
            if f.name.startswith('.'):
                continue
            if f.suffix.lower() not in ('.json', '.xlsx', '.xls'):
                continue

            with self._lock:
                if f.name in self._processing:
                    continue
                # Wait for file to stabilize (stop being written)
                size1 = f.stat().st_size
                time.sleep(0.5)
                if f.stat().st_size != size1:
                    continue
                self._processing.add(f.name)

            self._process(f)

    def _process(self, input_file: Path):
        """Process one file through the pipeline."""
        from scripts.process_amazon import run_amazon_pipeline

        stem = input_file.stem
        processing_path = self.processing_dir / input_file.name
        out_dir = self.output_dir / stem

        try:
            # Move to processing
            shutil.move(str(input_file), str(processing_path))

            # Run pipeline
            print(f'[watcher] Processing: {input_file.name}')
            t0 = time.time()
            output = run_amazon_pipeline(str(processing_path))
            elapsed = time.time() - t0

            # Move output to output dir
            out_dir.mkdir(parents=True, exist_ok=True)
            output_path = Path(output)
            if output_path.exists():
                shutil.move(str(output_path), str(out_dir / output_path.name))

            # Also move cache and metrics if they exist
            cache_path = processing_path.with_suffix('').with_name(processing_path.stem + '_amz_cache.json')
            if cache_path.exists():
                shutil.move(str(cache_path), str(out_dir / cache_path.name))

            # Write status
            status = {
                'status': 'done',
                'input_file': input_file.name,
                'output_dir': str(out_dir),
                'elapsed_seconds': int(elapsed),
            }
            with open(out_dir / 'status.json', 'w', encoding='utf-8') as f:
                json.dump(status, f, ensure_ascii=False, indent=2)

            print(f'[watcher] Done: {input_file.name} ({elapsed:.0f}s) -> {out_dir}')

        except Exception as e:
            # Move to failed
            failed_path = self.failed_dir / processing_path.name
            shutil.move(str(processing_path), str(failed_path))
            error_msg = f'{type(e).__name__}: {e}'
            with open(self.failed_dir / f'{stem}.error.txt', 'w', encoding='utf-8') as f:
                f.write(error_msg)
            print(f'[watcher] FAILED: {input_file.name} -> {error_msg}')

        finally:
            with self._lock:
                self._processing.discard(input_file.name)


def start_watcher(watch_dir: str | None = None) -> FolderWatcher:
    """Convenience: create and start a watcher in background."""
    w = FolderWatcher(watch_dir)
    w.start_background()
    return w

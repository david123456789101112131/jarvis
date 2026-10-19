"""Speed measurements: how long each kind of work took, kept on this computer to report real numbers.

Only measurement names and durations are stored — never a request's text, a path or a result — in
`<data>/metrics.json`: the last `KEEP` samples of each measurement, plus named snapshots of the summary
("before" / "after" a speed change) so a comparison is made from what was really measured. The UI shows
the median and the slowest tenth (p90) of each measurement; the privacy area can clear them.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from jarvis.common.clock import iso

log = logging.getLogger(__name__)

KEEP = 200                # samples kept per measurement
SAVE_EVERY_S = 5.0        # the file is rewritten at most this often (and at shutdown)


def _percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, round(fraction * (len(sorted_values) - 1))))
    return sorted_values[index]


class Metrics:
    def __init__(self, file: Path | None = None, *, keep: int = KEEP) -> None:
        self.file = file
        self.keep = keep
        self._lock = threading.Lock()
        self._samples: dict[str, list[tuple[float, bool, str]]] = {}    # name -> [(ms, ok, at)]
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self._saved_at = 0.0
        self._load()

    # ------------------------------------------------------------------ recording
    def record(self, name: str, ms: float, *, ok: bool = True) -> None:
        if not name or ms < 0:
            return
        with self._lock:
            rows = self._samples.setdefault(name, [])
            rows.append((round(float(ms), 1), bool(ok), iso()))
            if len(rows) > self.keep:
                del rows[: len(rows) - self.keep]
            self._dirty = True
            due = time.monotonic() - self._saved_at >= SAVE_EVERY_S
        if due:
            self.flush()

    @contextlib.contextmanager
    def timer(self, name: str) -> Iterator[None]:
        """Measure the block; a block that raises is recorded as not ok."""
        started = time.perf_counter()
        ok = False
        try:
            yield
            ok = True
        finally:
            self.record(name, (time.perf_counter() - started) * 1000, ok=ok)

    # ------------------------------------------------------------------ reading
    def summary(self, prefix: str | None = None) -> dict[str, dict[str, Any]]:
        with self._lock:
            items = {k: list(v) for k, v in self._samples.items() if prefix is None or k.startswith(prefix)}
        out: dict[str, dict[str, Any]] = {}
        for name, rows in sorted(items.items()):
            values = sorted(ms for ms, _ok, _at in rows)
            last_ms, _last_ok, last_at = rows[-1]
            out[name] = {
                "count": len(rows),
                "ok": sum(1 for _ms, ok, _at in rows if ok),
                "median_ms": _percentile(values, 0.5),
                "p90_ms": _percentile(values, 0.9),
                "min_ms": values[0],
                "max_ms": values[-1],
                "last_ms": last_ms,
                "last_at": last_at,
            }
        return out

    def samples(self, name: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._samples.get(name, []))[-limit:]
        return [{"ms": ms, "ok": ok, "at": at} for ms, ok, at in rows]

    def snapshot(self, label: str) -> dict[str, Any]:
        """Keep the current summary under `label` (for example "before" a speed change)."""
        snap = {"at": iso(), "summary": self.summary()}
        with self._lock:
            self._snapshots[label[:40]] = snap
            self._dirty = True
        self.flush()
        return snap

    def snapshots(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self._snapshots))

    def view(self) -> dict[str, Any]:
        return {"summary": self.summary(), "snapshots": self.snapshots()}

    def clear(self) -> None:
        with self._lock:
            self._samples.clear()
            self._snapshots.clear()
            self._dirty = True
        self.flush()

    # ------------------------------------------------------------------ the file
    def _load(self) -> None:
        if self.file is None or not self.file.is_file():
            return
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("Could not read %s; speed measurements start empty", self.file)
            return
        for name, rows in (data.get("samples") or {}).items():
            clean = [(float(r[0]), bool(r[1]), str(r[2])) for r in rows if isinstance(r, list) and len(r) == 3]
            if clean:
                self._samples[str(name)] = clean[-self.keep:]
        snaps = data.get("snapshots")
        if isinstance(snaps, dict):
            self._snapshots = {str(k): v for k, v in snaps.items() if isinstance(v, dict)}

    def flush(self) -> None:
        if self.file is None:
            return
        with self._lock:
            if not self._dirty:
                return
            data = {"samples": {k: [list(r) for r in v] for k, v in self._samples.items()},
                    "snapshots": self._snapshots}
            self._dirty = False
            self._saved_at = time.monotonic()
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.file)
        except OSError:
            log.warning("Could not save the speed measurements", exc_info=True)


__all__ = ["KEEP", "Metrics"]

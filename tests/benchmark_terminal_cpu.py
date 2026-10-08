"""Compare terminal CPU against a Git revision using deterministic, content-free input.

Run: uv run python tests/benchmark_terminal_cpu.py --baseline main
Wall-clock thresholds are deliberately excluded from pytest; regression tests assert
zero hidden-view wakes and bounded parser feeds independently of machine speed.
"""

from __future__ import annotations

import argparse
import importlib
import json
import random
import statistics
import subprocess
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAMES = (
    b"\x1b[?2026h\x1b[?25l\x1b[8;2H\x1b[36m*\x1b[0m\x1b[9;3H\x1b[?25h\x1b[?2026l",
    b"\x1b[?2026h\x1b[8;1H\x1b[2KWorking\x1b[9;3H\x1b[?2026l",
    "\x1b[2;1H世界e\u0301\x1b[31mtext\x1b[0m\x1b[6n".encode(),
    b"\x1b]0;benchmark\x07\x1bP+q544e\x1b\\\x1b[c\x1b[3;2H\x1b[?6n",
    b"\x1b%@\xe9\x1b%G\xc3\xa9\x1b%@\xe9\x1b%8\xc3\xa9",
)


def baseline_module(revision: str, name: str) -> types.ModuleType:
    source = subprocess.check_output(["git", "show", f"{revision}:src/rodex/{name}.py"], cwd=ROOT, text=True)
    module = types.ModuleType(f"rodex._benchmark_baseline_{name}")
    sys.modules[module.__name__] = module
    exec(compile(source, f"{revision}/{name}.py", "exec"), module.__dict__)
    return module


def projection_state(projection) -> tuple:
    screen = projection.screen
    return (
        screen.buffer,
        (screen.cursor.x, screen.cursor.y, screen.cursor.attrs, screen.cursor.hidden),
        screen.mode,
        screen.margins,
        projection.at_boundary,
        projection.paintable,
    )


def check_projection_equivalence(baseline, current) -> int:
    randomizer = random.Random(20261008)
    corpus = b"".join(FRAMES * 10)
    checks = 0
    for _ in range(50):
        before, after = baseline.NativeTerminalProjection(80, 24), current.NativeTerminalProjection(80, 24)
        offset = 0
        while offset < len(corpus):
            width = randomizer.randrange(1, 128)
            data = corpus[offset : offset + width]
            old, new = before.feed(data), after.feed(data)
            assert (old.native, old.controls, old.replies) == (new.native, new.controls, new.replies)
            assert projection_state(before) == projection_state(after)
            offset += width
            checks += 1
    return checks


def median_cpu(action, repeats: int) -> float:
    samples = []
    for _ in range(repeats):
        started = time.process_time()
        action()
        samples.append(time.process_time() - started)
    return statistics.median(samples)


def measure_projection(module, count: int, repeats: int) -> float:
    def run() -> None:
        projection = module.NativeTerminalProjection(80, 24)
        for index in range(count):
            projection.feed(FRAMES[index % len(FRAMES)])

    return median_cpu(run, repeats)


def presentation_fixture(module):
    presentation = module.SessionPresentationPipeline()
    presentation.bind_root_thread("root")
    for index in range(128):
        presentation.observe_protocol_output(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "root",
                    "item": {"id": str(index), "type": "agentMessage", "phase": "commentary", "text": "sample"},
                },
            }
        )
    for _ in range(512):
        presentation.observe_protocol_output(delta())
    return presentation


def delta() -> dict:
    return {"method": "item/agentMessage/delta", "params": {"threadId": "root", "itemId": "127", "delta": "x"}}


def measure_presentation(module, count: int, repeats: int) -> tuple[float, int]:
    wakes = 0

    def run() -> None:
        nonlocal wakes
        presentation = presentation_fixture(module)
        wakes = 0

        def sync_view() -> None:
            nonlocal wakes
            wakes += 1
            presentation.snapshot()

        presentation.subscribe(sync_view)
        for _ in range(count):
            presentation.observe_protocol_output(delta())

    elapsed = median_cpu(run, repeats)
    return elapsed, wakes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="main")
    parser.add_argument("--count", type=int, default=5000)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.count < 1 or args.repeats < 1:
        parser.error("count and repeats must be positive")
    baseline_projection = baseline_module(args.baseline, "native_terminal_projection")
    projection = importlib.import_module("rodex.native_terminal_projection")
    equivalence_checks = check_projection_equivalence(baseline_projection, projection)
    before = measure_projection(baseline_projection, args.count, args.repeats)
    after = measure_projection(projection, args.count, args.repeats)
    baseline_presentation = baseline_module(args.baseline, "presentation_policy")
    presentation = importlib.import_module("rodex.presentation_policy")
    old_cpu, old_wakes = measure_presentation(baseline_presentation, args.count, args.repeats)
    new_cpu, new_wakes = measure_presentation(presentation, args.count, args.repeats)
    print(
        json.dumps(
            {
                "baseline": args.baseline,
                "count": args.count,
                "repeats": args.repeats,
                "projection_equivalence_checks": equivalence_checks,
                "projection": {
                    "before_cpu_s": before,
                    "after_cpu_s": after,
                    "reduction_percent": 100 * (1 - after / before),
                },
                "native_presentation": {
                    "before_cpu_s": old_cpu,
                    "after_cpu_s": new_cpu,
                    "reduction_percent": 100 * (1 - new_cpu / old_cpu),
                    "before_wakes": old_wakes,
                    "after_wakes": new_wakes,
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

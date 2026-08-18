#!/usr/bin/env python3
"""Render measured-window Samply CPU or off-CPU samples as a self-contained SVG flame graph."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from analyze_samply import build_symbol_resolver, resolve_thread_frames, stack_frames


def load_json(path: Path) -> Any:
    if path.suffix == ".gz":
        with gzip.open(path, "rt") as stream:
            return json.load(stream)
    return json.loads(path.read_text())


@dataclass
class Node:
    name: str
    value: float = 0.0
    children: dict[str, "Node"] = field(default_factory=dict)


def add_stack(root: Node, stack: tuple[str, ...], value: float) -> None:
    root.value += value
    node = root
    for name in stack:
        node = node.children.setdefault(name, Node(name))
        node.value += value


def color(name: str) -> str:
    digest = hashlib.sha256(name.encode()).digest()
    return f"rgb({205 + digest[0] % 45},{75 + digest[1] % 95},{35 + digest[2] % 55})"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--sidecar", type=Path)
    parser.add_argument("--mode", required=True, choices=("cpu", "offcpu"))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    profile = load_json(args.profile)
    manifest = load_json(args.manifest)
    sidecar = load_json(args.sidecar) if args.sidecar and args.sidecar.is_file() else None
    resolver = build_symbol_resolver(sidecar)
    profile_start_ms = float(profile["meta"]["startTime"])
    window_start_ms = float(manifest["boundaries"]["start_epoch_ms"])
    window_end_ms = float(manifest["boundaries"]["completion_epoch_ms"])

    root = Node("all threads")
    max_depth = 0
    sample_rows = 0
    for thread in profile["threads"]:
        frame_names = resolve_thread_frames(thread, profile["libs"], resolver)
        samples = thread["samples"]
        weights = samples.get("weight")
        cpu_deltas = samples.get("threadCPUDelta")
        for index in range(samples["length"]):
            timestamp_ms = profile_start_ms + samples["time"][index]
            if not window_start_ms <= timestamp_ms <= window_end_ms:
                continue
            cpu_us = float(cpu_deltas[index] or 0) if cpu_deltas else 0.0
            if args.mode == "cpu":
                value = cpu_us
                if value <= 0:
                    continue
            else:
                if cpu_us != 0:
                    continue
                value = float(weights[index]) if weights else 1.0
            stack = (f"thread: {thread['name']} [{thread['tid']}]",) + stack_frames(
                thread, frame_names, samples["stack"][index]
            )
            add_stack(root, stack, value)
            max_depth = max(max_depth, len(stack))
            sample_rows += 1

    if root.value <= 0:
        raise RuntimeError(f"no {args.mode} samples in measured window")

    width = 1800
    margin = 12
    header = 62
    frame_height = 18
    height = header + (max_depth + 1) * frame_height + 20
    usable_width = width - margin * 2
    unit = "thread CPU microseconds" if args.mode == "cpu" else "weighted off-CPU samples"
    title = f"{manifest['run_id']} — measured-window {args.mode.upper()} flame graph"
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        "<style>text{font-family:Menlo,monospace;font-size:11px;fill:#111}.title{font-size:18px;font-weight:bold}"
        ".subtitle{font-size:12px;fill:#444}rect{stroke:#fff;stroke-width:.5}</style>",
        '<rect width="100%" height="100%" fill="#fafafa"/>',
        f'<text class="title" x="{margin}" y="24">{html.escape(title)}</text>',
        f'<text class="subtitle" x="{margin}" y="45">width = {unit}; '
        f'{sample_rows:,} samples; total = {root.value:,.3f}</text>',
    ]

    def draw(node: Node, depth: int, x: float, node_width: float) -> None:
        if depth > 0 and node_width >= 0.25:
            y = header + (max_depth - depth) * frame_height
            pct = node.value / root.value * 100
            tooltip = f"{node.name}\n{node.value:,.3f} {unit} ({pct:.3f}%)"
            parts.append(
                f'<g><title>{html.escape(tooltip)}</title><rect x="{x:.3f}" y="{y}" '
                f'width="{node_width:.3f}" height="{frame_height - 1}" fill="{color(node.name)}"/>'
            )
            max_chars = max(0, int(node_width / 7.0) - 1)
            label = node.name if len(node.name) <= max_chars else node.name[: max(0, max_chars - 2)] + ".."
            if max_chars >= 4:
                parts.append(
                    f'<text x="{x + 3:.3f}" y="{y + 12}">{html.escape(label)}</text>'
                )
            parts.append("</g>")
        child_x = x
        for child in sorted(node.children.values(), key=lambda item: item.value, reverse=True):
            child_width = node_width * child.value / node.value
            draw(child, depth + 1, child_x, child_width)
            child_x += child_width

    draw(root, 0, margin, usable_width)
    parts.append("</svg>")
    args.output.write_text("\n".join(parts) + "\n")
    print(json.dumps({"mode": args.mode, "samples": sample_rows, "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

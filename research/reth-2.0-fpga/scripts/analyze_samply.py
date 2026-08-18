#!/usr/bin/env python3
"""Reduce a Samply Firefox profile to measured-window CPU and off-CPU stack evidence."""

from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    if path.suffix == ".gz":
        with gzip.open(path, "rt") as stream:
            return json.load(stream)
    return json.loads(path.read_text())


def build_symbol_resolver(sidecar: dict[str, Any] | None):
    if sidecar is None:
        return {}
    strings = sidecar["string_table"]
    result: dict[str, list[tuple[int, int, str]]] = {}
    for image in sidecar["data"]:
        # The frozen maxperf Reth binary is intentionally stripped and exposes only a handful of
        # enormous C++ ranges in the monolithic image. A profiling build with debug symbols exposes
        # thousands of narrow symbols and is safe to resolve.
        if image["debug_name"] == "reth" and len(image.get("symbol_table", [])) < 1_000:
            result[image["debug_name"]] = []
            continue
        rows = []
        for symbol in image.get("symbol_table", []):
            name = strings[symbol["symbol"]]
            if name == "_mh_execute_header":
                continue
            rows.append((symbol["rva"], symbol["rva"] + symbol["size"], name))
        result[image["debug_name"]] = rows
    return result


def resolve_thread_frames(thread: dict[str, Any], libs: list[dict[str, Any]], resolver) -> list[str]:
    strings = thread["stringArray"]
    functions = thread["funcTable"]
    resources = thread["resourceTable"]
    frames = thread["frameTable"]
    names = []
    for frame_index in range(frames["length"]):
        func_index = frames["func"][frame_index]
        raw = strings[functions["name"][func_index]]
        resource_index = functions["resource"][func_index]
        lib_index = resources["lib"][resource_index] if resource_index is not None else None
        lib = libs[lib_index] if lib_index is not None and lib_index >= 0 else None
        lib_name = lib["name"] if lib else "unknown"
        address = frames["address"][frame_index]
        resolved = None
        if lib:
            for start, end, symbol in resolver.get(lib["debugName"], []):
                if start <= address < end:
                    resolved = symbol
                    break
        if resolved:
            names.append(resolved)
        elif not raw.startswith("0x"):
            names.append(raw)
        else:
            names.append(f"{lib_name}+0x{address:x}")
    return names


def stack_frames(thread: dict[str, Any], frame_names: list[str], stack_index: int | None) -> tuple[str, ...]:
    if stack_index is None:
        return ("[no stack]",)
    table = thread["stackTable"]
    result = []
    while stack_index is not None:
        result.append(frame_names[table["frame"][stack_index]])
        stack_index = table["prefix"][stack_index]
    result.reverse()
    return tuple(result)


def ranked(counter, total: float, limit: int, unit: str):
    return [
        {"value": value, "unit": unit, "ratio": value / total if total else 0.0, "key": key}
        for key, value in counter.most_common(limit)
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--sidecar", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    profile = load_json(args.profile)
    manifest = load_json(args.manifest)
    sidecar = load_json(args.sidecar) if args.sidecar and args.sidecar.is_file() else None
    resolver = build_symbol_resolver(sidecar)
    interval_ms = float(profile["meta"]["interval"])
    profile_start_ms = float(profile["meta"]["startTime"])
    window_start_ms = float(manifest["boundaries"]["start_epoch_ms"])
    window_end_ms = float(manifest["boundaries"]["completion_epoch_ms"])

    thread_wall = collections.Counter()
    thread_cpu = collections.Counter()
    wall_leaf = collections.Counter()
    cpu_leaf = collections.Counter()
    offcpu_leaf = collections.Counter()
    wall_stacks = collections.Counter()
    cpu_stacks = collections.Counter()
    offcpu_stacks = collections.Counter()
    thread_details = {}
    sample_rows = 0
    weighted_samples = 0.0
    cpu_us_total = 0.0
    oncpu_weighted_samples = 0.0
    offcpu_weighted_samples = 0.0

    for thread in profile["threads"]:
        frame_names = resolve_thread_frames(thread, profile["libs"], resolver)
        samples = thread["samples"]
        thread_name = thread["name"]
        local_wall = collections.Counter()
        local_cpu = collections.Counter()
        local_offcpu = collections.Counter()
        local_cpu_us = 0.0
        local_weight = 0.0
        weights = samples.get("weight")
        cpu_deltas = samples.get("threadCPUDelta")
        for index in range(samples["length"]):
            timestamp_ms = profile_start_ms + samples["time"][index]
            if timestamp_ms < window_start_ms or timestamp_ms > window_end_ms:
                continue
            weight = float(weights[index]) if weights else 1.0
            cpu_us = float(cpu_deltas[index] or 0) if cpu_deltas else 0.0
            stack = stack_frames(thread, frame_names, samples["stack"][index])
            leaf = stack[-1]
            stack_id = hashlib.sha256(";".join(stack).encode()).hexdigest()[:16]
            compact_stack = f"{stack_id} | " + ";".join(stack[-10:])
            sample_rows += 1
            weighted_samples += weight
            cpu_us_total += cpu_us
            if cpu_us == 0:
                offcpu_weighted_samples += weight
            else:
                oncpu_weighted_samples += weight
            local_weight += weight
            local_cpu_us += cpu_us
            thread_wall[thread_name] += weight
            thread_cpu[thread_name] += cpu_us
            wall_leaf[f"{thread_name} | {leaf}"] += weight
            cpu_leaf[f"{thread_name} | {leaf}"] += cpu_us
            wall_stacks[f"{thread_name} | {compact_stack}"] += weight
            cpu_stacks[f"{thread_name} | {compact_stack}"] += cpu_us
            local_wall[compact_stack] += weight
            local_cpu[compact_stack] += cpu_us
            if cpu_us == 0:
                offcpu_leaf[f"{thread_name} | {leaf}"] += weight
                offcpu_stacks[f"{thread_name} | {compact_stack}"] += weight
                local_offcpu[compact_stack] += weight
        if local_weight:
            local_offcpu_weight = sum(local_offcpu.values())
            thread_details[f"{thread_name}:{thread['tid']}"] = {
                "weighted_samples": local_weight,
                "estimated_sampled_wall_seconds": local_weight * interval_ms / 1000,
                "thread_cpu_seconds": local_cpu_us / 1e6,
                "oncpu_sample_ratio": (local_weight - local_offcpu_weight) / local_weight,
                "offcpu_sample_ratio": local_offcpu_weight / local_weight,
                "top_wall_stacks": ranked(local_wall, local_weight, 12, "weighted_samples"),
                "top_cpu_stacks": ranked(local_cpu, local_cpu_us, 12, "thread_cpu_us"),
                "top_offcpu_stacks": ranked(local_offcpu, local_offcpu_weight, 12, "weighted_samples"),
            }

    output = {
        "schema_version": "reth-fpga-samply-analysis/oh-165-v1",
        "run_id": manifest["run_id"],
        "profile_sha256": hashlib.sha256(args.profile.read_bytes()).hexdigest(),
        "window": {
            "start_epoch_ms": window_start_ms,
            "completion_epoch_ms": window_end_ms,
            "seconds": (window_end_ms - window_start_ms) / 1000,
            "samply_interval_ms": interval_ms,
            "sample_rows": sample_rows,
            "weighted_samples": weighted_samples,
            "thread_cpu_seconds": cpu_us_total / 1e6,
            "oncpu_weighted_samples": oncpu_weighted_samples,
            "offcpu_weighted_samples": offcpu_weighted_samples,
            "oncpu_sample_ratio": oncpu_weighted_samples / weighted_samples if weighted_samples else 0.0,
            "offcpu_sample_ratio": offcpu_weighted_samples / weighted_samples if weighted_samples else 0.0,
        },
        "top_threads_by_sampled_wall": ranked(thread_wall, sum(thread_wall.values()), 25, "weighted_samples"),
        "top_threads_by_cpu": ranked(thread_cpu, sum(thread_cpu.values()), 25, "thread_cpu_us"),
        "top_wall_leaves": ranked(wall_leaf, sum(wall_leaf.values()), 30, "weighted_samples"),
        "top_cpu_leaves": ranked(cpu_leaf, sum(cpu_leaf.values()), 30, "thread_cpu_us"),
        "top_offcpu_leaves": ranked(offcpu_leaf, sum(offcpu_leaf.values()), 30, "weighted_samples"),
        "top_wall_stacks": ranked(wall_stacks, sum(wall_stacks.values()), 30, "weighted_samples"),
        "top_cpu_stacks": ranked(cpu_stacks, sum(cpu_stacks.values()), 30, "thread_cpu_us"),
        "top_offcpu_stacks": ranked(offcpu_stacks, sum(offcpu_stacks.values()), 30, "weighted_samples"),
        "threads": thread_details,
    }
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"run_id": manifest["run_id"], "output": str(args.output), "sample_rows": sample_rows}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

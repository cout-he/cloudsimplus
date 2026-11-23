"""Validate the Java exports independently and render workload reports."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter
import numpy as np
import pandas as pd


COLUMNS = [
    "task_id", "task_type", "arrival_time_s", "required_cores", "required_ram_mb",
    "length_mi_per_core", "reference_runtime_s", "deadline_s",
]
VARIANT_COLUMNS = ["main_file", "variant_file", "deadline_alpha", "deadline_base_s"]
COLORS = ["#175d93", "#b45d21", "#137a67", "#7b428c"]
LABELS = {"short": "Short", "medium": "Medium", "long": "Long", "memory": "Memory-intensive"}
HOUR = 3600
EPSILON = 1e-8


@dataclass
class Scenario:
    meta: dict
    tasks: pd.DataFrame
    rates: pd.DataFrame
    only_a: int
    sha: str
    sensitivity: list[dict]

    @property
    def label(self):
        return f"{100 * self.meta['target']:g}%"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_csv(path):
    return pd.read_csv(path, float_precision="round_trip")


def read_variants(directory):
    path = directory / "deadline-variants.csv"
    return read_csv(path) if path.exists() else pd.DataFrame(columns=VARIANT_COLUMNS)


def read_properties(path):
    properties = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "!")):
            key, _, value = line.partition("=")
            properties[key.strip()] = value.strip()
    return properties


def config_values(config, key):
    return [value.strip() for value in config[key].split(",")]


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def summary(values):
    return " / ".join(f"{n:,.1f}" for n in np.quantile(values, [0, 0.5, 0.95, 1]))


def verify_task_types(tasks, config):
    types = config_values(config, "types")
    require(set(tasks.task_type) <= set(types), "Unexpected task type")
    ram_step = int(config["ram_step_mb"])
    for task_type, rows in tasks.groupby("task_type", sort=False):
        cores = {int(token.split(":")[0]) for token in config_values(config, f"{task_type}.cores")}
        require(set(rows.required_cores) <= cores, f"Invalid core count for {task_type}")
        lower, upper = map(int, config_values(config, f"{task_type}.ram_mb_per_core"))
        require((rows.required_ram_mb % (rows.required_cores * ram_step) == 0).all(), f"Invalid RAM grid for {task_type}")
        require(((rows.required_ram_mb >= lower * rows.required_cores)
                 & (rows.required_ram_mb <= upper * rows.required_cores)).all(), f"Invalid conditional RAM for {task_type}")
        minimum, _, maximum = map(int, config_values(config, f"{task_type}.runtime_s"))
        require(rows.reference_runtime_s.between(minimum, maximum).all(), f"Invalid runtime for {task_type}")


def vm_fit(tasks, shapes):
    fastest = np.zeros(len(tasks))
    fits_b = np.zeros(len(tasks), dtype=bool)
    for shape in shapes.itertuples(index=False):
        fits = ((tasks.required_cores <= shape.cores) & (tasks.required_ram_mb <= shape.ram_mb)).to_numpy()
        fastest = np.maximum(fastest, np.where(fits, shape.mips_per_core, 0))
        if shape.mips_per_core == 2000:
            fits_b |= fits
    require(np.all(fastest > 0), "At least one task fits no creatable VM")
    return fastest, int(np.sum(~fits_b))


def deadline_diagnostics(tasks, fastest, alpha, base, day, file):
    arrivals = tasks.arrival_time_s.to_numpy()
    runtimes = tasks.reference_runtime_s.to_numpy()
    deadlines = tasks.deadline_s.to_numpy()
    require(np.allclose(deadlines, arrivals + base + alpha * runtimes, rtol=0, atol=EPSILON)
            and np.all(deadlines > arrivals), f"Incorrect or nonpositive Deadline in {file}")
    earliest = arrivals + tasks.length_mi_per_core.to_numpy() / fastest
    missed = deadlines + EPSILON < earliest
    flags = pd.DataFrame({
        "file": file, "task_id": tasks.loc[missed, "task_id"].to_numpy(),
        "task_type": tasks.loc[missed, "task_type"].to_numpy(), "deadline_s": deadlines[missed],
        "earliest_finish_s": earliest[missed], "deficit_s": (earliest - deadlines)[missed],
    })
    return {
        "alpha": alpha, "base": base, "file": file, "infeasible": int(missed.sum()),
        "after_day": int(np.sum(deadlines > day)),
        "reference_wait": summary(deadlines - arrivals - runtimes),
        "min_slack": float((deadlines - earliest).min()),
    }, flags


def verify_arrivals(tasks, rates, config, meta):
    step, day = int(meta["window_s"]), int(meta["day_s"])
    types = config_values(config, "types")
    windows = pd.MultiIndex.from_product([range(0, day, step), types], names=["window_start_s", "task_type"])
    observed = (tasks.assign(window_start_s=(tasks.arrival_time_s // step).astype(int) * step)
                .groupby(["window_start_s", "task_type"]).size().reindex(windows, fill_value=0))
    recorded = rates.set_index(["window_start_s", "task_type"])
    require(recorded.index.is_unique and len(recorded) == len(windows), f"Incomplete rate table for {meta['file']}")
    recorded = recorded.reindex(windows)
    require(np.isfinite(recorded.rate_per_s).all() and (recorded.rate_per_s >= 0).all()
            and np.array_equal(recorded.arrivals, observed), f"Rate window mismatch for {meta['file']}")
    for task_type in types:
        amplitude, _, _, sigma = map(float, config_values(config, f"{task_type}.arrival"))
        if amplitude > 0 or sigma > 0:
            require(recorded.xs(task_type, level="task_type").rate_per_s.nunique() > 1, f"Flat rates for {task_type}")


def audit(directory, meta, config, shapes, all_rates, variants):
    file = meta["file"]
    tasks = read_csv(directory / file)
    require(list(tasks.columns) == COLUMNS and len(tasks) == meta["tasks"] and len(tasks) > 0,
            f"Invalid task table {file}")
    require(np.array_equal(tasks.task_id, np.arange(len(tasks))) and tasks.arrival_time_s.is_monotonic_increasing,
            f"IDs or chronological order invalid in {file}")
    require(meta["day_s"] == 86_400 and meta["reference_mips"] == 2000 and meta["capacity_mips"] == 6_789_984,
            "Unexpected V1.0 capacity or reference")
    require(((tasks.arrival_time_s >= 0) & (tasks.arrival_time_s < meta["day_s"])).all(), "Arrival out of window")
    require((tasks.length_mi_per_core == 2000 * tasks.reference_runtime_s).all(), "Per-core MI mismatch")
    total = int((tasks.required_cores * tasks.length_mi_per_core).sum())
    offered = total / (meta["capacity_mips"] * meta["day_s"])
    require(total == meta["total_mi"] and abs(offered - meta["offered_load"]) < EPSILON
            and abs(offered - meta["target"]) <= meta["tolerance"], f"Offered load mismatch in {file}")

    verify_task_types(tasks, config)
    rates = all_rates[all_rates.file == file]
    verify_arrivals(tasks, rates, config, meta)
    fastest, only_a = vm_fit(tasks, shapes)
    default, flags = deadline_diagnostics(tasks, fastest, meta["deadline_alpha"],
                                          meta["deadline_base_s"], meta["day_s"], file)
    sensitivity, all_flags = [default], [flags]
    for variant in variants[variants.main_file == file].itertuples(index=False):
        variant_tasks = read_csv(directory / variant.variant_file)
        require(variant_tasks.drop(columns="deadline_s").equals(tasks.drop(columns="deadline_s")),
                f"Non-Deadline columns differ in {variant.variant_file}")
        diagnostics, flags = deadline_diagnostics(variant_tasks, fastest, variant.deadline_alpha,
                                                  variant.deadline_base_s, meta["day_s"], variant.variant_file)
        if variant.variant_file != file:
            sensitivity.append(diagnostics)
            all_flags.append(flags)
    sensitivity.sort(key=lambda item: item["alpha"])
    return Scenario(meta, tasks, rates, only_a, digest(directory / file), sensitivity), all_flags


def check_repetitions(primary, directory, config, shapes):
    if directory is None:
        return []
    manifest = read_csv(directory / "manifest.csv").set_index("file")
    rates = read_csv(directory / "arrival-windows.csv")
    variants = read_variants(directory)
    repetitions = []
    for scenario in primary:
        file = scenario.meta["file"]
        other_file = f"{Path(file).stem}-run-2.csv"
        require(digest(directory / file) == scenario.sha, f"Same-seed export changed for {file}")
        require(manifest.loc[file, "seed"] == scenario.meta["seed"], f"First seed changed for {file}")
        other_meta = {"file": other_file, **manifest.loc[[other_file]].to_dict(orient="records")[0]}
        other, _ = audit(directory, other_meta, config, shapes, rates, variants)
        require(other_meta["seed"] != scenario.meta["seed"] and other.sha != scenario.sha
                and not np.array_equal(scenario.tasks.arrival_time_s, other.tasks.arrival_time_s),
                f"Independent replication repeated {file}")
        repetitions.append((scenario.label, other_meta["seed"], len(other.tasks), other_meta["offered_load"]))
    return repetitions


def write_scenario(output, scenario, types, config):
    meta, tasks = scenario.meta, scenario.tasks
    peak = (tasks.arrival_time_s // HOUR).value_counts().sort_index()
    default = next(item for item in scenario.sensitivity if item["file"] == meta["file"])
    lines = [
        f"# {scenario.label} offered load 验证", "",
        f"- 任务数 **{len(tasks):,}**，累计 **{meta['total_mi']:,} MI**，实际提交强度 **{meta['offered_load']:.4%}**。",
        f"- 样本种子 {meta['seed']}，校准 {meta['attempts']} 次，基准平均到达率 {meta['mean_rate_per_s']:.5f} 任务/秒。",
        f"- 到达最多的小时从第 {int(peak.idxmax()):02d} 时开始，共 {int(peak.max()):,} 个任务。",
        f"- {scenario.only_a:,} 个任务仅由 A 类承载，{default['after_day']:,} 个默认 Deadline 超过 24 小时。",
        f"- 任务文件 SHA-256 为 {scenario.sha}。",
        "", "以下数值为最小值／P50／P95／最大值。各类参数均为人工实验假设。", "",
        "| 类型 | 任务数 | 实际占比 | 设定占比 | RAM（MB） | 参考时长（秒） | 绝对 Deadline（秒） | 参考 CPU 可等待（秒） |",
        "|---|---:|---:|---:|---|---|---|---|",
    ]
    for task_type in types:
        group = tasks[tasks.task_type == task_type]
        if not group.empty:
            wait = group.deadline_s - group.arrival_time_s - group.reference_runtime_s
            lines.append(f"| {task_type} | {len(group):,} | {len(group) / len(tasks):.2%} | "
                         f"{float(config[f'{task_type}.share']):.0%} | {summary(group.required_ram_mb)} | "
                         f"{summary(group.reference_runtime_s)} | {summary(group.deadline_s)} | {summary(wait)} |")
    lines += ["", "## CPU 与 RAM 联合分布", "",
              "| 类型 | 核数 | 数量 | RAM（MB，最小值／P50／P95／最大值） |", "|---|---:|---:|---|"]
    for task_type in types:
        for cores, group in tasks[tasks.task_type == task_type].groupby("required_cores"):
            lines.append(f"| {task_type} | {cores} | {len(group):,} | {summary(group.required_ram_mb)} |")
    lines += ["", "## Deadline 敏感性", "",
              "| α | b（秒） | 先天不可行 | 超过 24 小时 | 参考 CPU 可等待（秒，最小值／P50／P95／最大值） | 最快 VM 最小余量（秒） |",
              "|---:|---:|---:|---:|---|---:|"]
    for item in scenario.sensitivity:
        lines.append(f"| {item['alpha']:g} | {item['base']:g} | {item['infeasible']:,} | {item['after_day']:,} | "
                     f"{item['reference_wait']} | {item['min_slack']:.2f} |")
    variants = [item for item in scenario.sensitivity if item["file"] != meta["file"]]
    if variants:
        lines += ["", "非默认 α 按类型的截止和等待分布，数值依次为最小值／P50／P95／最大值。", "",
                  "| α | 类型 | 绝对 Deadline（秒） | 参考 CPU 可等待（秒） |", "|---:|---|---|---|"]
        for item in variants:
            for task_type in types:
                rows = tasks[tasks.task_type == task_type]
                if not rows.empty:
                    deadline = rows.arrival_time_s + item["base"] + item["alpha"] * rows.reference_runtime_s
                    wait = deadline - rows.arrival_time_s - rows.reference_runtime_s
                    lines.append(f"| {item['alpha']:g} | {task_type} | {summary(deadline)} | {summary(wait)} |")
    lines += ["", "按单任务立即执行并独占所需资源检查先天可行性。整批任务的排队和截止达成率需要单独仿真。"]
    output.joinpath(Path(meta["file"]).with_suffix(".md")).write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_overview(output, scenarios, repeats, flags):
    lines = [
        "# 合成 CPU 工作负载验证报告", "",
        "逐行核对了任务 ID、到达窗口、每核 MI、CPU/RAM 条件关系、VM 承载及 Deadline。"
        "V1.0 主机全天理论容量为 586,654,617,600 MI。", "",
        "| 目标 | 实际提交强度 | 任务数 | 总 MI | 仅 A 类承载 | 默认 Deadline 先天不可行 | 分布报告 |",
        "|---:|---:|---:|---:|---:|---:|---|",
    ]
    for scenario in scenarios:
        meta = scenario.meta
        default = next(item for item in scenario.sensitivity if item["file"] == meta["file"])
        stem = Path(meta["file"]).stem
        lines.append(f"| {scenario.label} | {meta['offered_load']:.4%} | {len(scenario.tasks):,} | {meta['total_mi']:,} | "
                     f"{scenario.only_a:,} | {default['infeasible']:,} | [{stem}]({stem}.md) |")
    lines += ["", "## 复现与敏感性", ""]
    if repeats:
        lines += [
            "同一场景种子的三个主 CSV 在两次独立 Java 进程中逐字节相同。"
            "每档第二次抽样的种子、到达时刻序列和 CSV 摘要均不同。", "",
            "| 目标 | 第二次生成的种子 | 任务数 | 实际提交强度 |", "|---:|---:|---:|---:|",
        ]
        for label, seed, count, load in repeats:
            lines.append(f"| {label} | {seed} | {count:,} | {load:.4%} |")
    count = sum(len(item.sensitivity) for item in scenarios)
    lines += [
        "", f"核验了 {count} 个强度与 α 的组合，敏感性文件除 deadline_s 外的字段逐列一致。"
        f"先天不可行共 {len(flags):,} 条，按任务列在 [deadline-infeasible.csv](deadline-infeasible.csv)。",
        "", "曲线的期望到达数来自校准到达率乘以区间秒数。排队、VM 启动和竞争的影响需要后续回放。",
        "", "[各类型到达曲线](arrivals.png) · [任务分布与 CPU/RAM 联合图](task-distributions.png)",
    ]
    output.joinpath("validation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def plot_arrivals(output, scenarios, types):
    cols = min(2, len(types))
    rows = (len(types) + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(11, 3 * rows + 0.7), squeeze=False, layout="constrained")
    for axis, task_type in zip(axes.flat, types):
        for index, scenario in enumerate(scenarios):
            data = scenario.rates[scenario.rates.task_type == task_type]
            starts = data.window_start_s.to_numpy()
            ends = starts + scenario.meta["window_s"]
            hours = np.arange(24)
            left = np.maximum(starts[:, None], hours * HOUR)
            right = np.minimum(ends[:, None], (hours + 1) * HOUR)
            expected = data.rate_per_s.to_numpy() @ np.maximum(0, right - left)
            arrivals = scenario.tasks.loc[scenario.tasks.task_type == task_type, "arrival_time_s"]
            observed, _ = np.histogram(arrivals, bins=np.arange(25) * HOUR)
            color = COLORS[index % len(COLORS)]
            axis.plot(hours + 0.5, observed, color=color, label=scenario.label, linewidth=1.9)
            axis.plot(hours + 0.5, expected, color=color, linestyle="--", linewidth=1, alpha=0.75)
        axis.set(title=LABELS.get(task_type, task_type), xlabel="Hour since start", ylabel="Tasks / hour",
                 xlim=(0, 24), xticks=range(0, 25, 4))
        axis.grid(axis="y", alpha=0.22)
    for axis in list(axes.flat)[len(types):]:
        axis.set_visible(False)
    axes.flat[0].legend(loc="best", ncols=min(3, len(scenarios)), frameon=False)
    fig.suptitle("24-hour arrivals  |  observed solid, conditional mean dashed", fontsize=15)
    fig.savefig(output / "arrivals.png", dpi=170)
    plt.close(fig)


def ecdf(axis, values, color, label):
    ordered = np.sort(values)
    axis.plot(ordered, np.arange(1, len(ordered) + 1) / len(ordered), color=color, label=label, linewidth=1.65)


def plot_distributions(output, scenarios, types):
    fig, axes = plt.subplots(3, 2, figsize=(11, 10.5), layout="constrained")
    shares, cores, ram, runtime, joint, wait = axes.flat
    x = np.arange(len(types))
    width = 0.82 / len(scenarios)
    all_cores = sorted({int(core) for item in scenarios for core in item.tasks.required_cores.unique()})
    for index, scenario in enumerate(scenarios):
        data = scenario.tasks
        color = COLORS[index % len(COLORS)]
        offset = (index - (len(scenarios) - 1) / 2) * width
        task_mix = data.task_type.value_counts(normalize=True).reindex(types, fill_value=0)
        core_mix = data.required_cores.value_counts(normalize=True).reindex(all_cores, fill_value=0)
        shares.bar(x + offset, task_mix, width=width, color=color, label=scenario.label)
        cores.bar(np.arange(len(all_cores)) + offset, core_mix, width=width, color=color)
        ecdf(ram, data.required_ram_mb, color, scenario.label)
        ecdf(runtime, data.reference_runtime_s, color, scenario.label)

    largest = max(scenarios, key=lambda item: item.meta["target"])
    for index, task_type in enumerate(types):
        data = largest.tasks[largest.tasks.task_type == task_type]
        if data.empty:
            continue
        bounds = data.groupby("required_cores").required_ram_mb.agg(["min", "median", "max"])
        positions = np.array([all_cores.index(core) for core in bounds.index]) + (index - (len(types) - 1) / 2) * 0.13
        color = COLORS[index % len(COLORS)]
        joint.vlines(positions, bounds["min"], bounds["max"], color=color, linewidth=2)
        joint.scatter(positions, bounds["median"], color=color, s=22, label=LABELS.get(task_type, task_type))
    joint.axhline(65_536, color="#666666", linewidth=0.9, linestyle="--")
    joint.annotate("B RAM limit", (0.1, 65_536), xytext=(0, 4), textcoords="offset points", fontsize=8)
    for index, item in enumerate(largest.sensitivity):
        waiting = (item["alpha"] - 1) * largest.tasks.reference_runtime_s + item["base"]
        ecdf(wait, waiting, COLORS[index % len(COLORS)], f"α = {item['alpha']:g}")
    shares.set(title="Task mix", ylabel="Share of jobs", xticks=x, xticklabels=[LABELS.get(t, t) for t in types])
    cores.set(title="CPU request", ylabel="Share of jobs", xlabel="Cores per task", xticks=range(len(all_cores)),
              xticklabels=all_cores)
    ram.set(title="RAM demand", xlabel="MB per task", xscale="log", ylabel="Empirical CDF")
    runtime.set(title="Reference runtime", xlabel="Seconds on 2,000 MIPS/core", xscale="log", ylabel="Empirical CDF")
    joint.set(title=f"CPU × RAM  |  {largest.label} load", xlabel="Cores per task", ylabel="RAM (MB)", yscale="log",
              xticks=range(len(all_cores)), xticklabels=all_cores)
    wait.set(title=f"Reference waiting budget  |  {largest.label} load", xlabel="Seconds", xscale="symlog",
             ylabel="Empirical CDF")
    for axis in axes.flat:
        axis.grid(axis="y", alpha=0.22)
    for axis in [shares, cores, ram, runtime, wait]:
        axis.yaxis.set_major_formatter(PercentFormatter(xmax=1))
    for axis in [shares, joint, wait]:
        axis.legend(loc="best", frameon=False, fontsize=8)
    fig.suptitle("Synthetic CPU workload  |  distributions and Deadline sensitivity", fontsize=15)
    fig.savefig(output / "task-distributions.png", dpi=170)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("generated", type=Path)
    parser.add_argument("report_directory", type=Path)
    parser.add_argument("--replicated", type=Path)
    args = parser.parse_args()

    config = read_properties(args.config)
    directory = args.generated
    manifest = read_csv(directory / "manifest.csv")
    rates = read_csv(directory / "arrival-windows.csv")
    variants = read_variants(directory)
    shapes = read_csv(directory / "vm-shapes.csv")
    require({tuple(shape) for shape in shapes.to_numpy()} == {(24, 98_304, 3091), (16, 65_536, 2000)},
            "Unexpected verified VM envelopes for V1.0")
    scenarios, flags = [], []
    for row in manifest.to_dict(orient="records"):
        scenario, scenario_flags = audit(directory, row, config, shapes, rates, variants)
        scenarios.append(scenario)
        flags.extend(scenario_flags)
    require(len(scenarios) > 0, "Empty scenario manifest")
    repeats = check_repetitions(scenarios, args.replicated, config, shapes)
    all_flags = pd.concat(flags, ignore_index=True)
    args.report_directory.mkdir(parents=True, exist_ok=True)
    all_flags.to_csv(args.report_directory / "deadline-infeasible.csv", index=False, lineterminator="\n")
    types = config_values(config, "types")
    for scenario in scenarios:
        write_scenario(args.report_directory, scenario, types, config)
    write_overview(args.report_directory, scenarios, repeats, all_flags)
    plot_arrivals(args.report_directory, scenarios, types)
    plot_distributions(args.report_directory, scenarios, types)
    print(f"Validated {len(scenarios)} main workloads, {sum(len(s.sensitivity) for s in scenarios)} Deadline combinations, "
          f"and {len(repeats)} independent replications.")


if __name__ == "__main__":
    main()

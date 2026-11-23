"""Audit actual CloudSim replay outputs and plot the shared baseline experiments."""

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


LOADS = (30, 60, 90)
DAY = 86_400
HOUR = 3600
JOULES_PER_KWH = 3_600_000
CPU_CAPACITY = 6_789_984
IDLE_WATTS = 7094.4
TOL = 1e-7
COLORS = ("#1d6491", "#b75d21", "#137965")
TASK_FIELDS = {
    "task_type": "task_type", "arrival_time": "arrival_time_s", "required_cores": "required_cores",
    "required_ram_mb": "required_ram_mb", "length_mi_per_core": "length_mi_per_core", "deadline": "deadline_s",
}


@dataclass
class Run:
    label: int
    summary: pd.Series
    traces: pd.DataFrame
    occupancy: pd.DataFrame
    completed_after_day: int
    a_placements: int
    b_placements: int


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_csv(path):
    return pd.read_csv(path, float_precision="round_trip")


def agree(actual, expected, message, atol=TOL):
    require(np.allclose(actual, expected, atol=atol, rtol=0), message)


def state_changes(rows, key, start, end, cpu, ram):
    """End and start deltas sharing a timestamp are applied atomically."""
    groups = rows[key].to_numpy()
    starts = pd.DataFrame({"entity": groups, "time": rows[start], "cpu": cpu, "ram": ram})
    ends = pd.DataFrame({"entity": groups, "time": rows[end], "cpu": -cpu, "ram": -ram})
    points = pd.concat([starts, ends], ignore_index=True).groupby(["entity", "time"], sort=True).sum()
    return points.groupby(level="entity").cumsum().reset_index()


def assert_bounds(points, inventory, key, cpu_limit, ram_limit, label):
    rows = points.join(inventory.set_index(key)[[cpu_limit, ram_limit]], on="entity")
    require((rows[["cpu", "ram"]] >= 0).all().all(), f"{label} negative resource state")
    require((rows.cpu <= rows[cpu_limit]).all() and (rows.ram <= rows[ram_limit]).all(),
            f"{label} CPU/RAM cap exceeded")
    require((rows.groupby("entity")[["cpu", "ram"]].last() == 0).all().all(), f"{label} not released")


def host_integrals(tasks, vms, hosts, service_end, power_specs):
    tasks = tasks.join(vms.set_index("vm_id")[["mips_per_core"]], on="vm_id", validate="many_to_one")
    busy = state_changes(tasks, "host_id", "start_time", "finish_time",
                         tasks.required_cores * tasks.mips_per_core, tasks.required_ram_mb)
    reserved = state_changes(vms, "host_id", "created", "destroyed", vms.cores, vms.ram_mb)
    assert_bounds(busy, hosts, "host_id", "mips_capacity", "ram_capacity_mb", "Host actual execution")
    assert_bounds(reserved, hosts, "host_id", "host_cores", "ram_capacity_mb", "Host VM reservation")
    operating = pd.DataFrame({
        "host_id": hosts.host_id,
        "cpu_seconds": 0.0, "task_ram_seconds": 0.0, "vm_ram_seconds": 0.0,
        "window_it_kwh": 0.0, "drain_it_kwh": 0.0,
    }).set_index("host_id")
    busy_groups = dict(tuple(busy.groupby("entity")))
    reserved_groups = dict(tuple(reserved.groupby("entity")))
    for host in hosts.itertuples(index=False):
        history = busy_groups.get(host.host_id, busy.iloc[0:0])
        boundaries = np.r_[0.0, history.time, service_end]
        cpu = np.r_[0.0, history.cpu]
        ram = np.r_[0.0, history.ram]
        during = np.diff(np.clip(boundaries, 0, DAY))
        tail = np.diff(np.clip(boundaries, DAY, service_end))
        curve = power_specs[host.host_mips]
        watts = np.interp(cpu / host.mips_capacity, np.linspace(0, 1, len(curve)), curve)
        vm_history = reserved_groups.get(host.host_id, reserved.iloc[0:0])
        vm_elapsed = np.diff(np.clip(np.r_[0.0, vm_history.time, max(service_end, DAY)], 0, DAY))
        operating.loc[host.host_id] = [
            cpu @ during, ram @ during, np.r_[0.0, vm_history.ram] @ vm_elapsed,
            watts @ during / JOULES_PER_KWH, watts @ tail / JOULES_PER_KWH,
        ]
    return operating


def verify_run(directory, input_file, specs, settings, curves, load):
    summary = read_csv(directory / "summary.csv").iloc[0]
    tasks = read_csv(directory / "tasks.csv")
    vms = read_csv(directory / "vms.csv")
    sequence = read_csv(directory / "timeseries.csv")
    original = read_csv(input_file).sort_values("task_id")
    hosts = read_csv(directory / "hosts.csv").sort_values("host_id")
    hosts["host_cores"] = np.where(hosts.host_id < 76, 24, 16)
    hosts["host_mips"] = np.where(hosts.host_id < 76, 3091, 2000)
    label = f"Baseline-{load}"
    require(list(hosts.host_id) == list(range(112))
            and np.array_equal(hosts.mips_capacity, hosts.host_cores * hosts.host_mips)
            and np.array_equal(hosts.ram_capacity_mb, np.where(hosts.host_id < 76, 98_304, 65_536)),
            f"{label} physical inventory differs from V1.0")
    require(summary.input_sha256 == sha256(input_file.read_bytes()).hexdigest(), f"{label} input checksum differs")
    require(tasks.task_id.is_unique and np.array_equal(tasks.task_id, original.task_id), f"{label} task reconciliation failed")
    for output_column, input_column in TASK_FIELDS.items():
        require(np.array_equal(tasks[output_column], original[input_column]), f"{label} changed {input_column}")

    completed = tasks[tasks.status == "SUCCESS"].copy()
    late = completed.finish_time > completed.deadline
    require(tasks.status.notna().all() and len(tasks) == summary.tasks
            and len(completed) == summary.completed and len(tasks) - len(completed) == summary.other,
            f"{label} terminal counts disagree")
    require(int(late.sum()) == summary.late and len(completed) - int(late.sum()) == summary.on_time
            and np.array_equal(completed.deadline_missed, late), f"{label} deadline counts disagree")
    require((completed.start_time >= completed.arrival_time).all(), f"{label} released a task early")
    if "admission_time" in completed:
        require(completed.admission_time.is_monotonic_increasing
                and (completed.admission_time >= completed.arrival_time).all()
                and (completed.start_time >= completed.admission_time).all(), f"{label} FCFS admission violation")
    agree(completed.waiting_time, completed.start_time - completed.arrival_time, f"{label} waiting time")
    agree(completed.turnaround_time, completed.finish_time - completed.arrival_time, f"{label} turnaround time")
    agree(completed.tardiness, np.maximum(0, completed.finish_time - completed.deadline), f"{label} tardiness")

    require(vms.vm_id.is_unique and vms.destroyed.notna().all(), f"{label} duplicate or undestroyed VMs")
    placed = (vms.join(hosts.set_index("host_id")[["host_mips"]], on="host_id", validate="many_to_one")
              .join(specs.set_index("vm_type"), on="vm_type", rsuffix="_expected", validate="many_to_one"))
    require((placed.mips_per_core == placed.host_mips).all() and (placed.cores == placed.cores_expected).all()
            and (placed.ram_mb == placed.ram_mb_expected).all()
            and (placed.loc[placed.vm_type == "VM9", "host_id"] < 76).all(),
            f"{label} VM MIPS, catalogue or VM9 placement mismatch")
    joined = completed.join(vms.set_index("vm_id"), on="vm_id", rsuffix="_vm", validate="many_to_one")
    require((joined.host_id == joined.host_id_vm).all() and (joined.start_time >= joined.ready - TOL).all()
            and (joined.finish_time <= joined.shutdown_started + TOL).all(), f"{label} invalid VM use")
    agree(joined.actual_execution_time, joined.length_mi_per_core / joined.mips_per_core,
          f"{label} native execution duration", atol=0.030)
    reduce = completed.groupby("vm_id").agg(last_finish=("finish_time", "max"), first_arrival=("arrival_time", "min"),
                                            tasks=("task_id", "size")).reindex(vms.vm_id)
    agree(vms.task_count, reduce.tasks, f"{label} VM task totals")
    require(np.all(vms.created.to_numpy() >= reduce.first_arrival.to_numpy() - TOL),
            f"{label} a VM was allocated before its first task arrived")
    agree(vms.shutdown_started, reduce.last_finish.to_numpy() + settings["idle_timeout_s"], f"{label} idle lifetime")
    agree(vms.destroyed, vms.shutdown_started + settings["shutdown_s"], f"{label} physical release")
    concurrent = state_changes(completed, "vm_id", "start_time", "finish_time",
                               completed.required_cores, completed.required_ram_mb)
    assert_bounds(concurrent, vms, "vm_id", "cores", "ram_mb", f"{label} task reservations")

    work = int((original.required_cores * original.length_mi_per_core).sum())
    require(work == summary.total_mi and summary.vms_created == len(vms), f"{label} task or VM totals")
    agree(summary.offered_load, work / (CPU_CAPACITY * DAY), f"{label} offered load")
    require(sequence.time.is_monotonic_increasing and sequence.time.is_unique
            and sequence.time.iloc[0] == 0 and DAY in sequence.time.to_numpy()
            and sequence.arrivals.sum() == len(tasks), f"{label} invalid measurement windows")
    require((sequence.time.diff().dropna() <= settings["sample_s"] + TOL).all()
            and ((sequence[["cpu_utilization", "ram_utilization", "vm_ram_utilization"]] >= -TOL)
                 & (sequence[["cpu_utilization", "ram_utilization", "vm_ram_utilization"]] <= 1 + TOL)).all().all(),
            f"{label} sample cadence or utilization range is invalid")
    agree(sequence.it_watts.iloc[0], IDLE_WATTS, f"{label} idle watts")
    agree(sequence.time.iloc[-1], summary.service_end_s, f"{label} tail boundary")
    require(summary.service_end_s >= max(DAY, completed.finish_time.max() - TOL)
            and summary.simulation_end_s >= vms.destroyed.max() - TOL, f"{label} cleanup truncated")

    totals = host_integrals(completed, vms, hosts, summary.service_end_s, curves)
    for field in ("window_it_kwh", "drain_it_kwh"):
        agree(hosts[field], totals[field], f"{label} per-Host energy")
        agree(summary[field], totals[field].sum(), f"{label} total energy")
    agree(summary.window_cpu_utilization, totals.cpu_seconds.sum() / (CPU_CAPACITY * DAY), f"{label} mean CPU")
    agree(summary.window_ram_utilization, totals.task_ram_seconds.sum() / (hosts.ram_capacity_mb.sum() * DAY),
          f"{label} mean task RAM")
    agree(summary.window_vm_ram_utilization, totals.vm_ram_seconds.sum() / (hosts.ram_capacity_mb.sum() * DAY),
          f"{label} mean VM RAM")

    occupancy = (vms.groupby("vm_type").size().reindex(specs.vm_type, fill_value=0).rename("instances").to_frame())
    print(f"{label}: reconciled {len(tasks):,} tasks, {len(vms):,} VMs, independently integrated Host energy.")
    return Run(load, summary, sequence, occupancy, int((completed.finish_time > DAY).sum()),
               int((vms.host_id < 76).sum()), int((vms.host_id >= 76).sum()))


def finish_time_axis(axis):
    axis.axvline(24, color="#777777", linestyle="--", linewidth=0.9)
    axis.grid(axis="y", alpha=0.25)
    axis.set_xlabel("Hours since simulation start")


def plot_counts(runs, output):
    fig, axes = plt.subplots(3, 2, figsize=(11, 8), layout="constrained")
    for row, run in enumerate(runs):
        x = run.traces.time / HOUR
        left, right = axes[row]
        left.plot(x, run.traces.arrivals, color=COLORS[row], linewidth=1.05)
        right.plot(x, run.traces.running_tasks, color="#1d6491", label="Running")
        right.plot(x, run.traces.queued_tasks, color="#b75d21", label="Waiting")
        left.set(title=f"Baseline-{run.label} · arrivals", ylabel="Tasks per sample")
        right.set(title=f"Baseline-{run.label} · execution and queue", ylabel="Tasks")
        finish_time_axis(left)
        finish_time_axis(right)
    axes[0, 1].legend(frameon=False)
    fig.savefig(output / "workload-counts.png", dpi=160)
    plt.close(fig)


def plot_resources(runs, output):
    fig, axes = plt.subplots(3, 1, figsize=(10, 8), layout="constrained")
    for axis, run in zip(axes, runs):
        x = run.traces.time / HOUR
        for color, field, label in zip(COLORS, ("cpu_utilization", "ram_utilization", "vm_ram_utilization"),
                                       ("Running CPU", "Task RAM", "VM RAM allocated on Hosts")):
            axis.plot(x, run.traces[field], color=color, label=label)
        axis.set(title=f"Baseline-{run.label}", ylabel="Share of capacity", ylim=(0, 1.02))
        axis.yaxis.set_major_formatter(PercentFormatter(xmax=1))
        finish_time_axis(axis)
    axes[0].legend(frameon=False, ncols=3)
    fig.savefig(output / "resource-utilization.png", dpi=160)
    plt.close(fig)


def plot_one(runs, output, field, title, ylabel, filename, transform=lambda x: x):
    fig, axis = plt.subplots(figsize=(9.5, 4.4), layout="constrained")
    for index, run in enumerate(runs):
        axis.plot(run.traces.time / HOUR, transform(run.traces[field]), color=COLORS[index],
                  label=f"Baseline-{run.label}", linewidth=1.4)
    if field == "it_watts":
        axis.axhline(IDLE_WATTS / 1000, color="#666666", linestyle=":", label="All Hosts idle")
    axis.set(title=title, ylabel=ylabel)
    finish_time_axis(axis)
    axis.legend(frameon=False, ncols=2)
    fig.savefig(output / filename, dpi=160)
    plt.close(fig)


def plot_comparison(runs, output):
    fig, axes = plt.subplots(1, 3, figsize=(11.6, 3.8), layout="constrained")
    labels = [str(run.label) + "%" for run in runs]
    miss = [run.summary.deadline_miss_rate for run in runs]
    waits = [run.summary.p95_wait_s / 60 for run in runs]
    day = [run.summary.window_it_kwh for run in runs]
    drain = [run.summary.drain_it_kwh for run in runs]
    axes[0].bar(labels, miss, color=COLORS)
    axes[0].set(title="Deadline miss rate", ylabel="Share of completed tasks")
    axes[0].yaxis.set_major_formatter(PercentFormatter(xmax=1))
    axes[1].bar(labels, waits, color=COLORS)
    axes[1].set(title="P95 wait", ylabel="Minutes")
    axes[2].bar(labels, day, label="Submission window", color="#1d6491")
    axes[2].bar(labels, drain, bottom=day, label="Drain phase", color="#b75d21")
    axes[2].set(title="IT energy", ylabel="kWh")
    axes[2].legend(frameon=False, fontsize=8)
    for axis in axes:
        axis.grid(axis="y", alpha=0.25)
        axis.set_xlabel("Target offered load")
    fig.savefig(output / "baseline-comparison.png", dpi=160)
    plt.close(fig)


def write_report(runs, specs, output):
    lines = [
        "# V1.2 多规格 VM 基准回放验收", "",
        "以下结果来自 CloudSim Plus 完整执行。逐条核对原始输入与任务终态、CPU/RAM 并发峰值、VM 生命周期、"
        "每核 MIPS 和截止时间，并用原 SPECpower 采样独立重算每台 Host 的两段能耗。", "",
        "| 场景 | Offered load | 输入 | 完成 | 按时 | 超期 | 其他 | P95 等待（秒） | 截止违约率 | 24 小时 IT（kWh） | 清空 IT（kWh） |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in runs:
        s = item.summary
        lines.append(f"| Baseline-{item.label} | {s.offered_load:.4%} | {s.tasks:,} | {s.completed:,} | {s.on_time:,} | "
                     f"{s.late:,} | {s.other:,} | {s.p95_wait_s:,.2f} | {s.deadline_miss_rate:.2%} | "
                     f"{s.window_it_kwh:.4f} | {s.drain_it_kwh:.4f} |")
    lines += ["", "## 等待、执行及结束时刻", "",
              "| 场景 | 等待均值（秒） | 执行均值（秒） | 周转均值 / P95（秒） | 超期时长均值 / P95（秒） | 24 小时后完成 | 最后任务终态（秒） | 仿真结束（秒） |",
              "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for item in runs:
        s = item.summary
        lines.append(f"| Baseline-{item.label} | {s.mean_wait_s:,.2f} | {s.mean_execution_s:,.2f} | "
                     f"{s.mean_turnaround_s:,.2f} / {s.p95_turnaround_s:,.2f} | "
                     f"{s.mean_tardiness_s:,.2f} / {s.p95_tardiness_s:,.2f} | "
                     f"{item.completed_after_day:,} | {s.last_terminal_s:,.2f} | {s.simulation_end_s:,.2f} |")
    lines += ["", "## VM 与资源利用率", "",
              "| 场景 | VM 累计创建 | A / B 类放置 | VM 同时存在峰值 | 复用任务 | VM 创建失败 | 队列峰值 | CPU 均值 | 任务 RAM 均值 | VM RAM 分配均值 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for item in runs:
        s = item.summary
        lines.append(f"| Baseline-{item.label} | {s.vms_created:,} | {item.a_placements:,} / {item.b_placements:,} | "
                     f"{s.peak_vms:,} | {s.reused_tasks:,} | "
                     f"{s.vm_creation_failures:,} | {s.peak_queued:,} | {s.window_cpu_utilization:.2%} | "
                     f"{s.window_ram_utilization:.2%} | {s.window_vm_ram_utilization:.2%} |")
    lines += ["", "| 规格 | 核 | RAM（MB） | Baseline-30 | Baseline-60 | Baseline-90 |",
              "|---|---:|---:|---:|---:|---:|"]
    for spec in specs.itertuples(index=False):
        counts = " | ".join(f"{item.occupancy.loc[spec.vm_type, 'instances']:,}" for item in runs)
        lines.append(f"| {spec.vm_type} | {spec.cores} | {spec.ram_mb:,} | {counts} |")
    high = max(runs, key=lambda item: item.label)
    peak_sample = high.traces.loc[high.traces.queued_tasks.idxmax()]
    lines += ["", f"{high.label}% 档在第 {peak_sample.time / HOUR:.2f} 小时的采样积压最大，"
              f"当时有 {int(peak_sample.queued_tasks):,} 项在等，CPU 使用率为 {peak_sample.cpu_utilization:.2%}，"
              f"VM RAM 分配率为 {peak_sample.vm_ram_utilization:.2%}。全局平均剩余容量可以与排队同时出现，"
              "因为准入需要在同一个 VM 或 Host 上满足队首任务所需的核数和内存。"]
    lines += [
        "", "截止违约率以成功完成任务为分母。非成功终态单独计数。等待从到达到开始，执行为实际运行，"
        "周转从到达到完成，超期时长统计超期完成的任务。窗口利用率以 86,400 秒为分母。",
        "", "时间序列虚线为 24 小时边界。最后任务终态之后的 VM 回收计入模拟器结束时间。"
        "能耗在提交窗口和任务清空阶段各自积分。",
        "", "[到达、运行与等待](workload-counts.png) · [资源曲线](resource-utilization.png) · "
        "[VM 数量](vm-count.png) · [IT 功率](it-power.png) · [三档对比](baseline-comparison.png)",
    ]
    (output / "validation.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--experiment", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    experiment = args.experiment
    specs = read_csv(experiment / "vm-specs.csv")
    settings = {}
    for line in (experiment / "baseline.properties").read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.endswith("_s"):
            settings[key] = float(value)
    folder = Path("src/test/resources/experiments/carbon/power-specs")
    curves = {3091: np.loadtxt(folder / "host_A_24core_96GB.txt"),
              2000: np.loadtxt(folder / "host_B_16core_64GB.txt")}
    runs = [verify_run(args.results / f"baseline-{load}",
                       experiment.parent / "synthetic-v1" / "results" / f"workload-{load}.csv",
                       specs, settings, curves, load)
            for load in LOADS]
    args.output.mkdir(parents=True, exist_ok=True)
    write_report(runs, specs, args.output)
    plot_counts(runs, args.output)
    plot_resources(runs, args.output)
    plot_one(runs, args.output, "active_vms", "Active VM instances", "VMs", "vm-count.png")
    plot_one(runs, args.output, "it_watts", "Server IT power", "kW", "it-power.png", lambda values: values / 1000)
    plot_comparison(runs, args.output)


if __name__ == "__main__":
    main()

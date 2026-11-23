"""Render a capacity-sizing note from measured workload statistics."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import gzip
from pathlib import Path
import math

BASE = Path(__file__).resolve().parent
SCENARIOS = (0.45, 0.65, 0.85)
ORIGIN_US = 600_000_000


def read_rows(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def number(value):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def fmt(value, decimals=3):
    return f"{value:,.{decimals}f}" if value is not None else "—"


def positive(value):
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("expected a finite positive number")
    return parsed


def machine_profiles():
    objects = read_rows(BASE / "download_manifest.csv")
    found = [row for row in objects if row["table"] == "machine_events"
             and row["status"].startswith("verified_")]
    if len(found) != 1:
        return 0, Counter()
    path = Path(found[0]["local_path"])
    path = path if path.is_absolute() else BASE / path
    active = {}
    with gzip.open(path, "rt", newline="", encoding="utf-8") as stream:
        for row in csv.reader(stream):
            time, machine, kind, _, cpu, mem = row
            if int(time) > ORIGIN_US:
                break
            if kind == "1":
                active.pop(machine, None)
            elif kind == "0" or (kind == "2" and machine in active):
                previous = active.get(machine, (None, None))
                active[machine] = (
                    previous[0] if not cpu else number(cpu),
                    previous[1] if not mem else number(mem),
                )
    counts = Counter(pair for pair in active.values() if None not in pair and min(pair) > 0)
    return len(active), counts


def statistic(rows, section, cohort, metric, stat):
    for row in rows:
        if (row["section"], row["cohort"], row["metric"], row["statistic"]) == (section, cohort, metric, stat):
            return number(row["value"])
    return None


def load_metrics(rows):
    metrics = ("cpu_requested_avg", "mem_requested_avg", "cpu_actual_avg", "mem_actual_avg", "running_tasks_peak")
    values = {}
    for cohort in ("observed_tasks", "batch_proxy"):
        values[cohort] = {stat: [statistic(rows, "timeseries_300s", cohort, metric, stat) for metric in metrics]
                          for stat in ("p95", "p99", "max")}
    return values


def render(rows, scale, cpu_unit, mem_unit, stats_folder):
    measured = load_metrics(rows)
    documented_raw = read_rows(BASE / "download_manifest.csv")
    catalog_size = sum(int(row["expected_bytes"]) for row in documented_raw)
    verified = [row for row in documented_raw if row["status"].startswith("verified_")]
    local_size = sum(int(row["local_bytes"]) for row in verified)
    covered = len(verified) == len(documented_raw) and bool(documented_raw)
    report = [
        "# Google Cluster Trace 2011 数据中心容量与 VM 规格建议",
        "",
        f"存储桶已验证 {len(verified):,}/{len(documented_raw):,} 个对象，{local_size / 10**9:.3f}/{catalog_size / 10**9:.3f} GB。"
        "对象覆盖与统计窗口各自有明确口径，容量计算使用下方注明的统计产物。"
        f"当前存储桶状态为{'完整' if covered else '部分'}。"
        "历史容量、归一化需求和研究设定分开列示。"
        f" [统计报告](workload_analysis.md) · [指标输入]({(stats_folder / 'workload_statistics.csv').as_posix()})",
        "",
        "## 观测到的机器资源形态",
        "",
    ]
    active, profiles = machine_profiles()
    if active:
        report += [
            f"对独立校验的 machine_events 文件，在有效时钟起点 t=600 秒回放 ADD、REMOVE、UPDATE 后，"
            f"同时在册 {active:,} 台机器，其中 {sum(profiles.values()):,} 台同时给出正的 CPU 和内存容量。"
            "以下按同一 CPU／内存容量对合并，不解析平台哈希为具体产品。",
            "",
            "| 归一化 CPU | 归一化内存 | 机器数 | 占双维已知的比例 |",
            "|---:|---:|---:|---:|",
        ]
        for (cpu, mem), count in profiles.most_common(10):
            report.append(f"| {cpu:.6g} | {mem:.6g} | {count:,} | {count/sum(profiles.values()):.2%} |")
    else:
        report.append("机器事件尚无可校验的本地统计输入。")
    report += [
        "", "## 容量基准和场景定义", "",
        "令 D_C、D_M 分别是所选轨迹在 5 分钟窗口中的 CPU 和内存总请求量。"
        "对每个维度取 P95 作为主体规模参考，P99 和峰值用于压力校核。历史请求曲线来自历史实际运行区间，"
        "实际使用曲线来自采样；两者都与提交即运行、重新调度的负载曲线具有不同含义。",
        "",
        "| 样本口径 | 分位 | CPU 请求 | 内存请求 | CPU 实际使用 | 内存实际使用 | 窗口峰值并发 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    names = {"observed_tasks": "历史观测", "batch_proxy": "严格批任务代理"}
    for cohort, group in measured.items():
        for stat, values in group.items():
            report.append(f"| {names[cohort]} | {stat.upper()} | " + " | ".join(map(fmt, values)) + " |")

    paired = {cohort: [
        statistic(rows, "joint_timeseries_300s", cohort, metric, "p95")
        for metric in ("cpu_requested_avg", "mem_requested_avg")
    ] for cohort in names}
    basis = paired if any(all(n is not None and n > 0 for n in p) for p in paired.values()) else {
        cohort: measured[cohort]["p95"][:2] for cohort in names
    }
    cohort = next((c for c in ("batch_proxy", "observed_tasks")
                   if all(x is not None and x > 0 for x in basis[c])), None)
    event_hours = statistic(rows, "coverage", "task_events", "window_duration", "value")
    use_hours = statistic(rows, "coverage", "task_usage", "window_duration", "value")
    if event_hours is not None and use_hours is not None:
        report += ["", f"本次容量建议基于任务事件 {event_hours / 3600:.2f} 小时与用量 {use_hours / 3600:.2f} 小时的"
                   "实测窗口。未达到全轨迹时，窗口内分位数用于设置预实验参数，后续应重估代表性。"]
    if cohort:
        demands = basis[cohort]
        if basis is paired:
            report += ["", "场景容量使用请求和用量共同覆盖的 5 分钟窗取 P95。"]
        benchmark = profiles.most_common(1)[0][0] if profiles else None
        report += [
            "",
            f"下表暂用**{names[cohort]}**的 P95 请求总量，工作负载倍率 α={scale:g}。"
            "每个目标负载率 ρ 定义为参考需求除以容量，CPU 和内存各自按 C=α·P95(D)/ρ 计算。"
            "这些是容量场景的设计比例。仿真 CPU 利用率需按实际运行时的有效使用量另行测量。",
            "",
            "| 目标负载率 | 归一化 CPU 容量 | 归一化内存容量 | 参考机器等效数下界 |",
            "|---:|---:|---:|---:|",
        ]
        for rho in SCENARIOS:
            cpu, mem = (scale * x / rho for x in demands)
            count = max(math.ceil(cpu / benchmark[0]), math.ceil(mem / benchmark[1])) if benchmark else None
            report.append(f"| {rho:.0%} | {fmt(cpu)} | {fmt(mem)} | {count if count is not None else '—'} |")
        if benchmark:
            report.append(
                f"\n参考机器是 t=600 秒样本中最常见的容量对（{benchmark[0]:g}, {benchmark[1]:g}）。"
                "机器等效数是两个维度的总量下界，实际装箱还要满足同一台 Host 可容纳完整 VM。"
            )
        if benchmark:
            cpu_fraction = scale * demands[0] / (256 * benchmark[0])
            mem_fraction = scale * demands[1] / (256 * benchmark[1])
            report += ["", f"将 256 台同规格参考机器作为容量情景，当前口径的 P95 负载占其"
                       f"归一化 CPU 容量 {cpu_fraction:.2%}、内存容量 {mem_fraction:.2%}。"
                       "该比值用于判断应验证怎样的负载规模。新 Host 规格确定后可代入实配容量重新计算。"]
        if cpu_unit is not None:
            report += [
                "",
                f"按命令行提供的物理校准 1 CPU 单位={cpu_unit:g} 核、1 内存单位={mem_unit:g} GiB，表中的容量对应",
                "",
                "| 目标负载率 | 理想物理 CPU 核数下界 | 理想物理内存 GiB 下界 |",
                "|---:|---:|---:|",
            ]
            for rho in SCENARIOS:
                cpu = math.ceil(scale * demands[0] / rho * cpu_unit)
                mem = math.ceil(scale * demands[1] / rho * mem_unit)
                report.append(f"| {rho:.0%} | {cpu:,} | {mem:,} |")
    else:
        report += [
            "",
            "目前暂无两个维度均为正的可用 P95 请求口径。先对每个 5 分钟窗口求运行任务的资源请求总和，"
            "得到成对记录后，再按 45%、65%、85% 的目标比例核算规模。",
        ]
    report += [
        "",
        "设 CPU 校准系数 K_C 为每个归一化 CPU 单位对应的研究参考核数，K_M 为每个归一化内存单位对应的 GiB。"
        "绝对容量下界为 ceil(C_C·K_C) 核和 ceil(C_M·K_M) GiB。两个缩放常数独立，"
        "Google 轨迹公开值不足以反推出本研究现代服务器的核心、MIPS、物理内存或功率。"
        "配置 Host 时还要单独提供可用 CPU 份额、可分配内存、基准算力和空闲／满载功率。",
        "",
        "### 异构服务器与缩放", "",
        "分别比较同规模的单规格、CPU 偏重加内存偏重两规格，以及第三种高能效通用机型。"
        "用相同 Cloudlet 需求和时间窗对各组合做二维 VM 装箱，比较放置成功率、"
        "资源碎片、留出容量及功率曲线。如果第三种规格没有提高可行性或降低实测能耗，优先保留两种规格。"
        "P95 或 P99 的分维下界应再用同一时段的成对 CPU／内存需求和峰值验证。",
        "",
        "将原轨迹用于单数据中心场景时，优先抽取连续的一天或多天稳定窗口，然后对 Job ID 做固定种子、"
        "分层的整组抽样，保留相对到达时间、任务请求和持续时间。按日、时段、任务类别、CPU／内存联合档位"
        "复验抽样后分布与峰均比。减少 Job 数量后重新统计负载，不把任务核心和内存按比例直接压小，"
        "也不把主机规模按全轨迹记录数线性外推。",
        "",
        "## VM 规格与按需回收", "",
        "对严格批任务代理的双维请求先拟合三档候选包络。每档从 CPU 和内存的边际分位值取得初始容量，"
        "再用联合分布核验命中率，任何任务都必须在同一规格内同时满足两个维度。",
        "",

    ]
    vm_cohort = "batch_proxy"
    concurrency = {
        stat: statistic(rows, "joint_timeseries_300s", vm_cohort, "running_tasks_peak", stat)
        for stat in ("p95", "p99", "max")
    }
    if all(value is not None for value in concurrency.values()):
        report += [
            f"严格批任务代理在共同观测窗口中的 5 分钟峰值并发为 "
            f"P95 {math.ceil(concurrency['p95'])}、P99 {math.ceil(concurrency['p99'])}、"
            f"最大 {math.ceil(concurrency['max'])}。"
            "若以一台 VM 同时运行一个任务为对照，这也是对应的活跃 VM 需求，"
            "启动中与延迟回收的 VM 另计。生产配置还应叠加规格装箱、回收滞后和仿真基准的压力校验。"
            f"同一口径下，观测峰值活跃数为 1,536 台候选池的 {concurrency['max'] / 1536:.2%}。",
            "",
        ]
    report += [
        "| 候选包络 | CPU 请求上界（归一化） | 内存请求上界（归一化） |",
        "|---|---:|---:|",
    ]
    joint_path = BASE / stats_folder / "plot_data" / "joint_requests.csv"
    cells = []
    for cell in read_rows(joint_path):
        if cell["cohort"] != vm_cohort:
            continue
        values = tuple(number(cell.get(field)) for field in
                       ("cpu_left", "cpu_right", "mem_left", "mem_right", "count"))
        if None not in values:
            cells.append(values)
    envelope_coverage = []
    exact_coverage = []
    for rank in ("p50", "p90", "p99"):
        metric = f"both_le_marginal_{rank}"
        cpu = statistic(rows, "joint_envelope", vm_cohort, metric, "cpu_limit")
        mem = statistic(rows, "joint_envelope", vm_cohort, metric, "mem_limit")
        hit = statistic(rows, "joint_envelope", vm_cohort, metric, "share")
        if cpu is None or mem is None:
            cpu = statistic(rows, "requests", vm_cohort, "cpu_request", rank)
            mem = statistic(rows, "requests", vm_cohort, "mem_request", rank)
        report.append(f"| {rank.upper()} 初始包络 | {fmt(cpu, 6)} | {fmt(mem, 6)} |")
        if hit is not None:
            exact_coverage.append(f"{rank.upper()} 档覆盖 {hit:.1%}")
            continue
        if cpu is None or mem is None or not cells:
            continue
        count = sum(cell[4] for cell in cells)
        lower = sum(n for _, ch, _, mh, n in cells if ch <= cpu and mh <= mem)
        upper = sum(n for cl, _, ml, _, n in cells if cl <= cpu and ml <= mem)
        if count > 0:
            envelope_coverage.append(f"{rank.upper()} 档覆盖 {lower / count:.1%}–{upper / count:.1%}")
    if exact_coverage:
        report += ["", "逐任务双维核验显示，" + "，".join(exact_coverage) + "。"]
    asym_count = statistic(rows, "joint_envelope", vm_cohort, "cpu_le_p50_mem_ge_p90", "count")
    asym_share = statistic(rows, "joint_envelope", vm_cohort, "cpu_le_p50_mem_ge_p90", "share")
    if asym_count is not None and asym_share is not None:
        report += ["", f"CPU 不高于中位数且内存不低于 P90 的任务有 {asym_count:,.0f} 个"
                   f"（占双维请求均可用的严格批任务代理 {asym_share:.2%}）。"]
    if envelope_coverage:
        report += ["", "根据联合二维分箱，上界包含所有可能相交的箱，下界只计完全纳入的箱。"
                   + "，".join(envelope_coverage) + "。规格确定时要用逐任务双维精确复核。"]

    report += [
        "",
        "用校准系数转换时，将 CPU 向上取整为支持的 vCPU 档位，内存向上取整到可采购或仿真的规格。"
        "用每个任务的双维请求选择可行 VM 中预计新增容量最小的规格。CPU 小且内存大的任务可分到内存偏重规格；"
        "是否单独开一档由覆盖率、装箱损失和能耗结果决定。超大任务可进入显式的大规格或不可行队列，"
        "保留分类和计数，禁止静默截断需求。",
        "",
        "VM 池只统计已创建／启动中／占用／等待回收的实例。按到达的 Cloudlet 先查找"
        "满足资源和截止时间的现有 VM，其次决定是否启动新 VM。策略应包含启动耗时、回收阈值与最小驻留时间。"
        "允许独立任务共用 VM 时，同步检查并发所需核心和内存之和，队列需按期限或策略运行。"
        "VM 数量由实际峰值并发、分规格装箱、启动等待和 SLA 决定，需用回放测量。",
        "",
        "外部校准仍需现代服务器规格与实测功率、每核性能基准、VM 启动及回收时延、"
        "内存保留口径、目标 SLA 及碳强度时序。"
        "校准后再评估任何固定服务器或 VM 数量。",
    ]
    return "\n".join(report) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stats", type=Path, default=BASE / "workload_statistics.csv")
    parser.add_argument("--output", type=Path, default=BASE / "datacenter_sizing_recommendations.md")
    parser.add_argument("--scale", type=positive, default=1)
    parser.add_argument("--cpu-per-unit", type=positive)
    parser.add_argument("--gib-per-unit", type=positive)
    args = parser.parse_args()
    if (args.cpu_per_unit is None) != (args.gib_per_unit is None):
        parser.error("provide both physical calibration factors or neither")
    try:
        relative = args.stats.resolve().parent.relative_to(args.output.resolve().parent)
    except ValueError:
        relative = args.stats.resolve().parent
    rows = read_rows(args.stats)
    args.output.write_text(
        render(rows, args.scale, args.cpu_per_unit, args.gib_per_unit, relative),
        encoding="utf-8",
    )
    print(args.output)


if __name__ == "__main__":
    main()

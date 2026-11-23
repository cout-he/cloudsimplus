"""将已完成的 Google Cluster Trace 2011 统计写入中文报告。"""

import argparse
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent


OBSERVED = "observed_tasks"
BATCH = "batch_proxy"
IMMEDIATE = "submit_immediate_proxy"
COHORT_NAMES = {OBSERVED: "观测任务", BATCH: "批处理代理", IMMEDIATE: "代理任务提交即运行"}
EDT = timezone(timedelta(hours=-4))
TRACE_ANCHOR = datetime(2011, 5, 1, 19, tzinfo=EDT)


def rows(path):
    with path.open(newline="", encoding="utf-8-sig") as source:
        return list(csv.DictReader(source))


def show(value, style="number"):
    if value is None:
        return "—"
    if style == "count":
        return f"{int(value):,}"
    if style == "rate":
        return f"{value:,.4f}"
    if style == "percent":
        return f"{value:.3%}"
    if style == "bytes":
        return f"{value / 1e9:,.3f} GB" if value >= 1e6 else f"{value / 1e3:,.1f} kB"
    return f"{value:,.5g}" if abs(value) < 1 else f"{value:,.3f}".rstrip("0").rstrip(".")


def table(header, data):
    lines = [header, ["---"] * len(header), *data]
    return "\n".join("| " + " | ".join(str(cell) for cell in line) + " |" for line in lines)


def local_time(trace_seconds):
    return (TRACE_ANCHOR + timedelta(seconds=trace_seconds - 600)).strftime("%m-%d %H:%M:%S")


class WorkloadReport:
    def __init__(self, summary_dir):
        self.summary_dir = summary_dir
        self.records = rows(summary_dir / "workload_statistics.csv")
        self.metrics = {
            (row["section"], row["cohort"], row["metric"], row["statistic"]): row for row in self.records
        }
        self.manifest = rows(summary_dir / "download_manifest.csv")

    def get(self, section, cohort, metric, stat="count", field="value"):
        value = self.metrics.get((section, cohort, metric, stat), {}).get(field)
        return float(value) if value else None

    def metric_text(self, section, cohort, metric, stat="count", style="number", field="value"):
        return show(self.get(section, cohort, metric, stat, field), style)

    def distribution_cells(
        self,
        section,
        cohort,
        metric,
        quantiles=("mean", "p50", "p75", "p90", "p95", "p99", "max"),
        style="number",
    ):
        return [
            self.metric_text(section, cohort, metric, "mean", "count", "n"),
            *[self.metric_text(section, cohort, metric, measure, style) for measure in quantiles],
        ]

    def coverage_block(self):
        validated = [x for x in self.manifest if x["status"].startswith("verified_")]
        total = sum(int(x["expected_bytes"]) for x in self.manifest)
        checked = sum(int(x["local_bytes"]) for x in validated)
        byte_coverage = show(checked / total if total else None, "percent")
        result = [
            f'本地清单列出 **{len(self.manifest):,} 个官方对象（{show(total, "bytes")}）**，其中 **{len(validated):,} 个已校验（{show(checked, "bytes")}，{checked / 2**30:,.3f} GiB）**。'
            f"已校验压缩字节占清单的 {byte_coverage}。"
            + (
                "清单所列对象均已获得完整校验。"
                if len(validated) == len(self.manifest)
                else f"尚有 {len(self.manifest)-len(validated):,} 个对象未通过本地完整性核验或尚未下载。具体状态及失败原因见清单。"
            )
            + " [下载清单](download_manifest.csv)",
            "统计输入固定为分析启动时通过 SHA-256 校验的连续分片前缀。从第 0 分片开始，到首个缺片处停止；后续补齐的对象在重跑时进入统计。",
        ]
        data = []
        incomplete = []
        for label in (
            "task_events",
            "task_usage",
            "job_events",
            "machine_events",
            "machine_attributes",
            "task_constraints",
            "metadata",
        ):
            expected = [x for x in self.manifest if x["table"] == label]
            good = [x for x in validated if x["table"] == label]
            analyzed = self.get("coverage", label, "analyzed_prefix_shards")
            if label != "metadata" and analyzed != len(expected):
                incomplete.append(label)
            data.append(
                [
                    f"`{label}`",
                    len(expected),
                    len(good),
                    show(sum(int(x["local_bytes"]) for x in good), "bytes"),
                    show(analyzed, "count"),
                ]
            )
        result.append(table(["表", "官方对象", "本地已校验", "已校验大小", "本次参与统计分片"], data))
        windows = []
        for label, display in [
            ("task_events", "任务事件"),
            ("task_usage", "用量观测"),
            ("joint", "两者交集"),
        ]:
            start = self.get("coverage", label, "window_start", "value")
            end = self.get("coverage", label, "window_end_exclusive", "value")
            hours = (end - start) / 3600 if start is not None and end is not None else None
            windows.append(
                [
                    display,
                    show(start),
                    show(end),
                    show(hours),
                    (
                        f"{local_time(start)} 至 {local_time(end)}"
                        if start is not None and end is not None
                        else "—"
                    ),
                ]
            )
        result.append(
            table(
                ["统计窗口", "开始时间戳（秒）", "截止时间戳（秒，开区间）", "覆盖小时", "2011 年 EDT"],
                windows,
            )
        )
        if incomplete:
            result.append(
                "**以下统计以已处理分片及上表时间窗为界。** 部分覆盖的表为 "
                + "、".join(f"`{name}`" for name in incomplete)
                + "。部分事件窗中的单任务作业判定可能随以后新增任务而变化。短时窗口只能描述该时段，无法支撑全周期外推。"
            )
        result.append(
            "下载覆盖率按压缩字节计，分析覆盖按分片和时间计。报告中的 `—` 表示该统计无可用数据。逐任务分布采用 DuckDB t-digest 近似分位数，结果可能随扫描顺序略有变化。时间窗指标和 VM 双维覆盖使用精确分位数或逐条计数。源数据见 [主要统计](workload_statistics.csv)及 [分窗明细](plot_data/timeseries.csv)。"
        )
        return "\n\n".join(result)

    def lifecycle_block(self):
        counts = [
            (("inventory", OBSERVED, name), label)
            for name, label in [
                ("tasks", "唯一任务"),
                ("jobs_in_task_events", "任务事件中的唯一 Job"),
                ("complete_lifecycle_tasks", "观测窗内完整生命周期任务"),
                ("complete_successful_tasks", "完整且成功的任务"),
                ("tasks_with_resubmission", "有重复提交的任务"),
                ("tasks_left_censored", "窗前已提交任务"),
                ("tasks_without_submit", "无提交事件的任务"),
                ("tasks_right_censored", "末状态仍待调度或运行的任务"),
                ("tasks_with_inferred_event", "含推断事件的任务"),
            ]
        ]
        counts += [
            (("inventory", "job_events", "jobs_in_job_events"), "作业事件中的唯一 Job"),
            (("eligibility", BATCH, "prescreen_candidates"), "事件及放置约束预筛通过"),
            (("eligibility", BATCH, "candidates_within_usage_window"), "完整落入用量观测窗的预筛任务"),
            (("eligibility", BATCH, "eligible_tasks"), "可用于独立 CPU 批处理研究的代理任务"),
        ]
        blocks = [
            "重建按任务键、时间戳、分片号和原始行序排序。一次完整尝试需要观测窗内的有效提交、调度与可判定终止，且运行区间为正；推断或 `LOST` 终止不计为完整。运行时长使用最后一次完整成功尝试的 `FINISH−SCHEDULE`。请求使用该次开始时的值，其他任务回退至首个可得请求。全局任务总数包括期初状态。",
            table(
                ["指标", "数量"], [(label, self.metric_text(*key, style="count")) for key, label in counts]
            ),
            table(
                ["退出原因", "事件行", "曾发生的任务", "最终状态为该原因的任务"],
                [
                    [
                        label,
                        *[
                            self.metric_text("outcomes", OBSERVED, metric, stat, "count")
                            for stat in ("event_rows", "tasks_ever", "tasks_last_state")
                        ],
                    ]
                    for label, metric in [
                        ("成功", "finish"),
                        ("失败", "fail"),
                        ("取消／其他不确定原因", "kill"),
                        ("驱逐", "evict"),
                        ("丢失终止", "lost"),
                    ]
                ],
            ),
        ]
        quality = [
            (
                label,
                self.metric_text("quality", OBSERVED, metric, "share", "percent"),
                self.metric_text("quality", OBSERVED, metric, "share", "count", "n"),
            )
            for label, metric in [
                ("CPU 请求缺失", "missing_cpu_request"),
                ("内存请求缺失", "missing_mem_request"),
                ("调度类别缺失", "missing_scheduling_class"),
                ("优先级缺失", "missing_priority"),
                ("带补录标记的事件行", "inferred_event_records"),
            ]
        ]
        usage_count = self.get("inventory", "task_usage", "usage_records")
        for label, metric in [
            ("测量区间不可用", "unusable_intervals"),
            ("记录持续时间超过 300 秒", "intervals_longer_than_300s"),
            ("平均 CPU 缺失", "missing_cpu_mean_records"),
            ("Canonical 内存缺失", "missing_mem_mean_records"),
            ("最大 CPU 缺失", "missing_cpu_max_records"),
            ("最大内存缺失", "missing_mem_max_records"),
        ]:
            value = self.get("inventory", "task_usage", metric)
            quality.append(
                (
                    label,
                    show(value / usage_count if value is not None and usage_count else None, "percent"),
                    show(usage_count, "count"),
                )
            )
        blocks.append(table(["质量指标", "比例", "分母（任务或记录）"], quality))
        blocks.append(
            "清洗按官方 `schema.csv` 读取，标识和时间保留整数，空字段保留缺失，剔除非有限或负的资源值，合法的零用量保留。总体使用分布按观测到的任务分别计算时长加权用量，重复尝试可贡献用量；批处理代理用同机器且与成功区间重叠的用量记录， CPU 与内存分别去重覆盖时间，覆盖时长均至少为运行时间的 90%，且 CPU 累积工作量为正。预筛要求观测中只有一个任务的作业、单次完整成功、调度类别 0–1、运行期固定的正 CPU／内存请求、无推断／重试／失效记录、无观测到的放置限制或异机约束。"
        )
        for field, title in [("scheduling_class", "调度类别"), ("priority", "优先级")]:
            keys = [field + "_" + str(i) for i in range(4 if field == "scheduling_class" else 13)] + [
                field + "_missing"
            ]
            data = []
            for key in keys:
                if self.get("mix", OBSERVED, key) is None and self.get("mix", BATCH, key) is None:
                    continue
                row = ["缺失" if key.endswith("missing") else key.rsplit("_", 1)[1]]
                for cohort in (OBSERVED, BATCH):
                    count = self.get("mix", cohort, key)
                    denominator = (
                        self.get("inventory", OBSERVED, "tasks")
                        if cohort == OBSERVED
                        else self.get("eligibility", BATCH, "eligible_tasks")
                    )
                    row += [
                        show(count if count is not None else (0 if denominator else None), "count"),
                        show((count or 0) / denominator if denominator else None, "percent"),
                    ]
                data.append(row)
            if data:
                blocks.append(table([title, "观测任务数", "观测占比", "代理任务数", "代理占比"], data))
        return "\n\n".join(blocks)

    def resources_block(self):
        data = []
        for cohort in (OBSERVED, BATCH):
            for name, section, key in [
                ("运行时间（秒）", "durations", "duration_s"),
                ("CPU 请求", "requests", "cpu_request"),
                ("任务平均 CPU 使用", "usage", "cpu_usage_mean"),
                ("任务峰值 CPU 使用", "usage", "cpu_usage_max"),
                ("内存请求", "requests", "mem_request"),
                ("任务平均 canonical 内存", "usage", "mem_usage_mean"),
                ("任务峰值 canonical 内存", "usage", "mem_usage_max"),
            ]:
                data.append([COHORT_NAMES[cohort], name, *self.distribution_cells(section, cohort, key)])
        blocks = [
            "资源项为归一化数值，运行时长以秒计。任务级实际用量取其有效测量窗口的时长加权均值，任务级峰值取所见窗口的最大值。总体请求和总体用量分布的分母可不同，需求差值使用任务配对。",
            table(["样本", "指标", "n", "均值", "P50", "P75", "P90", "P95", "P99", "最大"], data),
            table(
                ["样本", "短于 60 秒", "至少一小时"],
                [
                    [
                        COHORT_NAMES[c],
                        self.metric_text("durations", c, "duration_lt_60s", "share", "percent"),
                        self.metric_text("durations", c, "duration_ge_3600s", "share", "percent"),
                    ]
                    for c in (OBSERVED, BATCH)
                ],
            ),
        ]
        pairs = []
        for c in (OBSERVED, BATCH):
            for resource, name in [("cpu", "CPU"), ("mem", "内存")]:
                for suffix, desc in [
                    ("actual_minus_request", "用量 − 请求"),
                    ("actual_over_request", "用量／请求（正请求）"),
                ]:
                    pairs.append(
                        [
                            COHORT_NAMES[c],
                            name,
                            desc,
                            *self.distribution_cells(
                                "paired", c, resource + "_" + suffix, ("mean", "p50", "p90", "p95", "p99")
                            ),
                        ]
                    )
        blocks.append(table(["样本", "资源", "配对指标", "n", "均值", "P50", "P90", "P95", "P99"], pairs))
        blocks.append(
            table(
                [
                    "样本",
                    "CPU／内存请求 Pearson r",
                    "CPU／内存用量 Pearson r",
                    "CPU 请求 ≤ 0.02 且内存请求 ≥ 0.1",
                ],
                [
                    [
                        COHORT_NAMES[c],
                        self.metric_text("joint", c, "cpu_mem_request", "pearson_r"),
                        self.metric_text("joint", c, "cpu_mem_actual", "pearson_r"),
                        self.metric_text("joint", c, "cpu_le_0.02_mem_ge_0.1", "share", "percent"),
                    ]
                    for c in (OBSERVED, BATCH)
                ],
            )
        )
        blocks.append(
            table(
                [
                    "调度类别",
                    "CPU 请求 n",
                    "CPU 请求 P50",
                    "CPU 请求 P95",
                    "CPU 用量 n",
                    "平均 CPU 用量 P50",
                    "平均 CPU 用量 P95",
                ],
                [
                    [
                        str(c),
                        self.metric_text(
                            "class_resources", f"scheduling_class_{c}", "cpu_request", "mean", "count", "n"
                        ),
                        self.metric_text("class_resources", f"scheduling_class_{c}", "cpu_request", "p50"),
                        self.metric_text("class_resources", f"scheduling_class_{c}", "cpu_request", "p95"),
                        self.metric_text(
                            "class_resources",
                            f"scheduling_class_{c}",
                            "cpu_usage_mean",
                            "mean",
                            "count",
                            "n",
                        ),
                        self.metric_text(
                            "class_resources", f"scheduling_class_{c}", "cpu_usage_mean", "p50"
                        ),
                        self.metric_text(
                            "class_resources", f"scheduling_class_{c}", "cpu_usage_mean", "p95"
                        ),
                    ]
                    for c in range(4)
                ],
            )
        )
        if self.get("requests", OBSERVED, "cpu_request", "p50") is not None:
            blocks.append(
                f'观测任务 CPU 请求 P50 为 {self.metric_text("requests",OBSERVED,"cpu_request","p50")}，P99 为 {self.metric_text("requests",OBSERVED,"cpu_request","p99")}。这两个分位数分别说明典型请求和尾部容量压力。较大的归一化请求可用于挑选大 VM 候选，任务的实际并行线程数仍需其他资料确认。低 CPU／高内存任务占比以指定阈值计算，VM 规格需要按两维联合覆盖核验。'
            )
        temporal = []
        for c in (OBSERVED, BATCH):
            for label, metric in [
                ("CPU 峰值／平均用量", "cpu_peak_over_mean"),
                ("内存峰值／平均用量", "mem_peak_over_mean"),
                ("每任务用量记录数", "usage_samples_per_task"),
            ]:
                if self.get("temporal_usage", c, metric, "mean", "n"):
                    temporal.append(
                        [
                            COHORT_NAMES[c],
                            label,
                            *self.distribution_cells(
                                "temporal_usage", c, metric, ("p50", "p90", "p95", "p99")
                            ),
                        ]
                    )
        if temporal:
            blocks.append(
                "峰值／均值按同一任务配对。这个比值描述任务从有记录窗口观察到的波动，单窗口任务无法提供跨窗口变化证据。"
            )
            blocks.append(table(["样本", "时间波动指标", "n", "P50", "P90", "P95", "P99"], temporal))
        cutoff = self.get("usage", OBSERVED, "cpu_usage_max", "max")
        if cutoff is not None and cutoff > 1:
            blocks.append(
                f"观测任务的 CPU 窗口峰值最大为 {show(cutoff)}，超过最大单机归一化容量 1。该字段可能包含子容器最大值的加总，极端记录应按聚合标志和原始区间复核。容量推导直接使用请求分位数及区间平均用量，峰值尾部在原统计和分布图中保留。"
            )
        return "\n\n".join(blocks)

    def timeline_block(self):
        arrival = []
        for c in (OBSERVED, BATCH):
            for window in (60, 300, 900, 3600):
                s = f"timeseries_{window}s"
                arrival.append(
                    [
                        COHORT_NAMES[c],
                        str(window // 60),
                        self.metric_text(s, c, "new_tasks", "mean"),
                        *self.distribution_cells(
                            s, c, "arrival_rate_per_s", ("mean", "p95", "p99", "max"), "rate"
                        ),
                        self.metric_text(s, c, "zero_arrival_bins", "share", "percent"),
                    ]
                )
        blocks = [
            table(
                [
                    "样本",
                    "每窗分钟",
                    "每窗平均新任务数",
                    "窗数",
                    "平均率",
                    "P95",
                    "P99",
                    "峰值率",
                    "零到达窗比例",
                ],
                arrival,
            )
        ]
        load = []
        names = [
            ("cpu_requested_avg", "CPU 请求"),
            ("cpu_actual_avg", "CPU 实际用量"),
            ("mem_requested_avg", "内存请求"),
            ("mem_actual_avg", "内存实际用量"),
            ("running_tasks_avg", "并发均值"),
            ("running_tasks_peak", "窗内峰值并发"),
        ]
        for c in (OBSERVED, BATCH, IMMEDIATE):
            for metric, name in names:
                if c == IMMEDIATE and "actual" in metric:
                    continue
                load.append(
                    [
                        COHORT_NAMES[c],
                        name,
                        *self.distribution_cells(
                            "timeseries_300s", c, metric, ("mean", "p95", "p99", "max")
                        ),
                    ]
                )
        blocks.append(table(["口径", "5 分钟窗指标", "有效窗", "均值", "P95", "P99", "最大"], load))
        parts = ["daytime_07_19_EDT", "nighttime_19_07_EDT"]
        day_night = []
        for metric, name in [("arrival_rate_per_s", "到达率（个／秒）"), *names[:5]]:
            row = [name]
            for part in parts:
                row.extend(
                    [
                        self.metric_text(part, OBSERVED, metric, "mean"),
                        self.metric_text(part, OBSERVED, metric, "mean", "count", "n"),
                    ]
                )
            day_night.append(row)
        blocks.append(
            table(
                ["观测任务指标", "07:00–19:00 EDT 平均", "窗数", "19:00–07:00 EDT 平均", "窗数"], day_night
            )
        )
        hours = self.get("coverage", "task_usage", "window_duration", "value")
        if hours is not None and hours < 24 * 3600:
            blocks.append(
                "用量观测窗不足一个完整本地日。上表的日夜差异只能描述实际覆盖时段，无法判断日周期。"
            )
        else:
            blocks.append(
                "按小时的均值及样本数见 [当地小时统计](plot_data/diurnal.csv)。日夜差异为单个 cell 的描述统计，应结合分日曲线核验重复性。"
            )
            hour_path = self.summary_dir / "plot_data" / "diurnal.csv"
            if hour_path.is_file():
                hourly = [x for x in rows(hour_path) if x["cohort"] == OBSERVED]
                extrema = []
                for metric, label, count_key in [
                    ("arrival_rate_per_s", "到达率", "event_5min_bins"),
                    ("cpu_actual_avg", "CPU 用量", "usage_5min_bins"),
                    ("mem_actual_avg", "内存用量", "usage_5min_bins"),
                ]:
                    hours_with_data = [
                        (
                            float(h[metric]),
                            int(h.get("local_hour_ed_t") or h.get("local_hour_edt")),
                            int(h[count_key]),
                        )
                        for h in hourly
                        if h.get(metric) and int(h[count_key]) >= 24
                    ]
                    if not hours_with_data:
                        continue
                    lo, hi = min(hours_with_data), max(hours_with_data)
                    extrema.append(
                        [
                            label,
                            f"{lo[1]:02d}:00",
                            show(lo[0]),
                            lo[2],
                            f"{hi[1]:02d}:00",
                            show(hi[0]),
                            hi[2],
                        ]
                    )
                if extrema:
                    blocks.append("在至少 24 个有效 5 分钟窗的当地小时中，以下时段的跨日均值最低和最高。")
                    blocks.append(
                        table(
                            [
                                "观测指标",
                                "低值小时（EDT）",
                                "均值",
                                "窗数",
                                "高值小时（EDT）",
                                "均值",
                                "窗数",
                            ],
                            extrema,
                        )
                    )
        blocks.append(
            "运行请求在事件改变处更新，并按窗内运行时间加权；缺少请求的任务不贡献该维占用。观测总用量将每条窗口均值乘以与统计窗的重叠秒数后累加，再除以窗宽。并发峰值取窗内事件扫线的最大运行数。请求和实际用量均用归一化资源单位表示。提交即运行序列只重排历史成功时长及请求，估计的是资源压力，未加入仿真中的排队及容量限制。"
        )
        duration = self.get("timeseries_60s", OBSERVED, "arrival_rate_per_s", "mean")
        peak = self.get("timeseries_60s", OBSERVED, "arrival_rate_per_s", "max")
        if duration and peak is not None:
            blocks.append(
                f"观测任务的 1 分钟峰值到达率约为该粒度平均值的 {peak / duration:,.2f} 倍。这个比值用于标定短时突发，不宜用 1 小时平均替代。"
            )
        return "\n\n".join(blocks)

    def machines_block(self):
        labels = [
            ("machines", "机器数"),
            ("machines_at_trace_start", "期初机器数"),
            ("add_events", "ADD 记录"),
            ("remove_events", "REMOVE 记录"),
            ("update_events", "UPDATE 记录"),
            ("platforms", "平台 ID 数"),
            ("capacity_pairs", "观测 CPU／内存容量组合数"),
        ]
        data = [
            [label, self.metric_text("machines", "machine_events", metric, style="count")]
            for metric, label in labels
        ]
        labels = [
            ("attribute_records", "属性记录数"),
            ("machines_with_attributes", "有属性的机器数"),
            ("attribute_names", "属性名数"),
            ("attribute_deletions", "属性删除记录数"),
        ]
        data.extend(
            [label, self.metric_text("machines", "machine_attributes", metric, style="count")]
            for metric, label in labels
        )
        blocks = [table(["机器指标", "数量"], data)]
        blocks.append(
            table(
                ["每台机器首个已知容量", "n", "均值", "P50", "P75", "P90", "P95", "P99", "最大"],
                [
                    [name, *self.distribution_cells("machines", "machine_events", key)]
                    for name, key in [("CPU（归一化）", "cpu_capacity"), ("内存（归一化）", "mem_capacity")]
                ],
            )
        )
        assortment = [x for x in self.records if x["section"] == "machine_capacity_mix"]
        if assortment:
            top = []
            for row in sorted(assortment, key=lambda x: int(x["value"]), reverse=True)[:6]:
                _, cpu, _, mem = row["metric"].split("_")
                top.append([cpu, mem, show(float(row["value"]), "count")])
            blocks.append(
                "首次已知容量最常出现的组合以机器数排序。完整组合与匿名平台频数见 [主要统计](workload_statistics.csv)。\n\n"
                + table(["CPU 容量", "内存容量", "机器数"], top)
            )
        return "\n\n".join(blocks)

    def render(self, template):
        sections = {
            "coverage": self.coverage_block,
            "lifecycle methodology and counts": self.lifecycle_block,
            "resource metrics and interpretation": self.resources_block,
            "arrival, load and diurnal metrics": self.timeline_block,
            "machine metrics and interpretation": self.machines_block,
        }
        for section, build in sections.items():
            marker = f"<!-- {section} -->"
            if template.count(marker) != 1:
                raise ValueError(f"报告模板需要且仅允许一个占位符 {marker}")
            template = template.replace(marker, build())
        return template


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=HERE, help="统计 CSV 与下载清单所在目录")
    parser.add_argument("--template", type=Path, default=HERE / "workload_report_template.md")
    parser.add_argument("--output", type=Path, default=HERE / "workload_analysis.md")
    args = parser.parse_args()
    report = WorkloadReport(args.data_dir).render(args.template.read_text(encoding="utf-8-sig"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    pending = args.output.with_suffix(args.output.suffix + ".pending")
    pending.write_text(report, encoding="utf-8", newline="\n")
    pending.replace(args.output)


if __name__ == "__main__":
    main()

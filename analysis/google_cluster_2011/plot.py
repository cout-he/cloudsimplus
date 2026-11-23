"""绘制 Google Cluster Trace 2011 的聚合统计图。"""

import argparse
import csv
from pathlib import Path

import matplotlib as mpl

mpl.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager
from matplotlib.colors import LogNorm
from matplotlib.ticker import MaxNLocator, PercentFormatter


HERE = Path(__file__).resolve().parent
COHORTS = {'observed_tasks': '观测任务', 'batch_proxy': '批处理代理样本'}
BLUE = '#21678a'
ORANGE = '#a64c23'
GREEN = '#337359'


def configure_style():
    fonts = (
        'Microsoft YaHei', 'Noto Sans CJK SC', 'Source Han Sans SC',
        'PingFang SC', 'WenQuanYi Zen Hei', 'SimHei', 'SimSun',
    )
    installed = {font.name for font in font_manager.fontManager.ttflist}
    selected = next((name for name in fonts if name in installed), None)
    if selected is None:
        raise SystemExit('缺少中文字体，请安装 Noto Sans CJK SC 或其他受支持的中文字体。')
    mpl.rcParams.update({
        'font.family': 'sans-serif',
        'font.sans-serif': [selected, 'DejaVu Sans'],
        'font.size': 9,
        'axes.labelsize': 9,
        'axes.titlesize': 11,
        'axes.titleweight': 'bold',
        'axes.spines.right': False,
        'axes.spines.top': False,
        'axes.linewidth': 0.7,
        'axes.unicode_minus': False,
        'grid.linewidth': 0.45,
        'legend.frameon': False,
        'legend.fontsize': 8,
        'pdf.fonttype': 42,
        'svg.fonttype': 'none',
        'savefig.facecolor': 'white',
    })


def read_rows(path, cohort):
    with path.open(newline='', encoding='utf-8-sig') as stream:
        return [row for row in csv.DictReader(stream) if row['cohort'] == cohort]


def canvas(title, cohort):
    fig, ax = plt.subplots(figsize=(7.1, 3.6), layout='constrained')
    ax.set_title(f'{title} · {COHORTS[cohort]}', loc='left', pad=12)
    ax.tick_params(length=3, pad=4, color='#53646d')
    ax.set_axisbelow(True)
    ax.grid(axis='y', alpha=0.35, color='#9dabb3')
    return fig, ax


def outside_legend(ax):
    ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.2), ncols=3, borderaxespad=0)


def no_data(ax):
    ax.text(0.5, 0.5, '当前统计输入没有可绘制的记录', ha='center', va='center',
            transform=ax.transAxes, color='#52626d')


def time_series(rows, seconds, title, ylabel, series, cohort, integers=False):
    fig, ax = canvas(title, cohort)
    active = [row for row in rows if int(row['bin_seconds']) == seconds
              and any(row.get(field) not in (None, '') for field, *_ in series)]
    if not active:
        ax.set_xlabel('跟踪时间（秒）')
        ax.set_ylabel(ylabel)
        no_data(ax)
        return fig

    by_start = {int(float(row['bin_start_s'])): row for row in active}
    first, last = min(by_start), max(by_start)
    starts = np.arange(first, last + seconds, seconds, dtype=np.int64)
    duration = last + seconds - first
    scale, unit = (86400, '天') if duration >= 2 * 86400 else (3600, '小时')
    if duration < 7200:
        scale, unit = 60, '分钟'
    edges = np.append(starts, last + seconds) / scale

    for field, label, color, line in series:
        values = np.array([float(by_start.get(start, {}).get(field) or 'nan') for start in starts])
        if np.isfinite(values).any():
            ax.stairs(values, edges, baseline=None, color=color, linestyle=line,
                      linewidth=1.0, label=label)

    ax.set_xlim(edges[0], edges[-1])
    ax.set_ylim(bottom=0)
    ax.set_xlabel(f'跟踪时间（{unit}，相对时间戳零点）')
    ax.set_ylabel(ylabel)
    ax.xaxis.set_major_locator(MaxNLocator(nbins=8))
    ax.yaxis.set_major_locator(MaxNLocator(nbins=6, integer=integers))
    if len(ax.get_legend_handles_labels()[0]) > 1:
        outside_legend(ax)
    return fig


def distributions(rows, title, xlabel, series, near_zero, cohort):
    fig, ax = canvas(title, cohort)
    for metric, label, color, line in series:
        bins = sorted((float(row['bin_left']), float(row['bin_right']), int(row['count']))
                      for row in rows if row['metric'] == metric)
        counts = np.array([count for _, _, count in bins], dtype=np.int64)
        total = counts.sum()
        if total > 0:
            edges = [bins[0][0], *(right for _, right, _ in bins)]
            fraction = np.concatenate(([0], np.cumsum(counts) / total))
            ax.step(edges, fraction, where='post', linewidth=1.35, linestyle=line,
                    color=color, label=f'{label}（n={total:,}）')

    ax.set_xlabel(xlabel)
    ax.set_ylabel('累计比例')
    ax.set_xscale('symlog', linthresh=near_zero)
    ax.set_xlim(left=0)
    ax.set_ylim(0, 1.025)
    ax.set_yticks([0, 0.25, 0.5, 0.75, 1])
    ax.yaxis.set_major_formatter(PercentFormatter(1))
    handles, _ = ax.get_legend_handles_labels()
    if handles:
        outside_legend(ax)
    else:
        no_data(ax)
    return fig


def joint_requests(rows, cohort):
    fig, ax = canvas('CPU 与内存请求联合分布', cohort)
    ax.set_xlabel('CPU 请求（归一化）')
    ax.set_ylabel('内存请求（归一化）')
    ax.grid(False)
    if not rows or not any(int(row['count']) for row in rows):
        no_data(ax)
        return fig

    cpu_edges = sorted({float(row[field]) for row in rows for field in ('cpu_left', 'cpu_right')})
    mem_edges = sorted({float(row[field]) for row in rows for field in ('mem_left', 'mem_right')})
    cpu_index = {value: index for index, value in enumerate(cpu_edges)}
    mem_index = {value: index for index, value in enumerate(mem_edges)}
    counts = np.zeros((len(mem_edges) - 1, len(cpu_edges) - 1), dtype=np.int64)
    for row in rows:
        counts[mem_index[float(row['mem_left'])], cpu_index[float(row['cpu_left'])]] = int(row['count'])

    mesh = ax.pcolormesh(cpu_edges, mem_edges, np.ma.masked_equal(counts, 0),
                         norm=LogNorm(vmin=1, vmax=max(2, counts.max())), cmap='viridis')
    ax.set_xscale('symlog', linthresh=1e-5)
    ax.set_yscale('symlog', linthresh=1e-5)
    fig.colorbar(mesh, ax=ax, label='任务数（每格）', pad=0.025, fraction=0.04)
    return fig


def save_figure(fig, directory, name, publication):
    fig.savefig(directory / f'{name}.png', dpi=300)
    fig.savefig(directory / f'{name}.pdf')
    if publication:
        fig.savefig(directory / f'{name}.svg')
        fig.savefig(directory / f'{name}.tiff', dpi=600)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=HERE / 'plot_data')
    parser.add_argument('--output-dir', type=Path, default=HERE / 'figures')
    parser.add_argument('--cohort', choices=COHORTS, default='observed_tasks')
    parser.add_argument('--publication-export', action='store_true', help='另存 SVG 和 600 dpi TIFF')
    args = parser.parse_args()
    configure_style()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    timeseries = read_rows(args.data_dir / 'timeseries.csv', args.cohort)
    histogram = read_rows(args.data_dir / 'distributions.csv', args.cohort)
    joint = read_rows(args.data_dir / 'joint_requests.csv', args.cohort)

    time_charts = (
        ('arrival_rate', 300, '任务到达率（5 分钟）', '新任务数／秒', False,
         [('arrival_rate_per_s', '首次提交', BLUE, '-')]),
        ('datacenter_cpu', 300, '历史 CPU 负载（5 分钟）', '总 CPU（归一化单位）', False,
         [('cpu_requested_avg', '运行任务请求', BLUE, '-'),
          ('cpu_actual_avg', '观测用量', ORANGE, '--')]),
        ('datacenter_memory', 300, '历史内存负载（5 分钟）', '总内存（归一化单位）', False,
         [('mem_requested_avg', '运行任务请求', BLUE, '-'),
          ('mem_actual_avg', '观测用量', ORANGE, '--')]),
        ('concurrent_tasks', 300, '历史运行任务并发（5 分钟）', '在运行任务数', True,
         [('running_tasks_avg', '窗内均值', BLUE, '-'),
          ('running_tasks_peak', '窗内峰值', ORANGE, '--')]),
    )
    for name, seconds, title, ylabel, integers, series in time_charts:
        save_figure(time_series(timeseries, seconds, title, ylabel, series, args.cohort, integers),
                    args.output_dir, name, args.publication_export)

    distribution_charts = (
        ('duration', '任务执行时间分布', '运行时间（秒）', 1,
         [('duration_s', '运行时间', BLUE, '-')]),
        ('cpu_demand', 'CPU 请求与实际使用分布', 'CPU（归一化）', 1e-5,
         [('cpu_request', '请求', BLUE, '-'), ('cpu_usage_mean', '平均使用', ORANGE, '--'),
          ('cpu_usage_max', '峰值使用', GREEN, ':')]),
        ('memory_demand', '内存请求与实际使用分布', '内存（归一化）', 1e-5,
         [('mem_request', '请求', BLUE, '-'), ('mem_usage_mean', '平均使用', ORANGE, '--'),
          ('mem_usage_max', '峰值使用', GREEN, ':')]),
    )
    for name, title, xlabel, near_zero, series in distribution_charts:
        save_figure(distributions(histogram, title, xlabel, series, near_zero, args.cohort),
                    args.output_dir, name, args.publication_export)
    save_figure(joint_requests(joint, args.cohort), args.output_dir, 'cpu_memory_joint',
                args.publication_export)


if __name__ == '__main__':
    main()

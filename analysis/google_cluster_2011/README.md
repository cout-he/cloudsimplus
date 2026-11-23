# Google Cluster Trace 2011 v2.1 分析与复现

本目录用于单数据中心、独立 CPU 批任务的负载研究。当前轨迹是 Google 公开的 `clusterdata-2011-2`，以独立脚本读取，CloudSim Plus Java 框架使用仓库原版代码。

## 数据来源与目录

- [Google 开放数据及许可说明](https://github.com/google/cluster-data/blob/master/ClusterData2011_2.md)
- [官方 v2.1 字段文档](https://drive.google.com/file/d/0B5g07T_gRDg9Z0lsSTEtTWtpOW8/view)
- 官方对象清单由 `https://storage.googleapis.com/storage/v1/b/clusterdata-2011-2/o` 分页获取。对象数、大小、generation、GCS MD5、CRC32C 和 SHA256 校验记录见 [download_manifest.csv](download_manifest.csv)。
- `data/raw/` 保留 GZIP 原件及官方 README、schema.csv 和校验清单。下载中的对象后缀为 `.part`，校验通过才改名。 `data/bucket_inventory.json` 是本次对象清单快照。
- `data/`、`work/` 和本地依赖目录均由本目录的 `.gitignore` 排除。原始轨迹独立于 Git 版本管理。下载全桶需至少 44.13 GB 原件空间，分析另需本地数据库及磁盘溢写空间。
- 存储桶包括用户指定的五表、`task_constraints` 和元数据。全量下载以清单中全部对象均通过校验为完成标准，统计覆盖另以实际分析的表和窗口为准。

## Windows 复跑

从仓库根目录执行。本次使用 Python 3.13.5、DuckDB 1.5.6、Requests 2.32.3、Matplotlib 3.10.0、NumPy 2.4.0 和 Pandas 2.3.3。

```powershell
python -m venv analysis/google_cluster_2011/.venv
$py = "analysis/google_cluster_2011/.venv/Scripts/python.exe"
& $py -m pip install -r analysis/google_cluster_2011/requirements.txt

# 检查首批分片后再下载全桶。重新执行可从已成功的对象及 .part 继续。
& $py analysis/google_cluster_2011/download.py --sample-shards 1
& $py analysis/google_cluster_2011/download.py --workers 12

# 数据统计、绘图及容量换算
& $py analysis/google_cluster_2011/analyze.py --threads 2 --memory-limit 4GB --partitions 32
& $py analysis/google_cluster_2011/plot.py
& $py analysis/google_cluster_2011/render_report.py
& $py analysis/google_cluster_2011/sizing.py
```

`download.py` 优先调用已安装的 `gcloud storage cp`，其余环境走同一官方对象的 HTTPS 端点。已有部分文件继续走 HTTPS Range，服务器返回完整响应时从头写入该部分文件。正式文件以官方 SHA256 和 GCS MD5（两项存在时均核对）验证。官方元数据若未被 SHA256SUM 列出，以对象的 GCS MD5 核对。无 `gcloud` 的主机可用 `--transport https` 显式指定。网络较慢时可调低并发，控制磁盘写入和重试开销。

分析前可用 `python analysis/google_cluster_2011/analyze.py --help` 核对日期窗口、输出目录、线程和内存选项。中间结果默认保存在 `data/work/`。全量模式按 Job 对生命周期分区，按任务标识对用量聚合分区，并按天分割事件时间轴。原始分片投影、生命周期和逐任务用量按输入校验、窗口和计算定义复用磁盘缓存，时间轴使用临时磁盘分区。对部分分片运行时保留输出中的窗口和覆盖说明，切换全量输入后重新汇总。图表默认读取 `plot_data/`，输出到 `figures/`。

`sizing.py` 默认输出归一化容量。目标硬件有测量依据后，可传入 `--cpu-per-unit`、`--gib-per-unit` 和明确的目标抽样倍率 `--scale`，再核对物理容量下界。比较不同资源组合时要保留请求分布与同窗并发，最后用拆分任务与 VM 规格做二维装箱验证。

## 交付物

- [工作负载报告](workload_analysis.md)
- [工作负载统计表](workload_statistics.csv)
- [Cloudlet 转换方案](cloudlet_conversion_plan.md)
- [容量建议](datacenter_sizing_recommendations.md)
- [CloudSim Plus 扩展清单](cloudsim_modification_plan.md)

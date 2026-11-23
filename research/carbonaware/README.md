# 单数据中心物理与工作负载模型 V1.0

场景入口为 `CarbonHostFactory.createDatacenter(simulation)`，实现位于 Maven 测试源码内。主机参数和两条功率曲线从实验资源加载。每类主机共享只读采样点，每次插值用两个相邻点，计算时间为 O(1)。

| 类型 | 台数 | 核数/台 | MIPS/核 | RAM/台 | 网卡/台 | SSD/台 | 空闲/满载 W |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A · Supermicro AS-1015SV-WTNRT | 76 | 24 | 3091 | 96 GB | 10 Gbps | 1 TB | 68.1 / 152 |
| B · Lenovo ThinkEdge SE360 V2 | 36 | 16 | 2000 | 64 GB | 1 Gbps | 480 GB | 53.3 / 135 |

总计 112 台、2,400 核、标称 9,600 GB，按仓库的内存单位换算为 9,830,400 MB。总仿真算力为 6,789,984 MIPS。服务器开机时，功率以 CPU 利用率代理 SPECpower 的目标吞吐比例，按 0% 至 100% 的 11 个点线性插值。112 台同时处于空闲、45%、50% 和 100% 时的服务器 IT 功率分别为 7,094.4 W、11,575.6 W、11,832 W 和 16,412 W。场景从全部开机开始，关闭状态的服务器功率为 0 W。设施用电还需单独建模。[主机配置](../../src/test/resources/experiments/carbon/host-config/hosts.csv) · [A 类报告](https://www.spec.org/power_ssj2008/results/res2023q3/power_ssj2008-20230912-01314.html) · [B 类报告](https://www.spec.org/power_ssj2008/results/res2024q1/power_ssj2008-20240130-01362.html) · [SPECpower 字段定义](https://www.spec.org/power/docs/specpower_ssj2008-result_file_fields/#4.1.1)

## 任务样本

默认选择仓库 LCG 轨迹的首个 24 小时，共 13,651 条单核任务记录。以 2000 MIPS/核为暂定参考尺度，`Cloudlet.length = max(运行秒数, 1) × 2000`。按该时间窗运行时长的经验 P50 和 P90 将任务划为短、中、长三类，阈值分别为 307 和 12,220 秒。该时间窗的作业状态、实际内存使用量和内存请求均为缺失值。模型保留 CPU 批任务的提交时间和执行需求。[LCG 轨迹](../../src/test/resources/LCG.swf) · [Grid Workloads Archive](https://atlarge-research.com/gwa-t-11/)

到达强度定义为窗口内提交的总 MI 除以 `6,789,984 × 86,400`。用固定种子 42 对每条原始作业生成整数份副本，目标与原始平均强度之比决定期望副本数。三档共享原始记录及其提交时间，高档包含低档作业的全部副本。窗口度量的是提交的工作量，作业可以在窗口结束后完成。

| 目标强度 | 实际提交强度 | 任务数 | 短 / 中 / 长 | 任务文件 |
| ---: | ---: | ---: | ---: | --- |
| 30% | 30.0849% | 14,136 | 7,073 / 5,645 / 1,418 | [30% CSV](results/lcg-30-percent.csv) |
| 60% | 60.1346% | 28,283 | 14,136 / 11,315 / 2,832 | [60% CSV](results/lcg-60-percent.csv) |
| 90% | 90.3357% | 42,361 | 21,177 / 16,936 / 4,248 | [90% CSV](results/lcg-90-percent.csv) |

`LcgTraceWorkload.read(...)` 和 `generate(...)` 可指定轨迹窗口、参考 MIPS、目标强度和种子，再通过 `Scenario.cloudlets()` 取得可提交给 Broker 的任务列表。`cloudlet_id` 是该任务集内的唯一 ID，`source_job_id` 可回溯到原始 LCG 记录。使用空间共享 VM 调度时，VM 的每核 MIPS 决定任务执行速率。基准仿真分别使用与 A、B 主机等速的 VM 来验证异构性。

## 复现

在仓库根目录使用 JDK 25 和 Maven。在 PowerShell 中执行以下命令生成 `target/carbon-v1/lcg-*-percent.csv`。完整的单元及集成测试入口是 `mvn verify`。

```powershell
mvn '-Dtest=CarbonDatacenterIT,LcgTraceWorkloadTest' test
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$report = [xml](Get-Content -Raw 'target/surefire-reports/TEST-org.cloudsimplus.experiments.carbon.LcgTraceWorkloadTest.xml')
$classpath = ($report.testsuite.properties.property | Where-Object name -eq 'surefire.test.class.path').value
& "$env:JAVA_HOME/bin/java.exe" -cp $classpath org.cloudsimplus.experiments.carbon.LcgTraceWorkload 'src/test/resources/LCG.swf' 'target/carbon-v1'
```

本机的 `UtilizationModelPlanetLabTest` 在 Windows 上会生成非法的测试资源路径。用以下命令绕过该测试类，并显式向 Failsafe 提供主项目类路径。

```powershell
$mainClasses = (Resolve-Path -LiteralPath 'target/classes').Path
mvn '-Dtest=!UtilizationModelPlanetLabTest' '-Dfailsafe.useModulePath=false' "-Dmaven.test.additionalClasspath=$mainClasses" '-Dmaven.javadoc.skip=true' verify
```

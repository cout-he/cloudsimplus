# 单数据中心合成 CPU 工作负载

该实验在已验证的 112 台异构 Host 上生成 24 小时的 CPU 批任务。Java 场景使用 `CarbonHostFactory`，从 A 类 76 台和 B 类 36 台主机读取容量。全天理论容量为 `6,789,984 MIPS × 86,400 s = 586,654,617,600 MI`。输入是 [workload.properties](workload.properties)，运行所需的依赖只有本仓库、JDK 25 和 Maven。

Lee 等的式（39）提供了按类别使用周期平均率和高斯扰动的生成方法。下列 CPU 类别、概率、资源需求、时长、Deadline 参数和扰动幅度全部是**人工实验假设**。分段常数率、负值截零、泊松抽样和负载校准均为本项目的实现选择。[Lee 等，2026，§V-A](https://arxiv.org/html/2607.03324v2#S5.SS1)

## 初始参数

参考性能固定为 **2,000 MIPS/核**。核数采用离散分布。抽到核数 (k) 后，从表中每核内存范围按 256 MB 步长均匀抽样，再乘 (k) 取得任务的内存请求。参考时长为各类型的三角分布，采样后四舍五入到整数秒。`memory` 的一部分 8 核任务需要超过 65,536 MB，由 A 类主机承载。

| 类型 | 基准任务占比 | 核数与概率 | 每核 RAM（MB） | 参考时长最小／众数／最大（秒） | 峰值小时 | 相对振幅 | 相对噪声 σ |
|---|---:|---|---:|---:|---:|---:|---:|
| `short` | 50% | 1 核 75%，2 核 25% | 512–1,024 | 30／120／300 | 14 | 0.35 | 0.10 |
| `medium` | 28% | 2 核 35%，4 核 50%，8 核 15% | 1,024–2,048 | 300／900／2,400 | 16 | 0.45 | 0.12 |
| `long` | 12% | 4 核 45%，8 核 40%，16 核 15% | 1,024–2,048 | 1,800／5,400／14,400 | 3 | 0.50 | 0.16 |
| `memory` | 10% | 2 核 20%，4 核 60%，8 核 20% | 6,144–10,240 | 240／1,200／3,600 | 20 | 0.30 | 0.12 |

## 到达与校准

对类型 \(m\) 和窗口 \(w\)，以窗口中点 \(t_w\)（小时）计算速率，默认窗口 300 秒、四类周期均为 24 小时。\(q\) 是全局基准任务率，\(p_m\) 为该类型的基准占比，\(Z_{mw}\) 为独立的标准高斯变量。

$$
\lambda_{mw}=q p_m\max\left(0,1+A_m\cos\left(\frac{2\pi(t_w-h_m)}{\Theta_m}\right)+\sigma_m Z_{mw}\right)
$$

窗口内按恒定 \(\lambda_{mw}\) 生成指数到达间隔，留下窗口内的到达点。条件于该速率，计数服从 \(\operatorname{Poisson}(\lambda_{mw}\Delta t)\)。零速率窗口产生零任务。表中的占比是基准率权重，实际比例随采样和截零变化。

校准以期望单任务总 MI 给出的基准率为起点。每次用相同的区间随机流重算样本，并用上下界调整 \(q\)，直到实际强度与目标的绝对误差至多 0.0025，即 0.25 个百分点。三档共享任务属性分布，各有独立种子。生成器以总工作量 `Σ(required_cores × length_mi_per_core)` 除以全天理论容量求 offered load。实际 CPU 利用率由后续仿真中的排队和放置决定。

每次校准的时间开销与到达窗口数、任务数线性相关，最终按到达时间排序需 O(N log N)，任务和窗口记录占用 O(N + W) 空间。工作负载生成发生在仿真启动前。

## 字段生成与 CloudSim Plus 映射

| 顺序 | 字段 | 规则 |
|---:|---|---|
| 1 | `task_type` | 按配置依次建立每类独立的到达过程 |
| 2 | `arrival_time_s` | 该类非齐次泊松到达点，相对实验起点的秒数，范围为 `[0, 86400)` |
| 3 | `required_cores`、`required_ram_mb` | 核数按类别离散抽样，RAM 按类别和核数条件抽样 |
| 4 | `reference_runtime_s` | 按类别抽取三角分布，在 2,000 MIPS/核下的参考秒数 |
| 5 | `length_mi_per_core` | `reference_runtime_s × 2000` |
| 6 | `deadline_s` | `arrival_time_s + b + α × reference_runtime_s`，表示绝对完成时刻 |
| 7 | `task_id` | 最终样本按到达顺序分配文件内唯一 ID，Deadline 变体沿用这些 ID |

默认 **α = 2.0、b = 0**。例如到达时刻为 3,600 秒、参考时长为 1,800 秒的任务，Deadline 为 7,200 秒。超过 86,400 秒的 Deadline 保留原值。参考 CPU 上的最大等待预算为 `deadline_s − arrival_time_s − reference_runtime_s`。VM 上的等待预算按该 VM 的处理速度计算。

`SyntheticCpuTask.toCloudlet()` 调用 `CloudletSimple.of(id, length_mi_per_core, required_cores)`。`getTotalLength()` 为 `required_cores × length_mi_per_core`。当每个 PE 都得到相同的 VM 每核 MIPS 时，满 CPU 利用率下的无竞争运行时间为 `length_mi_per_core / vm_mips_per_core`。

`required_ram_mb` 按同一单位用于 VM RAM 预留。验证器从 `CarbonHostFactory` 中取得可创建 VM 的联合包络并验证每条任务。A 类包络为 24 核、98,304 MB，B 类为 16 核、65,536 MB。示例按任务的核数与 RAM 创建专用 VM，检查 `Host.isSuitableForVm()` 后固定放置。共享 VM 的调度器可以使用 `fitsVm(vm)`、`requiredRamMb()` 和 `latestStartTime(vm)` 做准入、并发预留及 Deadline 判断。RAM 和 Deadline 均由实验任务元数据提供，原生 Cloudlet 负责 CPU 执行。

## 结果

| 目标 offered load | 主任务集（α = 2.0） | 逐档统计 |
|---:|---|---|
| 30% | [workload-30.csv](results/workload-30.csv) | [30% 报告](reports/workload-30.md) |
| 60% | [workload-60.csv](results/workload-60.csv) | [60% 报告](reports/workload-60.md) |
| 90% | [workload-90.csv](results/workload-90.csv) | [90% 报告](reports/workload-90.md) |

[总验证报告](reports/validation.md) · [到达曲线](reports/arrivals.png) · [任务分布图](reports/task-distributions.png) · [样本与种子](results/manifest.csv) · [5 分钟到达明细](results/arrival-windows.csv) · [VM 包络](results/vm-shapes.csv)

Deadline 敏感性用原样本重新计算 α = 1.5 和 4.0 的截止时间。九个组合使用同样的工作量口径。每个组合检查最快可行 VM 的无等待完成时间，先天不可行记录写入 [deadline-infeasible.csv](reports/deadline-infeasible.csv)。整批调度的截止达成率需要用完整回放测量。

## 复现

从仓库根目录使用 JDK 25。以下 PowerShell 命令运行 JUnit、导出三份主任务集和六份变体、执行两次独立生成，并运行 CloudSim Plus 多核示例。Python 统计脚本需要 `numpy`、`pandas` 和 `matplotlib`。

```powershell
mvn '-Dtest=SyntheticCpuWorkloadTest,CarbonDatacenterIT,LcgTraceWorkloadTest' test
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$reportPath = 'target/surefire-reports/TEST-org.cloudsimplus.experiments.carbon.synthetic.SyntheticCpuWorkloadTest.xml'
$report = [xml](Get-Content -Raw $reportPath)
$classpath = ($report.testsuite.properties.property | Where-Object name -eq 'surefire.test.class.path').value
$experiment = 'research/carbonaware/synthetic-v1'
$config = "$experiment/workload.properties"
$java = "$env:JAVA_HOME/bin/java.exe"
$cli = 'org.cloudsimplus.experiments.carbon.synthetic.SyntheticCpuWorkloadCli'
& $java -cp $classpath $cli $config 'target/carbon-synthetic-v1/with-sensitivity' 1 --sensitivity
& $java -cp $classpath $cli $config 'target/carbon-synthetic-v1/replicated' 2
& $java -cp $classpath org.cloudsimplus.experiments.carbon.synthetic.SyntheticCpuWorkloadExample $config
python "$experiment/validate_and_plot.py" $config 'target/carbon-synthetic-v1/with-sensitivity' 'target/carbon-synthetic-v1/reports' --replicated 'target/carbon-synthetic-v1/replicated'
```

CLI 第三个参数是每档独立生成的次数，默认为 1。每档增加重复次数时，首个场景种子和任务表保持不变。第四个参数 `--sensitivity` 使用配置的松弛系数列表导出配对任务表。

修改实验参数时，可以复制配置模板，并将 `$config` 指向副本。默认配置同时用于 JUnit 的回归验收。

# Google Cluster Trace 2011 v2.1 到 Cloudlet 的转换方案

主实验将可观测的任务提交时间作为释放时间，将历史 CPU 使用量的时间积分转换为固定的正 MI。调度器选择任务实际开始时间、VM 和 Host，CloudSim Plus 根据分配的 MIPS 和完成的 MI 算出结束时间。历史 `SCHEDULE`、`FINISH` 与 `machine_id` 用于重建与校验工作量。[Google v2.1 格式与 Schema，第 2–11 页](https://drive.google.com/file/d/0B5g07T_gRDg9Z0lsSTEtTWtpOW8/view?resourcekey=0-cozD56gA4fUDdrkHnLJSrQ&usp=sharing)

## 字段与单位

| 表 | v2.1 字段 | 转换用途 |
|---|---|---|
| `task_events` | `timestamp`、`missing info`、`job ID`、`task index`、`machine ID`、`event type`、`user name`、`scheduling class`、`priority`、CPU/RAM/本地磁盘请求、`different-machine constraint` | 组合键、生命周期、提交时资源请求、筛选和分层。优先级是有序等级，调度类描述时延敏感程度 |
| `task_usage` | 采样区间起止时间、组合键、机器 ID、平均 CPU、最大 CPU、canonical/assigned/maximum memory、页缓存、磁盘、CPI、MAI、sample portion、aggregation type、sampled CPU | 平均 CPU 用于积分。最大 CPU 和 v2.1 新增的随机 1 秒 sampled CPU 用于诊断。canonical memory 用于历史实际内存统计，maximum memory 可检验请求是否偏小。assigned memory 指容器分配量 |
| `machine_events` | 时间、机器 ID、`ADD/REMOVE/UPDATE`、匿名 platform ID、CPU/RAM 容量 | 时间扫线还原在线容量及异构资源比例。机器可反复上下线，因此同时报告不同机器 ID 总数和在线峰值。按 platform ID 与 CPU/RAM 容量组合统计机器类别。每个归一化维度的保留量和整机功耗另行标定 |
| `machine_attributes` | 时间、机器 ID、属性名、属性值、删除标志 | 识别属性生效区间与放置限制。属性名和值可能匿名化，部分整数是属性值的排序编号 |
| `job_events` | 时间、`missing info`、job ID、事件类型、匿名用户名、调度类、匿名 job name 与 logical job name | 核对作业生命周期、任务构成及同类作业，量化代理队列的选择偏差 |
| `task_constraints` | 任务键、时间、属性比较操作、属性名和值 | 建立精细实验队列前审计放置限制，按需要剔除带约束的任务 |

上述资源量按各自维度的最大观测机器容量独立缩放，分别保留归一化 CPU 核数或核秒/秒、内存字节、本地磁盘字节、磁盘时间占比及其对应的归一化值。归一化 CPU 表示相对**整个 trace 的 CPU 基准**，也适用于 task usage。它无法直接作为某台机器或 VM 的利用率。CPI、MAI 分别是每条指令的周期数、内存访问次数。没有与采样区间对应的原始执行周期数及硬件校准，CPI 无法独立恢复指令数。时间戳的单位为微秒，真实观测窗口始于原始时间 `600_000_000`，原始 `0` 标记窗口前，官方 `2^63−1` 与在末尾任务事件分片实际观察到的 `2^64−1` 均作为窗口后标记。[官方数据说明](https://github.com/google/cluster-data/blob/master/ClusterData2011_2.md)，[v2.1 Schema](https://drive.google.com/file/d/0B5g07T_gRDg9Z0lsSTEtTWtpOW8/view?resourcekey=0-cozD56gA4fUDdrkHnLJSrQ&usp=sharing)

## 生命周期与候选队列

按 `(job_id, task_index)` 分组，再按时间、分片编号和原始行号重建每次尝试。同时间戳允许终止后立即再次 `SUBMIT`，也允许 `SUBMIT` 后立即 `SCHEDULE`。状态机从等待到获派机器，再由 `EVICT`、`FAIL`、`FINISH`、`KILL` 或 `LOST` 结束该尝试。两个 `UPDATE` 记录等待或运行中的请求、类别或约束变化。保留首次提交、历次尝试、末次结果等口径。历史 `SCHEDULE` 是资源获派时刻，可能早于任务进程开始运行。用 `FINISH − SCHEDULE` 衡量历史获派后的持续时间，用采样 CPU 积分估计计算量。[v2.1 Schema，第 6–11 页](https://drive.google.com/file/d/0B5g07T_gRDg9Z0lsSTEtTWtpOW8/view?resourcekey=0-cozD56gA4fUDdrkHnLJSrQ&usp=sharing)

主分析采用独立 CPU 批任务代理队列。统计脚本的候选规则是在已核验的连续覆盖范围内仅观测到同一作业的一项任务，调度类为 0 或 1，具有恰好一次真实且完整的 `SUBMIT → SCHEDULE → FINISH`，运行时长为正，运行开始时 CPU 和 RAM 请求均为正。剔除历史重试或驱逐、失败、取消、丢失、运行中请求更新、显式反亲和，以及出现 Google 补写事件的任务。`missing info` 空字段代表未标记补写，数值 `0` 代表一种补写类型。若已处理 `task_constraints`，则进一步排除表中观测到的放置约束。任务运行窗口需处于用量覆盖范围内，同机器匹配的 CPU、RAM 有效覆盖各至少 90%，且 CPU 积分为正。本次候选口径使用长度不超过 300 秒的采样记录。分别报告预筛、用量关联后的有效任务和各排除原因。完整计数和覆盖见 [工作负载分析](workload_analysis.md)。

作业匿名字段、调度类和任务索引无法证明业务依赖关系、线程宽度、CPU 密集度或 GPU 使用。即使一个 Job 只观测到一个 Task，也可能存在作业间协调或依赖。正式投入仿真前，优先审计完整事件覆盖、`task_constraints` 和逻辑作业名重复情况，并用只保留 class 0、扩大至 class 0–1、收紧覆盖阈值、限制磁盘 I/O 或最小 CPU 强度等口径做敏感性分析。磁盘 I/O 很低仍只是 CPU 型代理。成功且可观测的筛选会低估失败、长尾与极短任务比例，结论适用于筛后队列。模型的单任务 PE 数为容量映射结果，归一化 CPU 的平均值或峰值均无法识别真实并行度。

## 输入协议与资源映射

| Cloudlet 输入 | 建议规则 | 来源性质 |
|---|---|---|
| 标识 | 保留 `job_id`、`task_index` 和 attempt。以稳定递增数值作 Cloudlet ID，保留映射表 | 原始标识与派生索引 |
| 到达时间 `a_i` | 首个合格 `SUBMIT` 的微秒值相对实验窗口原点变成秒。完整 trace 原点为原始 600 秒。相同释放时间按稳定 ID 排序 | 原始观测 |
| 参考 CPU 核数 `q_i` | `max(1, ceil(r_cpu_i × K_C))`，其中 `r_cpu_i` 是归一化请求，`K_C` 表示规范化值 1 对应的参考 vCPU 数。用于 VM 的 PE 下界和任务并发配额 | 观测请求与场景标定 |
| RAM 配额 `m_i` | `ceil(r_ram_i × K_M_bytes / 2^20)`，`K_M_bytes` 为规范化值 1 对应的参考字节数。CloudSim Plus 的 `Ram` 文档标记为 MB，当前 `BytesConversion` 每单位采用 1024² 字节 | 观测请求与场景标定 |
| 文件大小、输出、带宽 | trace 的本地运行时磁盘请求体现暂存空间。CPU 实验可采用框架约定的最小占位与显式固定的通信延迟假设，配置中保留该假设 | 实验设定 |
| 优先级、调度类与 Deadline | 原始等级作为元数据，调度规则和 Deadline 作为研究输入 | 观测等级与实验设定 |

保留小数 `r_cpu_i × K_C` 用于比较整核取整的消耗。全量计算中报告取整差额、单核最低粒度造成的请求膨胀、无法匹配任何 VM 的任务比例及资源约束维度。参考 `K_C`、`K_M_bytes` 与每核 MIPS 使用明确的场景配置，在多个尺度下复核。请求采用 `SUBMIT` 已有值，若 `UPDATE_PENDING` 改变请求，后续仿真可在更新的相对时间应用新版本。固定请求的首版主队列对这类任务优先检查提交与获派版本是否相同，缺少提交请求或发生改变的记录需单列。这一步可能使实际可导入任务数低于统计队列的候选数。

RAM 用请求量做硬预留，同时可用 `UtilizationModelDynamic(Unit.ABSOLUTE, ramUnits)` 表示常量绝对 RAM 用量。需要拟合用量时，可从 canonical 使用序列派生模型，硬预留仍由准入记录控制。并发运行的 Cloudlet 必须同时满足 CPU 和 RAM 合计限制。构建 VM 规格时以 CPU/RAM 的**联合覆盖**决定最小可行规格，并为无规格匹配的任务记录原因。一个任务超过最大规格时，允许加入单独报告、可落地的特大规格，或显式拒绝并纳入 SLA 分母。[本地 Ram](../../src/main/java/org/cloudsimplus/resources/Ram.java)，[本地 BytesConversion](../../src/main/java/org/cloudsimplus/util/BytesConversion.java)

## CPU 积分到每核 MI

设任务的单次历史获派区间为 `[s_i, f_i)`。通过任务键和历史机器 ID 关联有效采样区间 `[b_j, e_j)`，将区间截断到尝试窗口。本次分析按截断后的起止时间和用量值为采样排序，分别从 CPU 与 RAM 的后续记录中移除已覆盖时间。重新建模时应保留重叠质量标记，用丢弃冲突区间的敏感性结果检验该规则。对截断的片段采用记录的平均 CPU，等价于假设该片段与完整采样区间的平均使用相同。分别计算 CPU 与 RAM 的覆盖比例。

$$
\Delta t_j=\max\{0,\min(e_j,f_i)-\max(b_j,s_i)\},\qquad
I_i=\sum_j \bar u_{ij}\Delta t_j,\qquad
g_i=\frac{\sum_j \Delta t_j}{f_i-s_i}.
$$

时间量先从微秒换为秒。\(\bar u_{ij}\) 为归一化平均 CPU，\(I_i\) 的单位为归一化核秒。以设定或用基准校准的 \(K_C\) 还原为参考核秒，再乘参考每核计算速率 \(M_{ref}\)（MI/核秒）。Cloudlet 的 `length` 以**每核 MI** 计，总工作量对应 `length × cores`。[本地 Cloudlet.getLength/getTotalLength](../../src/main/java/org/cloudsimplus/cloudlets/Cloudlet.java)

$$
\widehat W_i^{\mathrm{obs}} = I_i K_C M_{ref},\qquad
\widehat L_i=\max(1,\lceil\widehat W_i^{\mathrm{obs}} / q_i\rceil).
$$

创建 `CloudletSimple.of(cloudletId, lengthPerCoreMi, cores)` 后设置正长度、使用模型及元数据。计算中保留双精度，写入整型前检查有限性、正数和总量溢出。按每核向上取整后，多出的总工作量小于 \(q_i\) MI。\(\widehat W_i^{\mathrm{obs}}\) 来自已覆盖区间。若希望估计完整尝试工作量，可追加 \(\widehat W_i^{\mathrm{obs}}/g_i\) 敏感性场景，前提是缺口与已观测区间平均强度相当。缺口未知、边缘窗口混合、资源竞争、机器间每核性能差异、任务未始终运行，以及 CPI 的工作阶段变化都会影响估计。最大 CPU、随机 1 秒采样与 sample portion 用于质量分层和诊断。主积分使用均值乘观察时长。原始字段只说明历史 CPU 时间，参考 MIPS 提供的是可控实验尺度。[v2.1 Schema，第 9–11 页](https://drive.google.com/file/d/0B5g07T_gRDg9Z0lsSTEtTWtpOW8/view?resourcekey=0-cozD56gA4fUDdrkHnLJSrQ&usp=sharing)

例如，假设 \(K_C=16\)、\(M_{ref}=2500\) MI/核秒，CPU 请求为 0.10，任务连续两个窗口分别为 60 秒、40 秒，平均归一化 CPU 为 0.05、0.02。估计参考 CPU 时间是 60.8 核秒，工作量是 152,000 MI，使用 2 PE 时每核长度为 76,000 MI。相同速率下的 Full 模型预计执行 30.4 秒，与历史获派持续时间 100 秒可以不同。这组数字用于核对单位和 API 语义。

## CPU 利用率方案

| 方案 | 使用方式 | 适用范围 |
|---|---|---|
| `UtilizationModelFull` | 每核以可用计算速率执行，长度使用上式。调度时以实际分配的 MIPS、VM 竞争和启动时间确定完成时刻 | 可解释的 CPU 批任务基线。低占空比任务在 Full 模型中的执行可能缩短，资源峰值可能被夸大 |
| `UtilizationModelDynamic` 常量比例 | 用参考归一化平均负载估计每核活跃比例 \(p_i=K_C I_i/(q_i \sum_j\Delta t_j)\)，在参考硬件先校准。\(p_i>1\) 时标记核数或观测不一致，单列后再选择调整核数或剔除 | 保持固定工作量并近似保留历史占空比的对照。预留核与活跃核分开解释，比较时用同一批任务和工作量 |
| 相对进度的 trace 模型 | 构建任务计算阶段与利用率序列，按实际虚拟执行起点或累积计算进度推进阶段。使用相对可用 MIPS 的每核比例，阶段边界需要验证是否与处理更新对齐 | 验证能耗结果对波动的敏感性。零利用率阶段需要可推进的时钟或显式等待阶段，处理事件过稀可能漏掉阶段变化 |

若观察到的低利用率导致静态请求取整后明显放大空闲预留，优先并报 Full 与常量占空比。使用量已经进入 MI，利用率模型只改变服务速率和能耗轨迹，工作量保持一致。以历史绝对时间挂载 CPU 更新会将旧调度的时间耦合引入反事实调度。专用的动态模型只在具备相对时间或进度时采用。

## Deadline、SLA 与校验

这五张表的 schema 没有期限或合同 SLA。用 `d_i = a_i + b_i + α × t_ref_i` 构造绝对 Deadline，\(t_{ref,i}\) 由参考资源配置和已导出的正 MI 计算，\(b_i\) 为显式配置的基础宽限秒数，\(α\) 为宽松系数。将不同宽限策略写入实验清单。历史 `FINISH` 可用于离线比较构造后期限相对于观察排队时间的松紧。正式模型按 `a_i ≤ start_i` 和 `C_i ≤ d_i` 检查。用单任务最快可行速率与不可省略的冷启动时间筛查先天不可行期限，记录为场景设定造成的不可行。逐任务报告超期量 `max(0, C_i − d_i)`，同时统计失约率、未完成或拒绝比例、排队和完成时延。说明调度器在任务到达时可见的工作量是估计先验，历史运行样本用于离线构造输入，评估时可增加估计误差，避免将这一先验等同于在线业务的完美预测。

校验应包含人工可算的积分、重复与重叠区间、截断、稀疏采样和同时间戳重提交。用固定 VM MIPS 做守恒和单位校验，独占条件下检查 `length / 单核实际 MIPS`。再只改变 VM 单核 MIPS、提交延迟或并发竞争，确认完工时刻相应变化，且结果日志只从仿真器的完成回调读取。历史拟合按同机、持续时间分层及留出时段检查恢复误差，报告窗口覆盖阈值敏感性。统计脚本得出的历史负载、按提交时刻平移的需求以及仿真产生的负载，分别记录时间轴和口径。

清洗用 GZIP 流式读取与按复合任务键分区的外存关联，保留原始行的文件和行号以及排除原因。可排序地归并同一任务的事件与采样后，只输出一行可运行的任务记录和必要的可选阶段文件。I/O 近似随记录数线性增长，排序成本按分区累计，峰值内存取决于批次及分区，仿真事件不随原始采样行数增长。现有 replay 读取器的 API 边界见 [CloudSim Plus 改造评估](cloudsim_modification_plan.md)。

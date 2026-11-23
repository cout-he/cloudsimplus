# CloudSim Plus 仿真适配与改造评估

当前版本可以承载本研究的单数据中心、动态到达、按需 VM 和无迁移初始放置。推荐在独立实验模块用已有工厂、监听器和策略接口编排，先以真实提交时间和正 MI 跑出无迁移基线，再接入 SLA 与运行碳排放。此次提交范围为调查和设计，源码基线见下表。

## 仓库与运行环境

| 项目 | 核查结果 |
|---|---|
| 工作目录 | `E:\a_myprofile\a-cloudsimplus` |
| 分支与提交 | `学习与复现分支`，`4a160b4bba6038b34f5545e023a384f396c32f5c`，2026-11-13，`Part of #603: Share CoreCapacity and BootModel instances among machines (#649)` |
| Maven 版本 | 仓库产物 `org.cloudsimplus:cloudsimplus:9.0.0-SNAPSHOT`，本机 Apache Maven 3.9.16 |
| Java | 默认终端为 Temurin 1.8.0_504。按 `.vscode/settings.json` 为测试进程设置 Java 25，实测 Oracle JDK 25.0.5.1 |
| 工作树与测试范围 | 开始时已有未跟踪的 `research/` 与 `src/test/resources/NASA-iPSC-1993-3.1-cln.swf`。三个已存在的 Google 读取器单测共 23 项通过，0 失败、0 错误、0 跳过。Maven 输出既有许可证告警。此次核查未执行全套集成测试 |

在 Java 25 环境中，可用以下 PowerShell 命令复现读取器测试。

```powershell
$env:JAVA_HOME = 'C:\Users\lll\.jdks\jdk-25.0.5.1'
$env:Path = "$env:JAVA_HOME\bin;$env:Path"
mvn -B test '-Dtest=GoogleTaskEventsTraceReaderTest,GoogleTaskUsageTraceReaderTest,GoogleMachineEventsTraceReaderTest'
```

官方 `GoogleTaskEventsExample1` 展示按用户建 Broker、从事件创建 Cloudlet、从用量更新资源模型以及通过负长度配合历史完成事件的工作流。`DynamicCreationOfVmsAndCloudletsExample` 展示在回调中动态提交 VM 和 Cloudlet。已核对官方示例源码。本地相邻目录没有 `cloudsimplus-examples` 仓库，此次用本地读取器测试验证对应机制。历史示例使用旧构造方式，实验以本地提交中的 `of(...)`、`CoreCapacity` 及 `listeners()` 为准。[框架仓库](https://github.com/cloudsimplus/cloudsimplus)，[官方 Google 任务示例](https://github.com/cloudsimplus/cloudsimplus-examples/blob/master/src/main/java/org/cloudsimplus/examples/traces/google/GoogleTaskEventsExample1.java)，[官方动态创建示例](https://github.com/cloudsimplus/cloudsimplus-examples/blob/master/src/main/java/org/cloudsimplus/examples/dynamic/DynamicCreationOfVmsAndCloudletsExample.java)

## 现有 Google 读取器

| 类 | 已支持的内容 | 适配注意事项 |
|---|---|---|
| [GoogleTaskEventsTraceReader](../../src/main/java/org/cloudsimplus/traces/google/GoogleTaskEventsTraceReader.java)、[TaskEventType](../../src/main/java/org/cloudsimplus/traces/google/TaskEventType.java) | 读取任务事件，`SUBMIT` 创建 `FROZEN` 的 Cloudlet，`SCHEDULE` 发 `CLOUDLET_READY`，历史 `FINISH` 发 `CLOUDLET_FINISH`，驱逐、失败、取消各有状态事件映射。`LOST` 和两类 `UPDATE` 在事件枚举的默认状态路径无操作。读取器另有属性更新路径和可选历史 VM 映射 | `setAutoSubmitCloudlets(false)` 只控制自动提交，仍会冻结 Cloudlet 并安排历史事件。改造实验使用单独的预处理输出作为输入 |
| [TaskEvent](../../src/main/java/org/cloudsimplus/traces/google/TaskEvent.java)、[TaskEventField](../../src/main/java/org/cloudsimplus/traces/google/TaskEventField.java)、[TaskData](../../src/main/java/org/cloudsimplus/traces/google/TaskData.java) | 表达事件类型、时间、Job ID、任务索引、机器 ID、用户、类别、优先级以及 CPU/RAM/本地磁盘请求。列枚举识别 `missing info` 和反亲和 | `TaskEvent.of()` 没有把后两列纳入 DTO，且空资源请求由列读取器默认成 0。`actualCpuCores(maxCpuCores)` 截断取整。现有任务 ID 用十进制字符串拼接，组合不同长度可碰撞，也存在超出 long 的可能。分析用复合键，仿真另分配稳定长整型 ID |
| [GoogleTaskUsageTraceReader](../../src/main/java/org/cloudsimplus/traces/google/GoogleTaskUsageTraceReader.java)、[TaskUsage](../../src/main/java/org/cloudsimplus/traces/google/TaskUsage.java) | 解析区间起止、Job/任务/机器、平均和最大 CPU、canonical/assigned/maximum RAM、部分磁盘用量。列枚举还识别两类页缓存。运行中按历史采样起点更新已找到 Cloudlet 的 CPU 均值与 canonical RAM | DTO 未完整保留页缓存及 v2.1 的 CPI、MAI、sample portion、aggregation type、随机 1 秒 sampled CPU。历史时钟事件与归一化值直接作为使用动态模型输入的行为适合原有 replay。新实验离线关联采样并计算工作量 |
| [GoogleMachineEventsTraceReader](../../src/main/java/org/cloudsimplus/traces/google/GoogleMachineEventsTraceReader.java)、[MachineEventField](../../src/main/java/org/cloudsimplus/traces/google/MachineEventField.java) | 解析机器 ID、时间、`ADD/REMOVE/UPDATE`、CPU/RAM 容量，构造初始 Host 或安排后续主机事件 | 以配置最大核数和 RAM 放大后取整，默认 8 核、16 GiB，属于读取器的仿真设定。回调 DTO 没有传递原始 platform ID，列枚举将其定义为整型。离线分析保留原始匿名 platform 字符串和归一化容量。主实验 Host 规格由容量配置生成 |
| [TaskVmMapper](../../src/main/java/org/cloudsimplus/traces/google/TaskVmMapper.java) | 包内部的 replay 助手，按历史用户名和任务首次已知的机器分组，汇总各维度同时运行请求的峰值，创建、绑定 VM，并可按历史机器决定放置 | 采用了历史上已知的物理机和未来峰值，适合 replay 校对。优化实验的虚实映射由外部调度器实时决定 |
| [TraceReaderAbstract](../../src/main/java/org/cloudsimplus/traces/TraceReaderAbstract.java)、[GoogleTraceReaderAbstract](../../src/main/java/org/cloudsimplus/traces/google/GoogleTraceReaderAbstract.java) | 可参考原有文本和压缩输入、列位置和小样本检查。Google 读取器积累仿真对象，部分子类还会积累未来事件 | v2.1 全量预处理需要跨表关联、保留原始字段、处理空值和截尾。独立流式预处理负责完整的 `job_events`、`machine_attributes` 和放置约束解析。每次仿真只读取所配置的导出任务窗口 |

采用两个层次的复用。历史回放运行现有读取器作为行为对照。正式实验使用离线任务表加框架扩展点，历史事件仅供校验。这样还可以测量 replay 与新策略结果差异来自哪些假设。

## 改造清单

表中类名为建议的实验模块命名。各项目可通过现有公开 API 集成。

| 模块 | 本地支持与可复用接口 | 建议新增与实现方式 | 核心改动 | 建模误差与检查 |
|---|---|---|---|---|
| 数据集预处理 | Google 读取器、Trace 列枚举提供参考 | 复用本次离线数据脚本的分区读取和稳定键口径。后续新增 `TraceWorkloadSource` 读取按时间排序的仿真输入，保留观测覆盖、原始字段与场景标定 | 无 | 区分缺失和真实零，核对同一微秒时间戳、同机器和重叠窗口。对跨表关联和保留率做质量检查 |
| Cloudlet 创建与 MI 转换 | `CloudletSimple.of`、`setUtilizationModelCpu`、`setUtilizationModelRam`、`CloudletListeners` | `TraceCloudletFactory` 依据预处理任务元数据，构造有正的每核 MI 的 Cloudlet。内存和释放时间作为调度元数据。历史完成时间仅留在验证表 | 无 | 一核取整、短任务采样缺失、每核性能差异以及 MI 的参考尺度都会影响结果，按转换报告校验 |
| 真实任务动态到达 | `CloudSimEntity` 的整数 tag `schedule`、`DatacenterBroker.submitCloudlet`、可选 `submissionDelay` | `WorkloadArrivalSource` 以到达时钟驱动，读下一组同一释放时刻的记录，调度一次到达事件。任务先进入研究调度队列，在选定允许时刻和 VM 就绪后提交 | 无 | 使用当前仿真时间计算未来相对延迟。动态提交时防止重复套用历史相对延迟。验证稳定的同时间戳顺序及无任务间隙中的 Broker 活性 |
| Deadline 与 SLA | `Cloudlet.getStartTime/getFinishTime`、`listeners().addOnStart/addOnFinish`、`SlaContract` 可装载 SLA JSON | `TaskRequirements` 记录释放时间、期限和资源。`DeadlineEvaluator` 检查约束、统计准时率、未完成及拒绝。把策略的可行性判定与业务 SLA 指标做成配置 | 无 | SLA JSON 不会自动执行逐 Cloudlet Deadline。显式配置截止时刻和任务处置语义，统一失约分母 |
| VM 按需创建与回收 | `VmSimple.of(CoreCapacity, scheduler)`、`submitVm`、VM 创建/失败与主机放置监听器、`Vm.setBootModel`、`setVmDestructionDelayFunction`、`Vm.shutdown()` | `VmLifecycleManager` 维护准备、活跃、释放状态。已有 VM 可容纳则复用，缺少时申请最小可行规格。在创建确认和可用后派发，所有运行、已排队和已承诺的任务均离开才回收。未使用的新建 VM 可由管理器在宽限期后主动关闭 | 无 | 预先检查 Host 可用容量，限制与记录创建失败和重试。Broker 某些创建失败路径可主动关闭，即使普通空闲关闭已禁用。高负载下测试失败回收、重复提交和预留泄漏 |
| Cloudlet → VM | `DatacenterBroker.setVmMapper(Function<Cloudlet, Vm>)`、`bindCloudletToVm`、`CloudletSchedulerSpaceShared` | `TaskVmSelector` 查询可用 VM、开始约束及剩余 CPU/RAM。由调度器保持任务等待并固定映射，映射函数只查本轮已选结果。主基线可允许同一 VM 中的多个独立 Cloudlet 按核空间共享，超出可用 CPU 或 RAM 时留在研究队列 | 无 | SpaceShared 启动时主要看空闲 CPU 核。调度器层还会模拟 RAM 不足后的行为，因此硬 RAM 约束需在提交前由研究准入逻辑维护。先验证一 VM 一任务，再比较多任务共置 |
| VM → Host 初始放置 | `DatacenterSimple.of`、`VmPlacementPolicySimple`、`setFindHostForVmFunction`、`Host.isSuitableForVm`、`ResourceAllocatorSimple`、`VmSchedulerSpaceShared` | 先以非迁移的现有放置策略为对照，再通过选择回调接入 `InitialHostSelector`，按 CPU、RAM 和每核 MIPS 检筛候选。启用分配失败处理。单 Datacenter 调用 `disableMigrations()` | 无 | 测试整核、内存碎片和启动占位，记录 VM 从创建到回收期间的 host ID。只换 Host 额定 MIPS 时，VM 自身 MIPS 配置仍可能限制实际速率 |
| 后续功率与碳排放 | 每 Host 可配置 `PowerModelHostSimple` 或 `PowerModelHostSpec`，`PowerModelDatacenterSimple` 可汇总，`PowerMeter` 提供定间隔采样 | `CarbonIntensitySeries` 提供带时区的外部电力碳强度。`CarbonAccounting` 对重叠区间积分。IT 基线使用主机功率，PUE 保持框架默认 1，按场景决定是否计入开关机 | 无 | Google trace 无服务器空闲、满载或启动功率，也无电网碳强度。功率采样间隔过大可能漏掉短任务峰值。框架默认模型对非活动机器给出零实测功耗，开关机能量应单独明确 |
| 结果统计与复现 | Broker 的完成列表、`CloudletsTableBuilder`、Cloudlet/VM/Host 监听器及 `testbeds` 基类 | `ExperimentRecorder` 输出到达、开始、完成、资源、映射、失败原因和功率。记录仓库 SHA、数据 manifest 校验、分析窗口、筛选阈值、参数、种子及结果文件 | 无 | 成功任务结果表无法覆盖拒绝和未完成。用输入数量与各最终状态对账。保存未完成、失约与策略排队日志 |

## 运行路径与工程边界

用一个 `DatacenterBrokerSimple` 作为执行入口，调用 `setShutdownWhenIdle(false)` 保持多次到达之间的空闲期。到达流每次只安排下一批，待所有到达处理并确认完成、拒绝或达到预先设定的截尾条件后自然收尾。对于一个任务，“允许开始时刻”由外部调度器选择，真正开始时刻由 `Cloudlet.getStartTime()` 记录。实际开始可能因 Host 与 VM 启动时间、已分配 PE、Broker 实现中的提交时机与数据中心更新粒度推迟。将两者同时写入日志，以核查算法决策是否落实。

VM 状态需区分已申请、已创建、已就绪和回收中。创建和回收是有延迟的，提交 Cloudlet 前同时确认 VM 创建成功和模型所需的就绪条件。VM 闲置 TTL 与尚未进入 VM 队列的预留任务协调，允许资源管理器在 VM 从未执行任务的边界情况下主动回收。直接映射未就绪 VM 可能令请求长期等待。单数据中心可用 `Host.isSuitableForVm` 加研究侧启动预留过滤创建请求，同时为失败或暂时满载明确重入队路径和停止条件。源码中 `processVmCreationFailure()` 的默认重试耗尽路径会关闭 Broker。首版优先采用外部准入、防止连续送入不可部署的规格并覆盖失败集成测试。若必须支持高频故障下定制 Broker 的失败恢复，再评估继承 `DatacenterBrokerSimple` 并覆盖可用行为，依据验证结果判断是否需要向框架提出扩展点。[本地 Broker 失败与回收实现](../../src/main/java/org/cloudsimplus/brokers/DatacenterBrokerAbstract.java)，[本地 VM 事件监听器](../../src/main/java/org/cloudsimplus/vms/VmListeners.java)

功率与碳排放用一致的时钟累计。功率以 W、时间以秒、碳强度以 gCO₂e/kWh 表示。区间能耗 `E_k = P_k Δt_k / 3,600,000`，碳排放 `C = Σ E_k × I_k`，其中 \(I_k\) 为当时碳强度。对功率或电力碳强度变化点拆分区间。固定时间窗包含空闲功耗，启停功率如已出现在采样曲线中则只计入一次。仿真设定应固定时区和日期映射、功率曲线、采样间隔与碳强度来源。切换任务到达窗口时记录该映射。制冷、电池和 GPU 置于实验边界外。[本地 PowerMeter](../../src/main/java/org/cloudsimplus/power/PowerMeter.java)，[本地 PowerModelHostSimple](../../src/main/java/org/cloudsimplus/power/models/PowerModelHostSimple.java)，[本地 PowerModelDatacenterSimple](../../src/main/java/org/cloudsimplus/power/models/PowerModelDatacenterSimple.java)

### 实施与验证顺序

1. 使用转换报告确定可导入窗口、单位和边界记录，用少量任务检查正 MI 的完成时间、改变 VM MIPS 后的结果以及历史事件未入仿真队列。
2. 接上真实到达和研究调度队列，验证同批到达、跨空闲间隔、工作量突发、VM 冷启动、回收与重新创建。
3. 依次接入硬 CPU/RAM 预留、VM 规格匹配、无迁移的初始放置和 Deadline。测试启动资源冲突、超规格、无可用 Host、超期、结束时仍有任务等情况。
4. 最后接上 Host 功率与分时碳强度，在常量功率和常量碳强度下验证积分，再对时间粒度、CPU 占空比及选择窗口做敏感性验证。容量范围从 [Host 和 VM 配置建议](datacenter_sizing_recommendations.md) 取出后形成正式可复现实验场景。

到达事件数随不同释放时刻数增长，读入工作量的额外内存随批量上限及当前待执行任务增长。调度选择在到达、资源释放和策略决策时执行，朴素扫描最坏为每次 `O(V + H)`，分别考察候选 VM 和 Host。大规模时用规格桶及容量索引减少扫描。现有 Broker 会保留创建和完成列表，流式导入只约束预处理与输入的暂存，并不能保证仿真总内存随运行任务数封顶。对全量规模先做内存和事件数量压测，必要时采用多个独立时间窗，保持其各自负载与碳强度映射。运行状态与资源更新沿用框架现有路径，避免每个使用采样或每次 Cloudlet 更新新增事件与对象。

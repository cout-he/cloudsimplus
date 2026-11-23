/*
 * CloudSim Plus: A modern, highly-extensible and easier-to-use Framework for
 * Modeling and Simulation of Cloud Computing Infrastructures and Services.
 * http://cloudsimplus.org
 *
 *     Copyright (C) 2015-2021 Universidade da Beira Interior (UBI, Portugal) and
 *     the Instituto Federal de Educação Ciência e Tecnologia do Tocantins (IFTO, Brazil).
 *
 *     This file is part of CloudSim Plus.
 *
 *     CloudSim Plus is free software: you can redistribute it and/or modify
 *     it under the terms of the GNU General Public License as published by
 *     the Free Software Foundation, either version 3 of the License, or
 *     (at your option) any later version.
 *
 *     CloudSim Plus is distributed in the hope that it will be useful,
 *     but WITHOUT ANY WARRANTY; without even the implied warranty of
 *     MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 *     GNU General Public License for more details.
 *
 *     You should have received a copy of the GNU General Public License
 *     along with CloudSim Plus. If not, see <http://www.gnu.org/licenses/>.
 */
package org.cloudsimplus.experiments.carbon.replay;

import org.cloudsimplus.experiments.carbon.replay.VmBaselineReplay.Result;
import org.cloudsimplus.experiments.carbon.replay.VmBaselineReplay.TaskResult;

import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.function.ToDoubleFunction;

/// Writes the observed replay state as task, VM, time-series, Host and summary CSV files.
final class ReplayOutput {
    private ReplayOutput() { }

    static void write(final Path directory, final Path input, final ReplayConfig config, final Result result) throws IOException {
        Files.createDirectories(directory);
        writeTasks(directory, result);
        writeVms(directory, result);
        writeSamples(directory, result);
        writeHosts(directory, result);
        writeSummary(directory, input, config, result);
    }

    private static void writeTasks(final Path directory, final Result result) throws IOException {
        try (var writer = Files.newBufferedWriter(directory.resolve("tasks.csv"))) {
            writer.write("task_id,task_type,arrival_time,required_cores,required_ram_mb,length_mi_per_core,vm_id,host_id,"
                + "admission_time,start_time,finish_time,waiting_time,actual_execution_time,turnaround_time,deadline,deadline_missed,tardiness,status\n");
            for (final var item : result.tasks()) {
                final var task = item.task();
                row(writer, task.taskId(), task.taskType(), task.arrivalTime(), task.requiredCores(), task.requiredRamMb(),
                    task.lengthMiPerCore(), identifier(item.vmId()), identifier(item.hostId()), item.admitted(), item.start(), item.finish(),
                    item.waiting(), item.execution(), item.turnaround(), task.deadline(),
                    item.completed() ? item.deadlineMissed() : null, item.completed() ? item.tardiness() : null, item.status());
            }
        }
    }

    private static void writeVms(final Path directory, final Result result) throws IOException {
        try (var writer = Files.newBufferedWriter(directory.resolve("vms.csv"))) {
            writer.write("vm_id,vm_type,cores,ram_mb,mips_per_core,host_id,created,ready,shutdown_started,destroyed,"
                + "task_count,lifetime_seconds,busy_seconds,idle_seconds\n");
            for (final var vm : result.vms()) {
                row(writer, vm.id(), vm.spec().id(), vm.spec().cores(), vm.spec().ramMb(), vm.mips(), vm.hostId(),
                    vm.created(), vm.ready(), vm.shutdownStarted(), vm.destroyed(), vm.taskCount(),
                    vm.destroyed() - vm.created(), vm.busySeconds(), vm.idleSeconds());
            }
        }
    }

    private static void writeSamples(final Path directory, final Result result) throws IOException {
        try (var writer = Files.newBufferedWriter(directory.resolve("timeseries.csv"))) {
            writer.write("time,arrivals,running_tasks,queued_tasks,global_queue,active_vms,cpu_utilization,ram_utilization,"
                + "vm_ram_utilization,it_watts\n");
            for (final var point : result.samples()) {
                row(writer, point.time(), point.arrivals(), point.running(), point.queued(), point.globalQueue(),
                    point.activeVms(), point.cpuUtilization(), point.ramUtilization(), point.vmRamUtilization(), point.watts());
            }
        }
    }

    private static void writeHosts(final Path directory, final Result result) throws IOException {
        try (var writer = Files.newBufferedWriter(directory.resolve("hosts.csv"))) {
            writer.write("host_id,mips_capacity,ram_capacity_mb,peak_cpu_utilization,peak_ram_utilization,peak_vm_ram_utilization,"
                + "window_cpu_utilization,window_ram_utilization,window_vm_ram_utilization,mean_it_watts,window_it_kwh,drain_it_kwh\n");
            for (final var host : result.hosts()) {
                final var metrics = host.metrics();
                row(writer, host.hostId(), host.mipsCapacity(), host.ramCapacity(), host.peakCpu(), host.peakTaskRam(),
                    host.peakVmRam(), metrics.cpuUtilization(), metrics.ramUtilization(), metrics.vmRamUtilization(),
                    metrics.avgWatts(), metrics.windowKwh(), metrics.drainKwh());
            }
        }
    }

    private static void writeSummary(final Path directory, final Path input, final ReplayConfig config,
                                     final Result result) throws IOException {
        final var completed = result.tasks().stream().filter(TaskResult::completed).toList();
        final var late = completed.stream().filter(TaskResult::deadlineMissed).toList();
        final int total = result.tasks().size();
        final int others = total - completed.size();
        final long work = result.tasks().stream().mapToLong(item -> item.task().totalMi()).sum();
        final var metrics = result.metrics();
        final List<Object> values = new ArrayList<>(Arrays.asList(input.getFileName(), checksum(input), total,
            completed.size(), completed.size() - late.size(), late.size(), others, divide(completed.size(), total),
            divide(late.size(), completed.size()), divide(late.size() + others, total), work,
            work / (result.capacityMips() * config.windowSeconds())));

        addStatistics(values, completed, TaskResult::waiting);
        addStatistics(values, completed, TaskResult::execution);
        addStatistics(values, completed, TaskResult::finish);
        addStatistics(values, completed, TaskResult::turnaround);
        addStatistics(values, late, TaskResult::tardiness);
        values.addAll(List.of(metrics.cpuUtilization(), metrics.ramUtilization(), metrics.vmRamUtilization(),
            result.vms().size(), result.peakVms(), result.reusedTasks(), result.vmCreationFailures(), result.peakQueue(),
            metrics.avgWatts(), metrics.windowKwh(), metrics.drainKwh(), config.windowSeconds(), result.lastTerminal(),
            result.serviceEnd(), result.simulationEnd()));
        final var header = new StringBuilder("input,input_sha256,tasks,completed,on_time,late,other,completion_rate,deadline_miss_rate,"
            + "sla_failure_rate,total_mi,offered_load,mean_wait_s,p95_wait_s,mean_execution_s,p95_execution_s,"
            + "mean_finish_s,p95_finish_s,mean_turnaround_s,p95_turnaround_s,mean_tardiness_s,p95_tardiness_s,"
            + "window_cpu_utilization,window_ram_utilization,window_vm_ram_utilization,vms_created,peak_vms,"
            + "reused_tasks,vm_creation_failures,peak_queued,mean_it_watts,window_it_kwh,drain_it_kwh,"
            + "window_end_s,last_terminal_s,service_end_s,simulation_end_s");
        config.specs().stream().sorted((a, b) -> a.id().compareTo(b.id())).forEach(spec -> {
            header.append(',').append(spec.id().toLowerCase(Locale.ROOT)).append("_count");
            values.add(result.vms().stream().filter(vm -> vm.spec().equals(spec)).count());
        });
        try (var writer = Files.newBufferedWriter(directory.resolve("summary.csv"))) {
            writer.write(header + "\n");
            row(writer, values.toArray());
        }
    }

    private static void addStatistics(final List<Object> values, final List<TaskResult> tasks,
                                      final ToDoubleFunction<TaskResult> field) {
        final double[] ordered = tasks.stream().mapToDouble(field).sorted().toArray();
        values.add(Arrays.stream(ordered).average().orElse(0));
        values.add(ordered.length == 0 ? 0 : ordered[(int) Math.ceil(ordered.length * 0.95) - 1]);
    }

    private static double divide(final int numerator, final int denominator) {
        return denominator == 0 ? Double.NaN : (double) numerator / denominator;
    }

    private static Long identifier(final long id) {
        return id < 0 ? null : id;
    }

    private static void row(final BufferedWriter writer, final Object... fields) throws IOException {
        final var line = new StringBuilder();
        for (int index = 0; index < fields.length; index++) {
            if (index > 0) {
                line.append(',');
            }
            final Object value = fields[index];
            if (value != null && !(value instanceof Double number && !Double.isFinite(number))) {
                final String text = value.toString();
                if (text.contains(",") || text.contains("\"") || text.contains("\n") || text.contains("\r")) {
                    line.append('"').append(text.replace("\"", "\"\"")).append('"');
                } else {
                    line.append(text);
                }
            }
        }
        writer.write(line.append('\n').toString());
    }

    private static String checksum(final Path input) throws IOException {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(input)));
        } catch (final NoSuchAlgorithmException error) {
            throw new IllegalStateException(error);
        }
    }
}

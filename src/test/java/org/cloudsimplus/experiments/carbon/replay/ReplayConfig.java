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

import org.cloudsimplus.experiments.carbon.synthetic.SyntheticCpuTask;
import org.cloudsimplus.hosts.Host;
import org.cloudsimplus.resources.CoreCapacity;
import org.cloudsimplus.resources.Processor;
import org.cloudsimplus.schedulers.cloudlet.CloudletSchedulerSpaceShared;
import org.cloudsimplus.vms.Vm;
import org.cloudsimplus.vms.VmSimple;

import java.io.BufferedReader;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Properties;

/// Fixed VM catalogue and lifecycle settings shared by all replay levels.
public record ReplayConfig(List<VmSpec> specs, double startupSeconds, double shutdownSeconds,
                           double idleSeconds, double sampleSeconds, double minEventSeconds, double windowSeconds) {
    private static final Comparator<VmSpec> SPEC_RANK = Comparator.comparingInt(VmSpec::cores)
        .thenComparingLong(VmSpec::ramMb);
    private static final double SUBMISSION_WINDOW = 86_400;
    private static final String TASK_HEADER = "task_id,task_type,arrival_time_s,required_cores,required_ram_mb,"
        + "length_mi_per_core,reference_runtime_s,deadline_s";

    public ReplayConfig {
        specs = specs.stream().sorted(SPEC_RANK).toList();
        if (specs.isEmpty() || specs.stream().map(VmSpec::id).distinct().count() != specs.size()) {
            throw new IllegalArgumentException("VM specifications must have unique names");
        }
        for (final double value : new double[]{startupSeconds, shutdownSeconds, idleSeconds,
                                                 sampleSeconds, minEventSeconds, windowSeconds}) {
            if (!Double.isFinite(value) || value < 0) {
                throw new IllegalArgumentException("Replay timings must be finite and nonnegative");
            }
        }
        if (minEventSeconds <= 0 || sampleSeconds < minEventSeconds || windowSeconds <= 0) {
            throw new IllegalArgumentException("Expected a positive window and a sampling interval of at least one event step");
        }
    }

    public record VmSpec(String id, int cores, long ramMb) {
        public VmSpec {
            if (!id.matches("[A-Za-z0-9_-]+") || cores <= 0 || ramMb <= 0) {
                throw new IllegalArgumentException("Invalid VM spec: " + id);
            }
        }

        boolean fits(final SyntheticCpuTask task) {
            return cores >= task.requiredCores() && ramMb >= task.requiredRamMb();
        }

        Vm instantiate(final Host host) {
            final var capacity = new CoreCapacity(cores, host.getProcessor().getWorkingCoreMips());
            final var vm = VmSimple.of(capacity, new CloudletSchedulerSpaceShared());
            return vm.setRam(ramMb).setBw(1).setSize(1);
        }
    }

    public static ReplayConfig read(final Path directory) throws IOException {
        final var properties = new Properties();
        try (var input = Files.newBufferedReader(directory.resolve("baseline.properties"))) {
            properties.load(input);
        }
        if (!"first-fit-fcfs".equals(properties.getProperty("task_policy"))
            || !"first-fit-original-order".equals(properties.getProperty("host_policy"))) {
            throw new IllegalArgumentException("Unsupported baseline dispatch or placement policy");
        }

        final List<VmSpec> specs = new ArrayList<>();
        try (var reader = Files.newBufferedReader(directory.resolve("vm-specs.csv"))) {
            expectHeader(reader, "vm_type,cores,ram_mb");
            String line;
            while ((line = reader.readLine()) != null) {
                final String[] fields = split(line, 3);
                specs.add(new VmSpec(fields[0], Integer.parseInt(fields[1]), Long.parseLong(fields[2])));
            }
        }
        return new ReplayConfig(specs, seconds(properties, "startup_s"), seconds(properties, "shutdown_s"),
            seconds(properties, "idle_timeout_s"), seconds(properties, "sample_s"),
            seconds(properties, "min_event_s"), SUBMISSION_WINDOW);
    }

    public static List<SyntheticCpuTask> readTasks(final Path path, final double windowSeconds) throws IOException {
        final List<SyntheticCpuTask> tasks = new ArrayList<>();
        final var ids = new HashSet<Long>();
        try (var reader = Files.newBufferedReader(path)) {
            expectHeader(reader, TASK_HEADER);
            String line;
            while ((line = reader.readLine()) != null) {
                final String[] fields = split(line, 8);
                final var task = new SyntheticCpuTask(Long.parseLong(fields[0]), Double.parseDouble(fields[2]),
                    Integer.parseInt(fields[3]), Long.parseLong(fields[4]), Long.parseLong(fields[5]),
                    Long.parseLong(fields[6]), Double.parseDouble(fields[7]), fields[1]);
                if (task.arrivalTime() >= windowSeconds || !ids.add(task.taskId()) || !task.taskType().matches("[A-Za-z0-9_-]+")) {
                    throw new IllegalArgumentException("Invalid or duplicate workload record " + task.taskId());
                }
                tasks.add(task);
            }
        }
        return tasks;
    }

    public void validateInventory(final List<SyntheticCpuTask> tasks, final List<Host> hosts) {
        if (hosts.isEmpty() || hosts.stream().anyMatch(host -> !host.isActive() || !host.getVmList().isEmpty())) {
            throw new IllegalArgumentException("Replay requires an initially active, empty Host inventory");
        }
        for (final VmSpec spec : specs) {
            if (hosts.stream().noneMatch(host -> canPlace(spec, host))) {
                throw new IllegalArgumentException("No Host fits VM specification " + spec.id());
            }
        }
        final var ids = new HashSet<Long>();
        for (final var task : tasks) {
            if (task.arrivalTime() >= windowSeconds || !ids.add(task.taskId())) {
                throw new IllegalArgumentException("Invalid arrival or duplicate task " + task.taskId());
            }
            if (specs.stream().noneMatch(spec -> spec.fits(task))) {
                throw new IllegalArgumentException("No VM specification fits task " + task.taskId());
            }
        }
    }

    static boolean canPlace(final VmSpec spec, final Host host) {
        return hasFreeResources(spec, host) && host.isSuitableForVm(spec.instantiate(host));
    }

    static boolean hasFreeResources(final VmSpec spec, final Host host) {
        return host.isActive() && Processor.freeCores(host) >= spec.cores()
            && host.getRam().getFree() >= spec.ramMb();
    }

    private static double seconds(final Properties properties, final String key) {
        final String value = properties.getProperty(key);
        if (value == null) {
            throw new IllegalArgumentException("Missing timing parameter " + key);
        }
        return Double.parseDouble(value);
    }

    private static void expectHeader(final BufferedReader reader, final String expected) throws IOException {
        if (!expected.equals(reader.readLine())) {
            throw new IllegalArgumentException("Expected CSV header " + expected);
        }
    }

    private static String[] split(final String line, final int expected) {
        final String[] fields = line.split(",", -1);
        if (fields.length != expected) {
            throw new IllegalArgumentException("Invalid CSV row " + line);
        }
        return fields;
    }
}

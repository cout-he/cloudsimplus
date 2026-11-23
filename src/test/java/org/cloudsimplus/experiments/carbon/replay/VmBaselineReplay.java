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

import org.cloudsimplus.core.CloudSimPlus;
import org.cloudsimplus.datacenters.DatacenterSimple;
import org.cloudsimplus.experiments.carbon.CarbonHostFactory;
import org.cloudsimplus.experiments.carbon.replay.ReplayConfig.VmSpec;
import org.cloudsimplus.experiments.carbon.synthetic.SyntheticCpuTask;
import org.cloudsimplus.hosts.Host;
import org.cloudsimplus.util.Log;
import ch.qos.logback.classic.Level;

import java.io.IOException;
import java.nio.file.Path;
import java.util.List;
import java.util.Locale;

/// Runs a complete workload on the fixed datacenter through the native simulation engine.
public final class VmBaselineReplay {
    public record TaskResult(SyntheticCpuTask task, long vmId, long hostId, double admitted, double start, double finish,
                             double execution, String status) {
        public boolean completed() { return "SUCCESS".equals(status); }
        public boolean deadlineMissed() { return completed() && finish > task.deadline(); }
        public double waiting() { return start - task.arrivalTime(); }
        public double turnaround() { return finish - task.arrivalTime(); }
        public double tardiness() { return Math.max(0, finish - task.deadline()); }
    }

    public record VmResult(long id, VmSpec spec, double mips, long hostId, double created, double ready,
                           double shutdownStarted, double destroyed, int taskCount, double busySeconds, double idleSeconds) { }

    public record Sample(double time, int arrivals, int running, int queued, int globalQueue, int activeVms,
                         double cpuUtilization, double ramUtilization, double vmRamUtilization, double watts) { }

    public record Result(List<TaskResult> tasks, List<VmResult> vms, List<Sample> samples,
                         ReplayMeter.Metrics metrics, List<ReplayMeter.HostMetrics> hosts, int peakVms,
                         long reusedTasks, long vmCreationFailures, int peakQueue, double lastTerminal,
                         double serviceEnd, double simulationEnd, double capacityMips) { }

    private VmBaselineReplay() { }

    public static Result run(final ReplayConfig config, final List<SyntheticCpuTask> tasks, final List<Host> hosts) {
        hosts.forEach(host -> host.setIdleShutdownDeadline(-1));
        final var simulation = new CloudSimPlus(config.minEventSeconds());
        final var datacenter = DatacenterSimple.of(simulation, hosts);
        config.validateInventory(tasks, datacenter.getHostList());
        datacenter.disableMigrations();
        final var broker = new ReplayBroker(simulation, datacenter, config, tasks);
        simulation.start();
        return broker.result();
    }

    public static void main(final String[] args) throws IOException {
        if (args.length != 3) {
            throw new IllegalArgumentException("Usage: VmBaselineReplay <config-directory> <workload.csv> <output-directory>");
        }

        Log.setLevel(Level.WARN);
        final var config = ReplayConfig.read(Path.of(args[0]));
        final var input = Path.of(args[1]);
        final List<SyntheticCpuTask> tasks = ReplayConfig.readTasks(input, config.windowSeconds());
        final var result = run(config, tasks, CarbonHostFactory.createHosts());
        ReplayOutput.write(Path.of(args[2]), input, config, result);
        final long completed = result.tasks().stream().filter(TaskResult::completed).count();
        final long late = result.tasks().stream().filter(TaskResult::deadlineMissed).count();
        System.out.printf(Locale.ROOT,
            "%s: %,d/%,d completed, %,d late, %,d VMs, peak %,d queued. IT energy %.6f kWh + %.6f kWh drain. "
                + "Service end %.6f s, simulator end %.6f s.%n",
            input.getFileName(), completed, tasks.size(), late, result.vms().size(), result.peakQueue(),
            result.metrics().windowKwh(), result.metrics().drainKwh(), result.serviceEnd(), result.simulationEnd());
        if (completed < tasks.size()) {
            System.err.printf(Locale.ROOT, "%,d other final states recorded in tasks.csv.%n", tasks.size() - completed);
        }
    }
}

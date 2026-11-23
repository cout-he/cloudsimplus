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
package org.cloudsimplus.experiments.carbon.synthetic;

import org.cloudsimplus.experiments.carbon.synthetic.SyntheticCpuWorkloadConfig.RuntimeRange;
import org.cloudsimplus.experiments.carbon.synthetic.SyntheticCpuWorkloadConfig.TaskType;
import org.cloudsimplus.hosts.Host;
import org.cloudsimplus.resources.CoreCapacity;
import org.cloudsimplus.resources.Processor;
import org.cloudsimplus.schedulers.cloudlet.CloudletSchedulerSpaceShared;
import org.cloudsimplus.vms.Vm;
import org.cloudsimplus.vms.VmSimple;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.SplittableRandom;

import static org.cloudsimplus.experiments.carbon.synthetic.SyntheticCpuWorkloadConfig.DAY_SECONDS;

/// Generates and calibrates a marked, piecewise-constant Poisson arrival process.
public final class SyntheticCpuWorkloadGenerator {
    public record VmShape(int cores, long ramMb, double mipsPerCore) {
        public boolean fits(final SyntheticCpuTask task) {
            return fits(task.requiredCores(), task.requiredRamMb());
        }

        private boolean fits(final int requiredCores, final long requiredRam) {
            return requiredCores <= cores && requiredRam <= ramMb;
        }

        Vm createVm(final int requiredCores, final long requiredRam) {
            if (!fits(requiredCores, requiredRam)) {
                throw new IllegalArgumentException("Task exceeds VM shape capacity");
            }

            final var vm = VmSimple.of(new CoreCapacity(requiredCores, mipsPerCore), new CloudletSchedulerSpaceShared());
            return vm.setRam(requiredRam).setBw(1).setSize(1);
        }

        static VmShape fromHost(final Host host) {
            return new VmShape(Math.toIntExact(Processor.cores(host)), host.getRam().getCapacity(), Processor.mips(host));
        }
    }

    public record RateWindow(int startSeconds, String taskType, double ratePerSecond, int arrivals) { }

    public record Scenario(
        double targetLoad, long seed, int attempts, double meanArrivalRate, long totalMi, double offeredLoad,
        List<SyntheticCpuTask> tasks, List<RateWindow> rateWindows)
    {
        public Scenario {
            tasks = List.copyOf(tasks);
            rateWindows = List.copyOf(rateWindows);
        }
    }

    private record TaskDraft(double arrivalTime, int cores, long ramMb, long lengthMi, long runtime,
                             double deadline, String type) {
        private SyntheticCpuTask withId(final long id) {
            return new SyntheticCpuTask(id, arrivalTime, cores, ramMb, lengthMi, runtime, deadline, type);
        }
    }

    private record Draw(List<TaskDraft> tasks, List<RateWindow> windows, long totalMi) { }

    private final SyntheticCpuWorkloadConfig config;
    private final double capacityMips;
    private final List<VmShape> vmShapes;
    private final double meanMiPerTask;

    /// The empty hosts are the physical inventory used to determine capacity and VM fit.
    public SyntheticCpuWorkloadGenerator(final SyntheticCpuWorkloadConfig config, final List<Host> hosts) {
        this.config = config;
        capacityMips = hosts.stream().mapToDouble(Processor::totalMips).sum();
        if (!(capacityMips > 0) || !Double.isFinite(capacityMips)) {
            throw new IllegalArgumentException("Expected a finite, positive CPU inventory");
        }

        vmShapes = verifyVmShapes(hosts);
        meanMiPerTask = config.types().stream().mapToDouble(type -> type.share() * type.meanMi(config.referenceMips())).sum();
        config.types().forEach(this::validateTaskType);
    }

    public double capacityMips() {
        return capacityMips;
    }

    public List<VmShape> vmShapes() {
        return vmShapes;
    }

    public Scenario generate(final double targetLoad, final long seed) {
        if (!(targetLoad > 0 && targetLoad <= 1)) {
            throw new IllegalArgumentException("Target load must be in (0, 1]");
        }

        final double availableMi = capacityMips * DAY_SECONDS;
        double meanRate = targetLoad * capacityMips / meanMiPerTask;
        double lowerRate = 0;
        double upperRate = Double.POSITIVE_INFINITY;
        double offeredLoad = 0;
        for (int attempt = 1; attempt <= config.maxAttempts(); attempt++) {
            final var draw = generateAtRate(meanRate, seed);
            offeredLoad = draw.totalMi() / availableMi;
            if (Math.abs(offeredLoad - targetLoad) <= config.tolerance()) {
                draw.tasks().sort(Comparator.comparingDouble(TaskDraft::arrivalTime));
                final List<SyntheticCpuTask> tasks = new ArrayList<>(draw.tasks().size());
                for (final var draft : draw.tasks()) {
                    tasks.add(draft.withId(tasks.size()));
                }
                return new Scenario(targetLoad, seed, attempt, meanRate, draw.totalMi(), offeredLoad,
                    tasks, draw.windows());
            }

            if (offeredLoad < targetLoad) {
                lowerRate = meanRate;
                meanRate = Double.isFinite(upperRate) ? (lowerRate + upperRate) / 2 : lowerRate * 2;
            } else {
                upperRate = meanRate;
                meanRate = (lowerRate + upperRate) / 2;
            }
        }

        throw new IllegalStateException("Offered load " + offeredLoad + " did not reach target " + targetLoad +
            " within tolerance " + config.tolerance() + " for seed " + seed);
    }

    private Draw generateAtRate(final double meanRate, final long seed) {
        final var seedSource = new SplittableRandom(seed);
        final List<TaskDraft> tasks = new ArrayList<>();
        final List<RateWindow> windows = new ArrayList<>();
        long totalMi = 0;
        for (final var type : config.types()) {
            for (int start = 0; start < DAY_SECONDS; start += config.windowSeconds()) {
                // Keep each window's streams stable across rate calibration attempts.
                final var arrivalRandom = seedSource.split();
                final var markRandom = seedSource.split();
                final double rate = windowRate(type, start, meanRate, seedSource.nextGaussian());
                final int beforeWindow = tasks.size();
                long windowMi = 0;
                for (double hazard = exponentialGap(arrivalRandom); hazard < rate * config.windowSeconds();
                     hazard += exponentialGap(arrivalRandom))
                {
                    final double time = start + hazard / rate;
                    final var task = sampleTask(type, time, markRandom);
                    tasks.add(task);
                    windowMi = Math.addExact(windowMi, Math.multiplyExact(task.lengthMi(), task.cores()));
                }
                totalMi = Math.addExact(totalMi, windowMi);
                windows.add(new RateWindow(start, type.name(), rate, tasks.size() - beforeWindow));
            }
        }
        return new Draw(tasks, windows, totalMi);
    }

    private double windowRate(final TaskType type, final int start, final double meanRate, final double noise) {
        final var pattern = type.arrival();
        final double midpointHour = (start + config.windowSeconds() / 2.0) / 3600;
        final double angle = 2 * Math.PI * (midpointHour - pattern.peakHour()) / pattern.periodHours();
        final double rateFactor = 1 + pattern.amplitude() * Math.cos(angle) + pattern.sigma() * noise;
        return meanRate * type.share() * Math.max(0, rateFactor);
    }

    private TaskDraft sampleTask(final TaskType type, final double arrival, final SplittableRandom random)
    {
        final int cores = sampleCores(type, random.nextDouble());
        final int ramStep = config.ramStepMb();
        final long ramUnitsPerCore = random.nextLong(type.minRamPerCore() / ramStep, type.maxRamPerCore() / ramStep + 1L);
        final long ramMb = ramUnitsPerCore * ramStep * cores;
        final long runtime = sampleRuntime(type.runtime(), random.nextDouble());
        final long length = Math.multiplyExact(runtime, config.referenceMips());
        final double deadline = config.deadline().absoluteDeadline(arrival, runtime);
        return new TaskDraft(arrival, cores, ramMb, length, runtime, deadline, type.name());
    }

    private static int sampleCores(final TaskType type, final double draw) {
        double cumulativeProbability = 0;
        for (final var option : type.coreOptions()) {
            cumulativeProbability += option.probability();
            if (draw < cumulativeProbability) {
                return option.cores();
            }
        }
        return type.coreOptions().getLast().cores();
    }

    private static long sampleRuntime(final RuntimeRange runtime, final double draw) {
        final double min = runtime.minimum();
        final double max = runtime.maximum();
        final double span = max - min;
        if (span == 0) {
            return runtime.minimum();
        }

        final double mode = runtime.mode();
        final double sample = draw < (mode - min) / span
            ? min + Math.sqrt(draw * span * (mode - min))
            : max - Math.sqrt((1 - draw) * span * (max - mode));
        return Math.round(sample);
    }

    private static double exponentialGap(final SplittableRandom random) {
        return -Math.log1p(-random.nextDouble());
    }

    private static List<VmShape> verifyVmShapes(final List<Host> hosts) {
        final var shapes = new LinkedHashSet<VmShape>();
        for (final Host host : hosts) {
            final var shape = VmShape.fromHost(host);
            if (!shapes.contains(shape) && host.isSuitableForVm(shape.createVm(shape.cores(), shape.ramMb()))) {
                shapes.add(shape);
            }
        }
        return List.copyOf(shapes);
    }

    private void validateTaskType(final TaskType type) {
        final int maxCores = type.coreOptions().stream().mapToInt(SyntheticCpuWorkloadConfig.CoreOption::cores).max().orElseThrow();
        final long maxRam = (long) maxCores * type.maxRamPerCore();
        if (vmShapes.stream().noneMatch(shape -> shape.fits(maxCores, maxRam))) {
            throw new IllegalArgumentException("No creatable VM fits all " + type.name() + " tasks");
        }
    }
}

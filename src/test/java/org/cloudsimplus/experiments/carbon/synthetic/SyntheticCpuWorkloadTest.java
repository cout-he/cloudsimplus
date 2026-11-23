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

import org.cloudsimplus.cloudlets.Cloudlet;
import org.cloudsimplus.experiments.carbon.CarbonHostFactory;
import org.cloudsimplus.experiments.carbon.synthetic.SyntheticCpuWorkloadConfig.ArrivalPattern;
import org.cloudsimplus.experiments.carbon.synthetic.SyntheticCpuWorkloadConfig.DeadlineRule;
import org.cloudsimplus.experiments.carbon.synthetic.SyntheticCpuWorkloadConfig.TaskType;
import org.cloudsimplus.resources.Processor;
import org.junit.jupiter.api.Test;

import java.nio.file.Path;
import java.util.HashMap;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class SyntheticCpuWorkloadTest {
    private static final Path CONFIG = Path.of("research/carbonaware/synthetic-v1/workload.properties");

    @Test
    void calibratesAllThreeLevelsAndPreservesCpuRamAndDeadlineConstraints() throws Exception {
        final var config = SyntheticCpuWorkloadConfig.read(CONFIG);
        final var generator = new SyntheticCpuWorkloadGenerator(config, CarbonHostFactory.createHosts());
        assertEquals(6_789_984, generator.capacityMips(), 0);
        assertEquals(2, generator.vmShapes().size());

        final var taskCounts = new HashMap<String, Integer>();
        long priorTaskCount = 0;
        double priorRate = 0;
        for (int index = 0; index < config.targets().size(); index++) {
            final double target = config.targets().get(index);
            final var scenario = generator.generate(target, 42 + index);
            final long sumMi = scenario.tasks().stream().mapToLong(SyntheticCpuTask::totalMi).sum();
            assertEquals(target, sumMi / (generator.capacityMips() * 86_400), config.tolerance());
            assertEquals(sumMi, scenario.totalMi());
            assertTrue(priorTaskCount < scenario.tasks().size());
            assertTrue(priorRate < scenario.meanArrivalRate());
            assertTrue(scenario.tasks().stream().anyMatch(task -> task.requiredRamMb() > 65_536));

            taskCounts.clear();
            long expectedId = 0;
            for (final var task : scenario.tasks()) {
                final var type = config.types().stream().filter(item -> item.name().equals(task.taskType())).findFirst().orElseThrow();
                assertTrue(type.coreOptions().stream().anyMatch(option -> option.cores() == task.requiredCores()));
                final long ramPerCore = task.requiredRamMb() / task.requiredCores();
                assertEquals(0, task.requiredRamMb() % task.requiredCores());
                assertEquals(0, ramPerCore % config.ramStepMb());
                assertTrue(ramPerCore >= type.minRamPerCore() && ramPerCore <= type.maxRamPerCore());
                assertEquals(task.referenceRuntime() * config.referenceMips(), task.lengthMiPerCore());
                assertTrue(task.arrivalTime() >= 0 && task.arrivalTime() < 86_400);
                assertEquals(expectedId++, task.taskId());
                assertEquals(task.arrivalTime() + 2 * task.referenceRuntime(), task.deadline());
                assertTrue(task.deadline() > task.arrivalTime());
                assertTrue(generator.vmShapes().stream().anyMatch(shape ->
                    shape.fits(task) && task.arrivalTime() + task.executionSeconds(shape.mipsPerCore()) <= task.deadline()));
                taskCounts.merge(task.taskType(), 1, Integer::sum);
            }
            for (final var type : config.types()) {
                assertEquals(type.share(), taskCounts.get(type.name()) / (double) scenario.tasks().size(), 0.03);
            }
            priorTaskCount = scenario.tasks().size();
            priorRate = scenario.meanArrivalRate();
        }
    }

    @Test
    void appliesAbsoluteDeadlinesWithoutChangingTaskAttributesOrTruncatingTheDay() throws Exception {
        final var config = SyntheticCpuWorkloadConfig.read(CONFIG);
        assertEquals(7200, config.deadline().absoluteDeadline(3600, 1800));
        final var generator = new SyntheticCpuWorkloadGenerator(config, CarbonHostFactory.createHosts());
        final var baseline = generator.generate(0.30, 42);
        assertTrue(baseline.tasks().stream().anyMatch(task -> task.deadline() > 86_400));

        for (final double alpha : config.sensitivityAlphas()) {
            final var variantRule = new DeadlineRule(alpha, 0);
            final var modifiedConfig = new SyntheticCpuWorkloadConfig(config.referenceMips(), config.windowSeconds(),
                config.seed(), config.targets(), config.tolerance(), config.maxAttempts(), config.ramStepMb(),
                variantRule, config.sensitivityAlphas(), config.types());
            final var variantGenerator = new SyntheticCpuWorkloadGenerator(modifiedConfig, CarbonHostFactory.createHosts());
            final var variant = variantGenerator.generate(0.30, 42);
            assertEquals(baseline.totalMi(), variant.totalMi());
            assertEquals(baseline.rateWindows(), variant.rateWindows());
            assertEquals(baseline.tasks().stream().map(task -> task.withDeadline(variantRule)).toList(), variant.tasks());
            assertEquals(baseline.tasks(), variant.tasks().stream().map(task -> task.withDeadline(config.deadline())).toList());
        }

        final var baseGrace = new DeadlineRule(1.5, 120);
        assertEquals(6420, baseGrace.absoluteDeadline(3600, 1800));
        final var task = new SyntheticCpuTask(4, 85_000, 4, 8192, 3_600_000, 1800, 88_600, "medium");
        assertEquals(92_200, task.withDeadline(new DeadlineRule(4, 0)).deadline());
        assertTrue(task.withDeadline(new DeadlineRule(0.1, 0)).deadline() < task.arrivalTime() + task.executionSeconds(3091));
        assertThrows(IllegalArgumentException.class, () -> new DeadlineRule(0, 0));
    }

    @Test
    void repeatsTheSameSeedAndUsesIndependentArrivalAndTaskDrawsForOtherSeeds() throws Exception {
        final var generator = new SyntheticCpuWorkloadGenerator(
            SyntheticCpuWorkloadConfig.read(CONFIG), CarbonHostFactory.createHosts());
        final var baseline = generator.generate(0.30, 42);
        assertEquals(baseline, generator.generate(0.30, 42));
        final var independent = generator.generate(0.30, 43);
        assertNotEquals(baseline.tasks().getFirst().arrivalTime(), independent.tasks().getFirst().arrivalTime());
        assertNotEquals(baseline.tasks(), independent.tasks());
        assertTrue(baseline.rateWindows().stream().allMatch(window -> window.ratePerSecond() >= 0));
        assertTrue(baseline.rateWindows().stream().filter(window -> window.taskType().equals("short"))
            .map(SyntheticCpuWorkloadGenerator.RateWindow::ratePerSecond).distinct().count() > 1);
    }

    @Test
    void runsAMulticoreCloudletWithPerCoreLengthOnBothHostTypes() {
        final var task = new SyntheticCpuTask(1, 12.5, 4, 8192, 120_000, 60, 150, "medium");
        final var cloudlet = task.toCloudlet();
        assertEquals(4, cloudlet.getRequiredCores());
        assertEquals(120_000, cloudlet.getLength());
        assertEquals(480_000, cloudlet.getTotalLength());
        assertEquals(12.5, cloudlet.getSubmissionDelay());

        for (final double mips : new double[]{2000, 3091}) {
            final var execution = SyntheticCpuWorkloadExample.replay(task, mips);
            assertEquals(Cloudlet.Status.SUCCESS, execution.status());
            assertEquals(mips, Processor.mips(execution.host()));
            assertTrue(execution.startTime() >= task.arrivalTime());
            assertEquals(120_000 / mips, execution.executionTime(), 0.03);
        }
    }

    @Test
    void clipsNegativeRatesAndGeneratesNoArrivalsInZeroRateWindows() throws Exception {
        final var config = SyntheticCpuWorkloadConfig.read(CONFIG);
        final var longType = config.types().stream().filter(type -> type.name().equals("long")).findFirst().orElseThrow();
        final var exaggeratedPattern = new ArrivalPattern(2, 24, 12, 0);
        final var type = new TaskType(longType.name(), 1, longType.coreOptions(), longType.minRamPerCore(),
            longType.maxRamPerCore(), longType.runtime(), exaggeratedPattern);
        final var modified = new SyntheticCpuWorkloadConfig(config.referenceMips(), config.windowSeconds(),
            config.seed(), config.targets(), config.tolerance(), config.maxAttempts(), config.ramStepMb(), config.deadline(),
            config.sensitivityAlphas(), List.of(type));
        final var generator = new SyntheticCpuWorkloadGenerator(modified, CarbonHostFactory.createHosts());
        final var scenario = generator.generate(0.30, 42);
        assertTrue(scenario.rateWindows().stream().anyMatch(window -> window.ratePerSecond() == 0));
        assertTrue(scenario.rateWindows().stream().allMatch(window -> window.ratePerSecond() >= 0));
        assertTrue(scenario.rateWindows().stream().filter(window -> window.ratePerSecond() == 0)
            .allMatch(window -> window.arrivals() == 0));
    }

    @Test
    void reservesAnAClassVmForAMemoryRequestOutsideTheBClassEnvelope() {
        final var task = new SyntheticCpuTask(1, 10, 8, 81_920, 1_200_000, 600, 1210, "memory");
        assertThrows(IllegalArgumentException.class, () -> SyntheticCpuWorkloadExample.replay(task, 2000));
        final var execution = SyntheticCpuWorkloadExample.replay(task, 3091);
        assertEquals(Cloudlet.Status.SUCCESS, execution.status());
        assertEquals(1_200_000.0 / 3091, execution.executionTime(), 0.03);
        assertTrue(execution.finishTime() < task.deadline());
    }
}

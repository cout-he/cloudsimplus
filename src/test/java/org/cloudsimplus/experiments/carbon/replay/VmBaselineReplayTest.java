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

import org.cloudsimplus.experiments.carbon.CarbonHostFactory;
import org.cloudsimplus.experiments.carbon.synthetic.SyntheticCpuTask;
import org.cloudsimplus.core.CloudSimPlus;
import org.cloudsimplus.hosts.Host;
import org.cloudsimplus.resources.Processor;
import org.junit.jupiter.api.Test;

import java.nio.file.Path;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class VmBaselineReplayTest {
    private static final Path CONFIG = Path.of("research/carbonaware/vm-baseline-v1.2");

    @Test
    void usesNativePerCoreSpeedAndReleasesResources() throws Exception {
        final var task = job(0, 0, 4, 8192, 60, 100);
        final var a = run(options(0, 0, 10, 120), List.of(task), List.of(aHost()));
        final var b = run(options(0, 0, 10, 120), List.of(task), List.of(bHost()));
        assertEquals(60, b.tasks().getFirst().execution(), 0.03);
        assertEquals(120_000.0 / 3091, a.tasks().getFirst().execution(), 0.03);
        assertEquals(1, a.vms().size());
        assertEquals(a.tasks().getFirst().finish() + 10, a.vms().getFirst().destroyed(), 1e-7);
        assertEquals(0, a.samples().getLast().activeVms());
        assertEquals(0, a.samples().getLast().ramUtilization(), 1e-12);
    }

    @Test
    void sharesVmOnlyWhenThereAreEnoughCoresAndRam() throws Exception {
        final var tasks = List.of(job(0, 0, 4, 4096, 15, 100), job(1, 1, 1, 6400, 60, 100),
            job(2, 16, 1, 800, 10, 100), job(3, 16.5, 1, 1000, 10, 100), job(4, 17, 1, 7600, 20, 100));
        final var result = run(options(0, 0, 30, 120), tasks, List.of(aHost()));
        assertNotEquals(result.tasks().get(0).vmId(), result.tasks().get(1).vmId());
        assertEquals(result.tasks().get(0).vmId(), result.tasks().get(2).vmId());
        assertEquals(result.tasks().get(2).vmId(), result.tasks().get(3).vmId());
        assertTrue(result.tasks().get(3).start() < result.tasks().get(2).finish());
        assertNotEquals(result.tasks().get(0).vmId(), result.tasks().get(4).vmId());
        for (final var item : result.tasks()) {
            assertEquals("SUCCESS", item.status());
        }
    }

    @Test
    void preservesFcfsUnderCpuPressureAndRetainsLateCompletions() throws Exception {
        final var tasks = List.of(job(0, 0, 16, 32768, 40, 80), job(1, 1, 1, 1024, 20, 10),
            job(2, 2, 1, 1024, 5, 100));
        final var result = run(options(0, 0, 10, 90), tasks, List.of(bHost()));
        final var first = result.tasks().get(0);
        final var delayed = result.tasks().get(1);
        final var third = result.tasks().get(2);
        assertTrue(delayed.start() >= first.finish());
        assertTrue(third.start() >= delayed.start());
        assertEquals(first.vmId(), delayed.vmId());
        assertTrue(delayed.deadlineMissed());
        assertFalse(first.deadlineMissed());
        assertEquals(3, result.tasks().size());
    }

    @Test
    void waitsForPhysicalReclamationThenCreatesTheSmallerVm() throws Exception {
        final var tasks = List.of(job(0, 0, 8, 90000, 40, 200), job(1, 1, 16, 32768, 20, 200));
        final var result = run(options(0, 7, 10, 120), tasks, List.of(aHost()));
        final var old = result.vms().getFirst();
        assertEquals("VM9", old.spec().id());
        assertEquals(result.tasks().getFirst().finish() + 17, old.destroyed(), 1e-7);
        assertTrue(result.tasks().get(1).start() >= old.destroyed());
        assertEquals("SUCCESS", result.tasks().get(1).status());
    }

    @Test
    void accountsForStartupAndResetsIdleTimeoutOnReuse() throws Exception {
        final var tasks = List.of(job(0, 0, 2, 2048, 12, 120), job(1, 20, 2, 2048, 3, 120));
        final var result = run(options(5, 3, 10, 75), tasks, List.of(bHost()));
        final var vm = result.vms().getFirst();
        assertEquals(1, result.vms().size());
        assertTrue(result.tasks().getFirst().start() >= 5);
        assertEquals(2, vm.taskCount());
        assertEquals(result.tasks().getLast().finish() + 13, vm.destroyed(), 1e-7);
        assertEquals(15, vm.busySeconds(), 0.06);
    }

    @Test
    void integratesIdleAndLoadedPowerAcrossTheWindowBoundary() throws Exception {
        final var config = options(0, 0, 5, 60);
        final List<Host> hosts = CarbonHostFactory.createHosts();
        final var idle = run(config, List.of(), hosts);
        assertEquals(7094.4 * 60 / 3_600_000, idle.metrics().windowKwh(), 1e-9);
        assertEquals(0, idle.metrics().drainKwh(), 1e-12);

        final var host = bHost();
        final double idleWatts = host.getPowerModel().getPower(0);
        final double fullWatts = host.getPowerModel().getPower(1);
        final var result = run(config, List.of(job(0, 50, 16, 32768, 20, 120)), List.of(host));
        final var task = result.tasks().getFirst();
        final double expectedWindowJ = task.start() * idleWatts + (60 - task.start()) * fullWatts;
        assertEquals(expectedWindowJ / 3_600_000, result.metrics().windowKwh(), 1e-9);
        assertEquals((task.finish() - 60) * fullWatts / 3_600_000, result.metrics().drainKwh(), 1e-9);
        assertTrue(task.finish() > 60);
    }

    @Test
    void verifiesTheWholeV11InventoryBeforeStartingAnyTask() throws Exception {
        final var cfg = ReplayConfig.read(CONFIG);
        for (final int offered : List.of(30, 60, 90)) {
            final var path = Path.of("research/carbonaware/synthetic-v1/results/workload-" + offered + ".csv");
            final var tasks = ReplayConfig.readTasks(path, cfg.windowSeconds());
            cfg.validateInventory(tasks, CarbonHostFactory.createDatacenter(new CloudSimPlus()).getHostList());
        }
        assertThrows(IllegalArgumentException.class, () ->
            cfg.validateInventory(List.of(job(0, 0, 8, 90000, 10, 120)), List.of(bHost())));
    }

    private static VmBaselineReplay.Result run(final ReplayConfig config, final List<SyntheticCpuTask> tasks,
                                               final List<Host> hosts) {
        final var fixtureSpecs = config.specs().stream()
            .filter(spec -> hosts.stream().anyMatch(host -> Processor.cores(host) >= spec.cores()
                && host.getRam().getCapacity() >= spec.ramMb())).toList();
        final var fixture = new ReplayConfig(fixtureSpecs, config.startupSeconds(), config.shutdownSeconds(),
            config.idleSeconds(), config.sampleSeconds(), config.minEventSeconds(), config.windowSeconds());
        return VmBaselineReplay.run(fixture, tasks, hosts);
    }

    private static ReplayConfig options(final double startup, final double shutdown, final double idle,
                                        final double window) throws Exception {
        return new ReplayConfig(ReplayConfig.read(CONFIG).specs(), startup, shutdown, idle, 1, 0.001, window);
    }

    private static Host aHost() {
        return CarbonHostFactory.createHosts().getFirst();
    }

    private static Host bHost() {
        return CarbonHostFactory.createHosts().getLast();
    }

    private static SyntheticCpuTask job(final long id, final double arrival, final int cores, final long ram,
                                        final long runtime, final double deadline) {
        return new SyntheticCpuTask(id, arrival, cores, ram, runtime * 2000, runtime, deadline, "test");
    }
}

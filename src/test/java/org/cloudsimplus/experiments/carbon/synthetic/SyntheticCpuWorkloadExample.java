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

import org.cloudsimplus.brokers.DatacenterBrokerSimple;
import org.cloudsimplus.cloudlets.Cloudlet;
import org.cloudsimplus.core.CloudSimPlus;
import org.cloudsimplus.experiments.carbon.CarbonHostFactory;
import org.cloudsimplus.hosts.Host;
import org.cloudsimplus.resources.Processor;

import java.io.IOException;
import java.nio.file.Path;
import java.util.Locale;
import java.util.Optional;

/// Replays one multi-core task on representative A and B hosts to verify the CPU time model.
public final class SyntheticCpuWorkloadExample {
    public record Execution(Host host, Cloudlet.Status status, double startTime, double executionTime, double finishTime) { }

    private SyntheticCpuWorkloadExample() { }

    public static Execution replay(final SyntheticCpuTask task, final double hostMips) {
        final var simulation = new CloudSimPlus(0.001);
        final var datacenter = CarbonHostFactory.createDatacenter(simulation);
        final Host host = datacenter.getHostList().stream()
            .filter(item -> Processor.mips(item) == hostMips && SyntheticCpuWorkloadGenerator.VmShape.fromHost(item).fits(task))
            .findFirst().orElseThrow(() -> new IllegalArgumentException("No host of the requested speed fits task " + task.taskId()));

        final var shape = SyntheticCpuWorkloadGenerator.VmShape.fromHost(host);
        final var vm = shape.createVm(task.requiredCores(), task.requiredRamMb());
        final var cloudlet = task.toCloudlet();
        if (!host.isSuitableForVm(vm) || !task.fitsVm(vm) || task.latestStartTime(vm) < task.arrivalTime()) {
            throw new IllegalArgumentException("The reference VM cannot run task " + task.taskId() + " within its budget");
        }

        datacenter.getVmPlacementPolicy().setFindHostForVmFunction((policy, candidate) -> Optional.of(host));
        final var broker = new DatacenterBrokerSimple(simulation);
        broker.submitVm(vm);
        broker.submitCloudlet(cloudlet);
        if (!broker.bindCloudletToVm(cloudlet, vm)) {
            throw new IllegalStateException("Cloudlet binding failed");
        }

        simulation.start();
        return new Execution(host, cloudlet.getStatus(), cloudlet.getStartTime(),
            cloudlet.getTotalExecutionTime(), cloudlet.getFinishTime());
    }

    public static void main(final String[] args) throws IOException {
        if (args.length != 1) {
            throw new IllegalArgumentException("Usage: SyntheticCpuWorkloadExample <config.properties>");
        }

        final var config = SyntheticCpuWorkloadConfig.read(Path.of(args[0]));
        final var generator = new SyntheticCpuWorkloadGenerator(config, CarbonHostFactory.createHosts());
        final var task = generator.generate(config.targets().getFirst(), config.seed()).tasks().stream()
            .filter(item -> item.requiredCores() > 1 && generator.vmShapes().stream().allMatch(shape -> shape.fits(item)))
            .findFirst().orElseThrow();
        for (final var shape : generator.vmShapes()) {
            final var result = replay(task, shape.mipsPerCore());
            if (result.status() != Cloudlet.Status.SUCCESS || result.finishTime() > task.deadline()) {
                throw new IllegalStateException("Reference task did not finish within its budget");
            }

            System.out.printf(Locale.ROOT,
                "Host %d | %.0f MIPS/core | %d cores | %,d MI/core | observed %.3f s | expected %.3f s%n",
                result.host().getId(), shape.mipsPerCore(), task.requiredCores(), task.lengthMiPerCore(),
                result.executionTime(), task.executionSeconds(shape.mipsPerCore()));
        }
    }
}

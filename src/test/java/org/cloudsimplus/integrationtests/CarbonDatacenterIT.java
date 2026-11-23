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
package org.cloudsimplus.integrationtests;

import org.cloudsimplus.brokers.DatacenterBrokerSimple;
import org.cloudsimplus.cloudlets.Cloudlet;
import org.cloudsimplus.cloudlets.CloudletSimple;
import org.cloudsimplus.core.CloudSimPlus;
import org.cloudsimplus.experiments.carbon.CarbonHostFactory;
import org.cloudsimplus.hosts.Host;
import org.cloudsimplus.resources.CoreCapacity;
import org.cloudsimplus.resources.Processor;
import org.cloudsimplus.schedulers.cloudlet.CloudletSchedulerSpaceShared;
import org.cloudsimplus.schedulers.vm.VmSchedulerSpaceShared;
import org.cloudsimplus.utilizationmodels.UtilizationModelFull;
import org.cloudsimplus.vms.VmSimple;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class CarbonDatacenterIT {
    private static final double WATT_TOLERANCE = 1.0e-6;
    // The datacenter adds 10 ms to the simulation's minimum event spacing.
    private static final double TIME_TOLERANCE_SECONDS = 0.02;
    private static final int JOB_LENGTH_MI = 20_000;

    @Test
    void createsTheFixedCapacityDatacenter() {
        final var datacenter = CarbonHostFactory.createDatacenter(new CloudSimPlus());
        final List<Host> hosts = datacenter.getHostList();

        assertEquals(112, hosts.size());
        assertEquals(76, hosts.stream().filter(host -> matches(host, 24, 3091, 98_304, 10_000, 1_000_000)).count());
        assertEquals(36, hosts.stream().filter(host -> matches(host, 16, 2000, 65_536, 1_000, 480_000)).count());
        assertEquals(2400, hosts.stream().mapToLong(Processor::cores).sum());
        assertEquals(9_830_400, hosts.stream().mapToLong(host -> host.getRam().getCapacity()).sum());
        assertEquals(6_789_984, hosts.stream().mapToDouble(Processor::totalMips).sum(), WATT_TOLERANCE);
        assertTrue(hosts.stream().allMatch(Host::isActive));
        assertTrue(hosts.stream().allMatch(host -> host.getVmScheduler() instanceof VmSchedulerSpaceShared));
    }

    @Test
    void measuresIdleAndInterpolatesThePublishedPowerCurves() {
        final var datacenter = CarbonHostFactory.createDatacenter(new CloudSimPlus());
        final List<Host> hosts = datacenter.getHostList();
        assertEquals(7094.4, datacenter.getPowerModel().getPowerMeasurement().getTotalPower(), WATT_TOLERANCE);
        assertEquals(11_575.6, clusterPowerAt(hosts, 0.45), WATT_TOLERANCE);
        assertEquals(11_832, clusterPowerAt(hosts, 0.5), WATT_TOLERANCE);
        assertEquals(16_412, clusterPowerAt(hosts, 1), WATT_TOLERANCE);

        final var host = hosts.getFirst();
        final var model = host.getPowerModel();
        host.setActive(false);
        assertEquals(0, model.getPowerMeasurement().getTotalPower(), WATT_TOLERANCE);
        assertEquals(7094.4 - 68.1, datacenter.getPowerModel().getPowerMeasurement().getTotalPower(), WATT_TOLERANCE);
    }

    @Test
    void sameCloudletRunsFasterOnTheSupermicroHost() {
        final var simulation = new CloudSimPlus(0.001);
        final var datacenter = CarbonHostFactory.createDatacenter(simulation);
        final List<Host> hosts = datacenter.getHostList();
        final var supermicro = hosts.getFirst();
        final var lenovo = hosts.getLast();
        final var fastVm = VmSimple.of(new CoreCapacity(Processor.mips(supermicro)), new CloudletSchedulerSpaceShared());
        final var slowVm = VmSimple.of(new CoreCapacity(Processor.mips(lenovo)), new CloudletSchedulerSpaceShared());
        datacenter.getVmPlacementPolicy().setFindHostForVmFunction(
            (policy, vm) -> Optional.of(vm == fastVm ? supermicro : lenovo));

        final var fastJob = referenceCloudlet();
        final var slowJob = referenceCloudlet();
        final var broker = new DatacenterBrokerSimple(simulation);
        fastVm.listeners().addOnHostPlacement(event -> assertSame(supermicro, event.getHost()));
        slowVm.listeners().addOnHostPlacement(event -> assertSame(lenovo, event.getHost()));
        broker.submitVmList(List.of(fastVm, slowVm));
        broker.submitCloudletList(List.of(fastJob, slowJob));
        assertTrue(broker.bindCloudletToVm(fastJob, fastVm));
        assertTrue(broker.bindCloudletToVm(slowJob, slowVm));

        simulation.start();

        assertEquals(Cloudlet.Status.SUCCESS, fastJob.getStatus());
        assertEquals(Cloudlet.Status.SUCCESS, slowJob.getStatus());
        assertEquals(JOB_LENGTH_MI / 3091.0, fastJob.getTotalExecutionTime(), TIME_TOLERANCE_SECONDS);
        assertEquals(JOB_LENGTH_MI / 2000.0, slowJob.getTotalExecutionTime(), TIME_TOLERANCE_SECONDS);
    }

    private static boolean matches(final Host host, final long cores, final double mips,
                                   final long ram, final long bandwidth, final long storage) {
        return Processor.cores(host) == cores && Processor.mips(host) == mips &&
               host.getRam().getCapacity() == ram && host.getBw().getCapacity() == bandwidth &&
               host.getStorage().getCapacity() == storage;
    }

    private static double clusterPowerAt(final List<Host> hosts, final double utilization) {
        return hosts.stream().mapToDouble(host -> host.getPowerModel().getPower(utilization)).sum();
    }

    private static Cloudlet referenceCloudlet() {
        final var cloudlet = CloudletSimple.of(JOB_LENGTH_MI, 1);
        return cloudlet.setUtilizationModelCpu(new UtilizationModelFull()).setSizes(1);
    }
}

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
import org.cloudsimplus.cloudlets.CloudletSimple;
import org.cloudsimplus.experiments.carbon.synthetic.SyntheticCpuWorkloadConfig.DeadlineRule;
import org.cloudsimplus.resources.Processor;
import org.cloudsimplus.vms.Vm;

import static org.cloudsimplus.experiments.carbon.synthetic.SyntheticCpuWorkloadConfig.REFERENCE_MIPS;

/// Task and metadata used by an experiment scheduler for admission and deadline checks.
public record SyntheticCpuTask(
    long taskId, double arrivalTime, int requiredCores, long requiredRamMb,
    long lengthMiPerCore, long referenceRuntime, double deadline, String taskType)
{
    public SyntheticCpuTask {
        if (taskId < 0 || requiredCores <= 0 || requiredRamMb <= 0 || referenceRuntime <= 0) {
            throw new IllegalArgumentException("Invalid task identity or resource request");
        }
        if (lengthMiPerCore != Math.multiplyExact(referenceRuntime, REFERENCE_MIPS)) {
            throw new IllegalArgumentException("Per-core length must match the reference runtime at 2000 MIPS");
        }
        if (!(arrivalTime >= 0 && deadline > arrivalTime) || !Double.isFinite(deadline)) {
            throw new IllegalArgumentException("Invalid task arrival or absolute Deadline");
        }
    }

    public long totalMi() {
        return Math.multiplyExact(lengthMiPerCore, requiredCores);
    }

    public double executionSeconds(final double mipsPerCore) {
        if (!(mipsPerCore > 0) || !Double.isFinite(mipsPerCore)) {
            throw new IllegalArgumentException("Expected finite positive MIPS per VM core");
        }
        return lengthMiPerCore / mipsPerCore;
    }

    /// Checks the VM's configured capacity. The scheduler still accounts for concurrent reservations.
    public boolean fitsVm(final Vm vm) {
        return requiredCores <= Processor.cores(vm) && requiredRamMb <= vm.getRam().getCapacity();
    }

    public double latestStartTime(final Vm vm) {
        return deadline - executionSeconds(Processor.mips(vm));
    }

    public SyntheticCpuTask withDeadline(final DeadlineRule rule) {
        return new SyntheticCpuTask(taskId, arrivalTime, requiredCores, requiredRamMb, lengthMiPerCore,
            referenceRuntime, rule.absoluteDeadline(arrivalTime, referenceRuntime), taskType);
    }

    public Cloudlet toCloudlet() {
        final var cloudlet = CloudletSimple.of(taskId, lengthMiPerCore, requiredCores);
        cloudlet.setSubmissionDelay(arrivalTime);
        return cloudlet;
    }
}

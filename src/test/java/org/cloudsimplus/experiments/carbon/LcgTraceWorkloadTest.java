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
package org.cloudsimplus.experiments.carbon;

import org.cloudsimplus.cloudlets.Cloudlet;
import org.cloudsimplus.resources.Processor;
import org.junit.jupiter.api.Test;

import java.nio.file.Path;
import java.util.EnumSet;
import java.util.List;
import java.util.stream.Collectors;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class LcgTraceWorkloadTest {
    @Test
    void generatesReproducibleTaskSizesAndArrivalIntensities() {
        final double capacity = CarbonHostFactory.createHosts().stream().mapToDouble(Processor::totalMips).sum();
        final var workload = LcgTraceWorkload.read(Path.of("src/test/resources/LCG.swf"), 0, 86_400, 2000);
        final var low = workload.generate(capacity, 0.3, 42);
        final var medium = workload.generate(capacity, 0.6, 42);
        final var high = workload.generate(capacity, 0.9, 42);

        assertEquals(0.3, low.offeredLoad(), 0.01);
        assertEquals(0.6, medium.offeredLoad(), 0.01);
        assertEquals(0.9, high.offeredLoad(), 0.01);
        assertTrue(low.tasks().size() < medium.tasks().size());
        assertTrue(medium.tasks().size() < high.tasks().size());
        assertEquals(EnumSet.allOf(LcgTraceWorkload.TaskSize.class),
            low.tasks().stream().map(LcgTraceWorkload.Task::size).collect(Collectors.toSet()));

        final var repeated = workload.generate(capacity, 0.3, 42);
        assertEquals(low.tasks().size(), repeated.tasks().size());
        final List<Cloudlet> cloudlets = low.cloudlets();
        assertEquals(cloudlets.stream().map(Cloudlet::getJobId).toList(),
            repeated.cloudlets().stream().map(Cloudlet::getJobId).toList());
        assertNotSame(cloudlets.getFirst(), repeated.cloudlets().getFirst());
        assertEquals(166_000, cloudlets.getFirst().getLength());
        assertEquals(0, cloudlets.getFirst().getSubmissionDelay());
        assertTrue(cloudlets.stream().allMatch(job -> job.getRequiredCores() == 1));
        assertTrue(cloudlets.stream().allMatch(job -> job.getSubmissionDelay() < 86_400));
    }
}

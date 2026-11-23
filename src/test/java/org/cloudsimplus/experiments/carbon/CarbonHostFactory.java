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

import org.cloudsimplus.core.Simulation;
import org.cloudsimplus.datacenters.Datacenter;
import org.cloudsimplus.datacenters.DatacenterSimple;
import org.cloudsimplus.hosts.Host;
import org.cloudsimplus.hosts.HostSimple;
import org.cloudsimplus.resources.CoreCapacity;
import org.cloudsimplus.schedulers.vm.VmSchedulerSpaceShared;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import static org.cloudsimplus.util.BytesConversion.gigaToMega;

/// Creates the fixed-capacity, single-datacenter research scenario.
public final class CarbonHostFactory {
    private static final String RESOURCE_ROOT = "/experiments/carbon/";
    private static final String HOST_CONFIG = "host-config/hosts.csv";

    private CarbonHostFactory() { }

    public static Datacenter createDatacenter(final Simulation simulation) {
        return DatacenterSimple.of(simulation, createHosts());
    }

    public static List<Host> createHosts() {
        final List<String> rows = readLines(HOST_CONFIG);
        final List<Host> hosts = new ArrayList<>();
        for (final String row : rows.stream().skip(1).toList()) {
            final var group = HostGroup.parse(row);
            for (int i = 0; i < group.count(); i++) {
                hosts.add(group.createHost());
            }
        }
        return hosts;
    }

    private static List<String> readLines(final String resource) {
        final var stream = CarbonHostFactory.class.getResourceAsStream(RESOURCE_ROOT + resource);
        if (stream == null) {
            throw new IllegalStateException("Missing experiment resource " + resource);
        }

        try (var reader = new BufferedReader(new InputStreamReader(stream, StandardCharsets.UTF_8))) {
            return reader.lines().filter(line -> !line.isBlank()).toList();
        } catch (final IOException e) {
            throw new UncheckedIOException("Cannot read experiment resource " + resource, e);
        }
    }

    private static List<Double> readPowerCurve(final String resource) {
        final List<String> lines = readLines(resource);
        if (lines.size() != 1) {
            throw new IllegalStateException("Expected one line of SPECpower data in " + resource);
        }

        final List<Double> watts = Arrays.stream(lines.getFirst().strip().split("\\s+"))
            .map(Double::parseDouble)
            .toList();
        if (watts.size() != 11 || watts.stream().anyMatch(value -> !Double.isFinite(value) || value <= 0)) {
            throw new IllegalStateException("Expected 11 positive watt values in " + resource);
        }
        return watts;
    }

    private record HostGroup(int count, CoreCapacity capacity, long ramMb, long bandwidthMbps,
                             long storageMb, List<Double> watts) {
        private static HostGroup parse(final String row) {
            final String[] fields = row.split(",", -1);
            if (fields.length != 13) {
                throw new IllegalStateException("Invalid host configuration " + row);
            }
            return new HostGroup(
                Integer.parseInt(fields[1]),
                new CoreCapacity(Long.parseLong(fields[5]), Double.parseDouble(fields[10])),
                (long) gigaToMega(Long.parseLong(fields[7])),
                Long.parseLong(fields[8]),
                Long.parseLong(fields[9]),
                readPowerCurve(fields[11]));
        }

        private Host createHost() {
            final var host = HostSimple.of(ramMb, bandwidthMbps, storageMb, capacity, true);
            host.setVmScheduler(new VmSchedulerSpaceShared());
            host.setPowerModel(new InterpolatedSpecPowerModel(watts));
            return host;
        }
    }
}

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

import org.cloudsimplus.hosts.Host;
import org.cloudsimplus.resources.Processor;

import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.List;

/// Integrates the realized execution and VM reservation state between simulator events.
final class ReplayMeter {
    record Metrics(double windowKwh, double drainKwh, double cpuUtilization, double ramUtilization,
                   double vmRamUtilization, double avgWatts) { }

    record HostMetrics(long hostId, double mipsCapacity, long ramCapacity, double peakCpu,
                       double peakTaskRam, double peakVmRam, Metrics metrics) { }

    private static final double JOULES_PER_KWH = 3_600_000;
    private final List<HostGauge> hosts = new ArrayList<>();
    private final IdentityHashMap<Host, HostGauge> gauges = new IdentityHashMap<>();
    private final Accumulator cluster;
    private final double window;
    private double limit = Double.POSITIVE_INFINITY;

    ReplayMeter(final List<Host> inventory, final double window) {
        this.window = window;
        double totalCpu = 0;
        long totalRam = 0;
        double idleWatts = 0;
        for (final Host host : inventory) {
            final var gauge = new HostGauge(host);
            hosts.add(gauge);
            gauges.put(host, gauge);
            totalCpu += gauge.state.capacity;
            totalRam += gauge.state.ramCapacity;
            idleWatts += gauge.state.watts;
        }
        cluster = new Accumulator(totalCpu, totalRam, idleWatts);
    }

    void change(final Host host, final double time, final double deltaMips, final long deltaTaskRam, final long deltaVmRam) {
        final var state = gauges.get(host).state;
        final double newCpu = state.cpu + deltaMips;
        final long newTaskRam = state.taskRam + deltaTaskRam;
        final long newVmRam = state.vmRam + deltaVmRam;
        if (newCpu < 0 || newCpu > state.capacity || newTaskRam < 0 || newTaskRam > newVmRam || newVmRam > state.ramCapacity) {
            throw new IllegalStateException("CPU or RAM capacity violated on Host " + host.getId());
        }

        final double newWatts = host.getPowerModel().getPower(newCpu / state.capacity);
        final double deltaWatts = newWatts - state.watts;
        final double included = Math.min(time, limit);
        state.advance(included, window);
        state.change(deltaMips, deltaTaskRam, deltaVmRam, deltaWatts);
        cluster.advance(included, window);
        cluster.change(deltaMips, deltaTaskRam, deltaVmRam, deltaWatts);
    }

    double cpu() { return cluster.cpu / cluster.capacity; }
    double taskRam() { return (double) cluster.taskRam / cluster.ramCapacity; }
    double vmRam() { return (double) cluster.vmRam / cluster.ramCapacity; }
    double watts() { return cluster.watts; }

    void seal(final double serviceEnd) {
        limit = serviceEnd;
    }

    Metrics metrics(final double serviceEnd) {
        return cluster.metrics(serviceEnd, window);
    }

    List<HostMetrics> hostMetrics(final double serviceEnd) {
        return hosts.stream().map(gauge -> gauge.metrics(serviceEnd, window)).toList();
    }

    void verifyNativeState() {
        for (final var gauge : hosts) {
            if (Math.abs(gauge.host.getCpuMipsUtilization() - gauge.state.cpu) > 1e-7
                || gauge.host.getRam().getOccupied() != gauge.state.vmRam) {
                throw new IllegalStateException("Replay resource state differs from CloudSim on Host " + gauge.host.getId());
            }
        }
    }

    private static final class HostGauge {
        private final Host host;
        private final Accumulator state;

        private HostGauge(final Host host) {
            this.host = host;
            state = new Accumulator(Processor.totalMips(host), host.getRam().getCapacity(), host.getPowerModel().getPower(0));
        }

        private HostMetrics metrics(final double end, final double window) {
            return new HostMetrics(host.getId(), state.capacity, state.ramCapacity,
                state.peakCpu / state.capacity, (double) state.peakTaskRam / state.ramCapacity,
                (double) state.peakVmRam / state.ramCapacity, state.metrics(end, window));
        }
    }

    private static final class Accumulator {
        private final double capacity;
        private final long ramCapacity;
        private double time;
        private double cpu;
        private long taskRam;
        private long vmRam;
        private double watts;
        private double peakCpu;
        private long peakTaskRam;
        private long peakVmRam;
        private double windowCpuSeconds;
        private double windowTaskRamSeconds;
        private double windowVmRamSeconds;
        private double windowJoules;
        private double drainJoules;

        private Accumulator(final double capacity, final long ramCapacity, final double idleWatts) {
            this.capacity = capacity;
            this.ramCapacity = ramCapacity;
            watts = idleWatts;
        }

        private void advance(final double current, final double window) {
            if (current < time) {
                throw new IllegalStateException("Replay resource event went backwards in time");
            }

            final double elapsed = current - time;
            final double within = Math.min(current, window) - Math.min(time, window);
            windowCpuSeconds += within * cpu;
            windowTaskRamSeconds += within * taskRam;
            windowVmRamSeconds += within * vmRam;
            windowJoules += within * watts;
            drainJoules += (elapsed - within) * watts;
            time = current;
        }

        private void change(final double dc, final long dr, final long dv, final double dw) {
            cpu += dc;
            taskRam += dr;
            vmRam += dv;
            watts += dw;
            peakCpu = Math.max(peakCpu, cpu);
            peakTaskRam = Math.max(peakTaskRam, taskRam);
            peakVmRam = Math.max(peakVmRam, vmRam);
        }

        private Metrics metrics(final double end, final double window) {
            advance(end, window);
            return new Metrics(windowJoules / JOULES_PER_KWH, drainJoules / JOULES_PER_KWH,
                windowCpuSeconds / (window * capacity), windowTaskRamSeconds / (window * ramCapacity),
                windowVmRamSeconds / (window * ramCapacity), windowJoules / window);
        }
    }
}

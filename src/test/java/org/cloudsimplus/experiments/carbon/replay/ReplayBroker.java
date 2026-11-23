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

import org.cloudsimplus.brokers.DatacenterBrokerSimple;
import org.cloudsimplus.cloudlets.Cloudlet;
import org.cloudsimplus.core.CloudSimPlus;
import org.cloudsimplus.core.CloudSimTag;
import org.cloudsimplus.core.events.SimEvent;
import org.cloudsimplus.datacenters.Datacenter;
import org.cloudsimplus.experiments.carbon.replay.ReplayConfig.VmSpec;
import org.cloudsimplus.experiments.carbon.replay.VmBaselineReplay.Result;
import org.cloudsimplus.experiments.carbon.replay.VmBaselineReplay.Sample;
import org.cloudsimplus.experiments.carbon.replay.VmBaselineReplay.TaskResult;
import org.cloudsimplus.experiments.carbon.replay.VmBaselineReplay.VmResult;
import org.cloudsimplus.experiments.carbon.synthetic.SyntheticCpuTask;
import org.cloudsimplus.hosts.Host;
import org.cloudsimplus.hosts.HostSuitability;
import org.cloudsimplus.placementpolicies.VmPlacementPolicyFirstFit;
import org.cloudsimplus.resources.Processor;
import org.cloudsimplus.vms.Vm;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

/// Keeps the FCFS queue and holds joint CPU/RAM reservations around native Broker events.
final class ReplayBroker extends DatacenterBrokerSimple {
    private static final int ARRIVAL = 301;
    private static final int VM_READY = 302;
    private static final int VM_IDLE_EXPIRED = 303;
    private static final int SAMPLE = 304;

    private record IdleExpiry(VmState vm, long revision) { }
    private record PlacementKey(Host host, VmSpec spec) { }

    private static final class Assignment {
        private final SyntheticCpuTask task;
        private final VmState vm;
        private final double admitted;
        private double start = Double.NaN;

        private Assignment(final SyntheticCpuTask task, final VmState vm, final double admitted) {
            this.task = task;
            this.vm = vm;
            this.admitted = admitted;
        }
    }

    private final ReplayConfig config;
    private final List<Host> hosts;
    private final List<SyntheticCpuTask> arrivals;
    private final ReplayMeter meter;
    private final Map<Vm, VmState> vms = new LinkedHashMap<>();
    private final Map<Long, Assignment> submitted = new HashMap<>();
    private final ArrayDeque<SyntheticCpuTask> waiting = new ArrayDeque<>();
    private final Set<PlacementKey> failedPlacements = new HashSet<>();
    private final List<TaskResult> taskResults = new ArrayList<>();
    private final List<VmResult> vmResults = new ArrayList<>();
    private final List<Sample> samples = new ArrayList<>();
    private final double progressTimeout;
    private int nextArrival;
    private int arrivalsSinceSample;
    private int running;
    private int peakVms;
    private int peakQueue;
    private long reusedTasks;
    private long vmCreationFailures;
    private double lastProgress;
    private double lastTerminal;
    private double serviceEnd = Double.POSITIVE_INFINITY;
    private VmState pending;

    ReplayBroker(final CloudSimPlus simulation, final Datacenter datacenter, final ReplayConfig config,
                 final List<SyntheticCpuTask> tasks) {
        super(simulation, "CpuRamFcfs");
        this.config = config;
        hosts = datacenter.getHostList();
        arrivals = tasks.stream().sorted(Comparator.comparingDouble(SyntheticCpuTask::arrivalTime)
            .thenComparingLong(SyntheticCpuTask::taskId)).toList();
        meter = new ReplayMeter(hosts, config.windowSeconds());
        final double slowest = hosts.stream().mapToDouble(Processor::mips).min().orElseThrow();
        final double longest = arrivals.stream().mapToDouble(task -> task.executionSeconds(slowest)).max().orElse(0);
        progressTimeout = longest + config.idleSeconds() + config.startupSeconds()
            + config.shutdownSeconds() + 2 * config.sampleSeconds();

        setShutdownWhenIdle(false);
        getVmCreation().setMaxRetries(0);
        final var policy = new VmPlacementPolicyFirstFit() {
            @Override
            public HostSuitability placeVm(final Vm vm) {
                final HostSuitability placement = super.placeVm(vm);
                if (placement.fully()) {
                    recordPlacement(vm);
                }
                return placement;
            }
        };
        policy.setFindHostForVmFunction((ignored, vm) ->
            pending != null && pending.vm.equals(vm) ? Optional.of(pending.host) : Optional.empty());
        datacenter.setVmPlacementPolicy(policy);
    }

    @Override
    public void startInternal() {
        super.startInternal();
        closeIfComplete();
        recordSample(0);
        scheduleNextArrival();
        scheduleSample();
    }

    @Override
    public void processEvent(final SimEvent event) {
        switch (event.getTag()) {
            case ARRIVAL -> processArrivals();
            case VM_READY -> {
                ready((Assignment) event.getData());
                dispatch();
            }
            case VM_IDLE_EXPIRED -> expire((IdleExpiry) event.getData());
            case SAMPLE -> sample();
            case CloudSimTag.VM_CREATE_ACK -> {
                super.processEvent(event);
                acknowledgeVm();
            }
            case CloudSimTag.CLOUDLET_RETURN -> {
                super.processEvent(event);
                complete((Cloudlet) event.getData());
            }
            default -> super.processEvent(event);
        }
    }

    private void scheduleNextArrival() {
        if (nextArrival < arrivals.size()) {
            schedule(arrivals.get(nextArrival).arrivalTime() - now(), ARRIVAL, null);
        }
    }

    private void processArrivals() {
        while (nextArrival < arrivals.size() && arrivals.get(nextArrival).arrivalTime() <= now()) {
            waiting.addLast(arrivals.get(nextArrival++));
            arrivalsSinceSample++;
        }
        lastProgress = now();
        updateQueuePeak();
        scheduleNextArrival();
        dispatch();
    }

    private void dispatch() {
        while (pending == null && !waiting.isEmpty()) {
            final var task = waiting.getFirst();
            final VmState existing = firstVm(task);
            if (existing != null) {
                reusedTasks++;
                admitHead(existing);
                continue;
            }

            pending = firstPlacement(task);
            if (pending != null) {
                submitVm(pending.vm);
                return;
            }

            if (!vms.isEmpty()) {
                return;
            }

            taskResults.add(new TaskResult(waiting.removeFirst(), -1, -1, Double.NaN, Double.NaN, Double.NaN,
                Double.NaN, "NO_FEASIBLE_PLACEMENT"));
            failedPlacements.clear();
            lastTerminal = now();
            System.err.printf("No feasible VM placement for task %d at %.6f seconds.%n", task.taskId(), now());
        }
        closeIfComplete();
    }

    private VmState firstVm(final SyntheticCpuTask task) {
        for (final var state : vms.values()) {
            if (state.fits(task)) {
                return state;
            }
        }
        return null;
    }

    private VmState firstPlacement(final SyntheticCpuTask task) {
        for (final VmSpec spec : config.specs()) {
            if (!spec.fits(task)) {
                continue;
            }
            for (final Host host : hosts) {
                if (!ReplayConfig.hasFreeResources(spec, host)
                    || (!failedPlacements.isEmpty() && failedPlacements.contains(new PlacementKey(host, spec)))) {
                    continue;
                }
                final Vm vm = spec.instantiate(host);
                if (host.isSuitableForVm(vm)) {
                    vm.setShutDownDelay(config.shutdownSeconds());
                    final var state = new VmState(vm, host, spec);
                    vm.listeners().addOnHostRemoval(info -> destroyed(state));
                    return state;
                }
            }
        }
        return null;
    }

    private void recordPlacement(final Vm vm) {
        if (pending == null || !pending.vm.equals(vm) || !pending.host.equals(vm.getHost())) {
            throw new IllegalStateException("VM placement differed from the selected Host");
        }
        final var state = pending;
        final var assigned = state.host.getVmScheduler().getAllocatedMips(vm);
        state.mips = assigned.mips();
        if (assigned.cores() != state.spec.cores() || state.mips != Processor.mips(state.host)) {
            throw new IllegalStateException("VM CPU allocation differs from the selected Host capacity");
        }
        state.created = now();
        vms.put(vm, state);
        meter.change(state.host, now(), 0, 0, state.spec.ramMb());
        peakVms = Math.max(peakVms, vms.size());
        lastProgress = now();
    }

    private void acknowledgeVm() {
        if (pending == null) {
            throw new IllegalStateException("Received an unexpected VM creation acknowledgement");
        }
        final var state = pending;
        pending = null;
        if (state.vm.isCreated()) {
            admitHead(state);
        } else {
            vmCreationFailures++;
            failedPlacements.add(new PlacementKey(state.host, state.spec));
        }
        dispatch();
    }

    private void admitHead(final VmState state) {
        final var task = waiting.removeFirst();
        failedPlacements.clear();
        state.reserve(task, now());
        final var assignment = new Assignment(task, state, now());
        if (submitted.put(task.taskId(), assignment) != null) {
            throw new IllegalStateException("Duplicate task admission: " + task.taskId());
        }
        final double readyAt = state.created + config.startupSeconds();
        if (!state.ready && now() < readyAt) {
            // A reserved VM remains unavailable until boot completes. Native boot with an event-driven
            // datacenter schedules processing repeatedly at zero delay.
            schedule(readyAt - now(), VM_READY, assignment);
        } else {
            ready(assignment);
        }
    }

    private void ready(final Assignment assignment) {
        final var state = assignment.vm;
        if (!state.ready) {
            state.ready = true;
            state.readyTime = now();
        }
        final Cloudlet cloudlet = assignment.task.toCloudlet();
        cloudlet.setSubmissionDelay(0);
        cloudlet.listeners().addOnStart(event -> started(assignment));
        submitCloudletList(List.of(cloudlet), state.vm);
    }

    private void started(final Assignment assignment) {
        final var task = assignment.task;
        final var state = assignment.vm;
        if (!Double.isNaN(assignment.start) || now() < task.arrivalTime()) {
            throw new IllegalStateException("Unexpected Cloudlet start for task " + task.taskId());
        }
        assignment.start = now();
        state.startTask(now());
        meter.change(state.host, now(), task.requiredCores() * state.mips, task.requiredRamMb(), 0);
        running++;
        lastProgress = now();
    }

    private void complete(final Cloudlet cloudlet) {
        final var assignment = submitted.remove(cloudlet.getId());
        if (assignment == null) {
            throw new IllegalStateException("Cloudlet returned without a matching task: " + cloudlet.getId());
        }
        final var task = assignment.task;
        final var state = assignment.vm;
        if (!Double.isNaN(assignment.start)) {
            state.finishTask(now());
            meter.change(state.host, now(), -task.requiredCores() * state.mips, -task.requiredRamMb(), 0);
            running--;
        }
        state.release(task);
        if (state.reservedCores == 0) {
            state.idleSince = now();
            schedule(config.idleSeconds(), VM_IDLE_EXPIRED, new IdleExpiry(state, state.revision));
        }
        taskResults.add(new TaskResult(task, state.vm.getId(), state.host.getId(), assignment.admitted, assignment.start,
            cloudlet.getFinishTime(), cloudlet.getTotalExecutionTime(), cloudlet.getStatus().name()));
        lastProgress = lastTerminal = now();
        if (closeIfComplete() && now() >= serviceEnd) {
            recordSample(now());
        }
        dispatch();
    }

    private void expire(final IdleExpiry expiry) {
        final var state = expiry.vm();
        if (state.retiring || state.revision != expiry.revision() || state.reservedCores != 0 || !state.ready) {
            return;
        }
        if (!state.vm.getCloudletScheduler().isEmpty()) {
            throw new IllegalStateException("VM expiration occurred while a Cloudlet was still scheduled");
        }
        state.retiring = true;
        state.shutdownTime = now();
        state.idleSeconds += now() - state.idleSince;
        state.vm.shutdown();
    }

    private void destroyed(final VmState state) {
        if (!state.retiring || state.reservedCores != 0 || vms.remove(state.vm) != state) {
            throw new IllegalStateException("Unexpected VM destruction for " + state.vm.getId());
        }
        vmResults.add(new VmResult(state.vm.getId(), state.spec, state.mips, state.host.getId(), state.created,
            state.readyTime, state.shutdownTime, now(), state.taskCount, state.busySeconds, state.idleSeconds));
        meter.change(state.host, now(), 0, 0, -state.spec.ramMb());
        lastProgress = now();
        failedPlacements.clear();
        dispatch();
    }

    private boolean closeIfComplete() {
        if (taskResults.size() != arrivals.size() || Double.isFinite(serviceEnd)) {
            return false;
        }
        serviceEnd = Math.max(config.windowSeconds(), now());
        meter.seal(serviceEnd);
        return true;
    }

    private void sample() {
        if (now() > serviceEnd) {
            return;
        }
        if (nextArrival == arrivals.size() && taskResults.size() < arrivals.size() && now() - lastProgress > progressTimeout) {
            throw new IllegalStateException("Replay made no progress with " + waiting.size()
                + " globally queued and " + submitted.size() + " admitted tasks");
        }
        recordSample(now());
        scheduleSample();
    }

    private void scheduleSample() {
        if (now() >= serviceEnd) {
            return;
        }
        final double boundary = now() < config.windowSeconds() ? config.windowSeconds() : serviceEnd;
        final double next = Math.min(now() + config.sampleSeconds(), boundary);
        schedule(next - now(), SAMPLE, null);
    }

    private void recordSample(final double time) {
        int arrivalsInBin = arrivalsSinceSample;
        if (!samples.isEmpty() && samples.getLast().time() == time) {
            arrivalsInBin += samples.removeLast().arrivals();
        }
        samples.add(new Sample(time, arrivalsInBin, running, queueSize(), waiting.size(), vms.size(),
            meter.cpu(), meter.taskRam(), meter.vmRam(), meter.watts()));
        arrivalsSinceSample = 0;
    }

    private int queueSize() {
        return waiting.size() + submitted.size() - running;
    }

    private void updateQueuePeak() {
        peakQueue = Math.max(peakQueue, queueSize());
    }

    private double now() {
        return getSimulation().clock();
    }

    Result result() {
        if (taskResults.size() != arrivals.size() || !vms.isEmpty() || pending != null || !submitted.isEmpty()) {
            throw new IllegalStateException("Replay ended with unresolved tasks or VMs");
        }
        meter.verifyNativeState();
        taskResults.sort(Comparator.comparingLong(item -> item.task().taskId()));
        vmResults.sort(Comparator.comparingLong(VmResult::id));
        return new Result(List.copyOf(taskResults), List.copyOf(vmResults), List.copyOf(samples),
            meter.metrics(serviceEnd), meter.hostMetrics(serviceEnd), peakVms, reusedTasks, vmCreationFailures,
            peakQueue, lastTerminal, serviceEnd, now(), hosts.stream().mapToDouble(Processor::totalMips).sum());
    }

    private static final class VmState {
        private final Vm vm;
        private final Host host;
        private final VmSpec spec;
        private double mips;
        private double created;
        private double readyTime;
        private double shutdownTime;
        private double idleSince = -1;
        private double busySince;
        private double busySeconds;
        private double idleSeconds;
        private long reservedRam;
        private long revision;
        private int reservedCores;
        private int runningTasks;
        private int taskCount;
        private boolean ready;
        private boolean retiring;

        private VmState(final Vm vm, final Host host, final VmSpec spec) {
            this.vm = vm;
            this.host = host;
            this.spec = spec;
        }

        private boolean fits(final SyntheticCpuTask task) {
            return ready && !retiring && task.requiredCores() <= spec.cores() - reservedCores
                && task.requiredRamMb() <= spec.ramMb() - reservedRam;
        }

        private void reserve(final SyntheticCpuTask task, final double time) {
            reservedCores += task.requiredCores();
            reservedRam += task.requiredRamMb();
            if (retiring || reservedCores > spec.cores() || reservedRam > spec.ramMb()) {
                throw new IllegalStateException("Attempted to overbook VM " + vm.getId());
            }
            revision++;
            if (idleSince >= 0) {
                idleSeconds += time - idleSince;
                idleSince = -1;
            }
        }

        private void release(final SyntheticCpuTask task) {
            reservedCores -= task.requiredCores();
            reservedRam -= task.requiredRamMb();
            if (reservedCores < 0 || reservedRam < 0) {
                throw new IllegalStateException("Released an unreserved resource on VM " + vm.getId());
            }
        }

        private void startTask(final double time) {
            if (runningTasks++ == 0) {
                busySince = time;
            }
            taskCount++;
        }

        private void finishTask(final double time) {
            if (--runningTasks == 0) {
                busySeconds += time - busySince;
            }
        }
    }
}

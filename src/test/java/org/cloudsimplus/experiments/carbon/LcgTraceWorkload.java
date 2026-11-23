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
import org.cloudsimplus.cloudlets.CloudletSimple;
import org.cloudsimplus.resources.Processor;
import org.cloudsimplus.traces.SwfWorkloadFileReader;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.SplittableRandom;

/// Generates single-core CPU batch jobs from an LCG trace window.
public final class LcgTraceWorkload {
    private static final long DAY_SECONDS = 86_400;
    private static final int REFERENCE_MIPS = 2000;
    private static final long SEED = 42;
    private static final double[] TARGET_LOADS = {0.30, 0.60, 0.90};

    public enum TaskSize { SHORT, MEDIUM, LONG }

    public record Task(Cloudlet cloudlet, TaskSize size) { }

    public record Scenario(List<Task> tasks, double offeredLoad) {
        public List<Cloudlet> cloudlets() {
            return tasks.stream().map(Task::cloudlet).toList();
        }
    }

    private record RecordedJob(long id, long arrivalSeconds, long lengthMi) {
        private Cloudlet createCloudlet(final long generatedId) {
            final var cloudlet = CloudletSimple.of(generatedId, lengthMi, 1);
            cloudlet.setJobId(id).setSubmissionDelay(arrivalSeconds);
            return cloudlet;
        }
    }

    private final List<RecordedJob> jobs;
    private final long windowSeconds;
    private final long sourceWorkMi;
    private final long medianLengthMi;
    private final long p90LengthMi;

    private LcgTraceWorkload(final List<RecordedJob> jobs, final long windowSeconds) {
        if (jobs.isEmpty()) {
            throw new IllegalArgumentException("The trace window contains no single-core jobs");
        }

        this.jobs = jobs;
        this.windowSeconds = windowSeconds;
        final long[] lengths = jobs.stream().mapToLong(RecordedJob::lengthMi).sorted().toArray();
        sourceWorkMi = jobs.stream().mapToLong(RecordedJob::lengthMi).sum();
        medianLengthMi = lengths[(int) Math.ceil(lengths.length * 0.5) - 1];
        p90LengthMi = lengths[(int) Math.ceil(lengths.length * 0.9) - 1];
    }

    public static LcgTraceWorkload read(
        final Path trace, final long windowStartSeconds, final long windowSeconds, final int referenceMips)
    {
        if (windowStartSeconds < 0 || windowSeconds <= 0 || referenceMips <= 0) {
            throw new IllegalArgumentException("Expected a nonnegative start time, positive duration and positive MIPS");
        }

        final long endSeconds = Math.addExact(windowStartSeconds, windowSeconds);
        // Read lengths as seconds first, then scale with long arithmetic.
        final var reader = new SwfWorkloadFileReader(trace.toString(), 1);
        reader.setPredicate(cloudlet -> {
            final double arrival = cloudlet.getSubmissionDelay();
            return cloudlet.getRequiredCores() == 1 && arrival >= windowStartSeconds && arrival < endSeconds;
        });
        final List<RecordedJob> jobs = reader.generateWorkload().stream()
            .map(cloudlet -> new RecordedJob(
                cloudlet.getId(), (long) cloudlet.getSubmissionDelay() - windowStartSeconds,
                cloudlet.getLength() * referenceMips))
            .toList();
        return new LcgTraceWorkload(jobs, windowSeconds);
    }

    /// Uses seeded sampling of integer job copies to adjust demand over the same arrival window.
    public Scenario generate(final double capacityMips, final double targetLoad, final long seed) {
        final double availableMi = capacityMips * windowSeconds;
        if (!Double.isFinite(availableMi) || availableMi <= 0 || !(targetLoad > 0 && targetLoad <= 1)) {
            throw new IllegalArgumentException("Expected positive finite capacity and a target in (0, 1]");
        }

        final double multiplier = targetLoad * availableMi / sourceWorkMi;
        if (Math.ceil(multiplier) * jobs.size() > Integer.MAX_VALUE) {
            throw new IllegalArgumentException("The generated workload can exceed the maximum list size");
        }

        final int wholeCopies = (int) multiplier;
        final double extraCopyProbability = multiplier - wholeCopies;
        final var random = new SplittableRandom(seed);
        final List<Task> tasks = new ArrayList<>();
        long generatedMi = 0;
        for (final RecordedJob job : jobs) {
            final int copies = wholeCopies + (random.nextDouble() < extraCopyProbability ? 1 : 0);
            final TaskSize size = classify(job.lengthMi());
            for (int i = 0; i < copies; i++) {
                tasks.add(new Task(job.createCloudlet(tasks.size()), size));
            }
            generatedMi += job.lengthMi() * copies;
        }
        return new Scenario(List.copyOf(tasks), generatedMi / availableMi);
    }

    private TaskSize classify(final long lengthMi) {
        if (lengthMi <= medianLengthMi) {
            return TaskSize.SHORT;
        }
        return lengthMi <= p90LengthMi ? TaskSize.MEDIUM : TaskSize.LONG;
    }

    public static void main(final String[] args) throws IOException {
        if (args.length != 2) {
            throw new IllegalArgumentException("Usage: LcgTraceWorkload <LCG.swf> <output-directory>");
        }

        final var output = Files.createDirectories(Path.of(args[1]));
        final var workload = read(Path.of(args[0]), 0, DAY_SECONDS, REFERENCE_MIPS);
        final double capacity = CarbonHostFactory.createHosts().stream().mapToDouble(Processor::totalMips).sum();
        for (final double target : TARGET_LOADS) {
            final var scenario = workload.generate(capacity, target, SEED);
            final Path file = output.resolve("lcg-%d-percent.csv".formatted(Math.round(100 * target)));
            writeCsv(file, scenario);
            System.out.printf(Locale.ROOT, "%s  %,d jobs  %.2f%% offered load%n",
                file, scenario.tasks().size(), scenario.offeredLoad() * 100);
        }
    }

    private static void writeCsv(final Path file, final Scenario scenario) throws IOException {
        try (var writer = Files.newBufferedWriter(file)) {
            writer.write("cloudlet_id,source_job_id,arrival_s,cores,length_mi_per_core,task_size\n");
            for (final Task task : scenario.tasks()) {
                final Cloudlet cloudlet = task.cloudlet();
                writer.write(String.format(Locale.ROOT, "%d,%d,%d,%d,%d,%s\n",
                    cloudlet.getId(), cloudlet.getJobId(), (long) cloudlet.getSubmissionDelay(),
                    cloudlet.getRequiredCores(), cloudlet.getLength(), task.size()));
            }
        }
    }
}

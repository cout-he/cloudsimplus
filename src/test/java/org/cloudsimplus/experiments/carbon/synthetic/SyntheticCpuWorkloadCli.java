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

import org.cloudsimplus.experiments.carbon.CarbonHostFactory;
import org.cloudsimplus.experiments.carbon.synthetic.SyntheticCpuWorkloadConfig.DeadlineRule;
import org.cloudsimplus.experiments.carbon.synthetic.SyntheticCpuWorkloadGenerator.Scenario;
import org.cloudsimplus.hosts.Host;

import java.io.BufferedWriter;
import java.io.IOException;
import java.math.BigDecimal;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Locale;
import java.util.SplittableRandom;

import static org.cloudsimplus.experiments.carbon.synthetic.SyntheticCpuWorkloadConfig.DAY_SECONDS;

/// Exports task files and the per-window arrival rates needed for independent validation.
public final class SyntheticCpuWorkloadCli {
    private SyntheticCpuWorkloadCli() { }

    public static void main(final String[] args) throws IOException {
        if (args.length < 2 || args.length > 4 || (args.length == 4 && !args[3].equals("--sensitivity"))) {
            throw new IllegalArgumentException(
                "Usage: SyntheticCpuWorkloadCli <config.properties> <output-directory> [replications] [--sensitivity]");
        }

        final int replications = args.length >= 3 ? Integer.parseInt(args[2]) : 1;
        if (replications < 1) {
            throw new IllegalArgumentException("replications must be positive");
        }

        final var config = SyntheticCpuWorkloadConfig.read(Path.of(args[0]));
        final List<Host> hosts = CarbonHostFactory.createHosts();
        final var generator = new SyntheticCpuWorkloadGenerator(config, hosts);
        final var output = Files.createDirectories(Path.of(args[1]));
        export(output, config, generator, replications, args.length == 4);
    }

    private static void export(
        final Path output, final SyntheticCpuWorkloadConfig config,
        final SyntheticCpuWorkloadGenerator generator, final int replications, final boolean exportSensitivity) throws IOException
    {
        try (var manifest = Files.newBufferedWriter(output.resolve("manifest.csv"));
             var rates = Files.newBufferedWriter(output.resolve("arrival-windows.csv"));
             var variants = Files.newBufferedWriter(output.resolve("deadline-variants.csv")))
        {
            manifest.write("file,target,seed,attempts,mean_rate_per_s,total_mi,offered_load,tasks,"
                + "capacity_mips,day_s,window_s,reference_mips,tolerance,deadline_alpha,deadline_base_s\n");
            rates.write("file,window_start_s,task_type,rate_per_s,arrivals\n");
            variants.write("main_file,variant_file,deadline_alpha,deadline_base_s\n");
            writeVmShapes(output, generator);
            final var seeds = new SplittableRandom(config.seed());
            for (final double target : config.targets()) {
                final var levelSeeds = seeds.split();
                for (int run = 1; run <= replications; run++) {
                    final var scenario = generator.generate(target, levelSeeds.nextLong());
                    final String file = taskFilename(target, run);
                    writeTasks(output.resolve(file), scenario.tasks());
                    writeManifestRow(manifest, file, scenario, config, generator.capacityMips());
                    writeRateRows(rates, file, scenario);
                    if (exportSensitivity) {
                        writeVariants(output, config, file, scenario, variants);
                    }
                    System.out.printf(Locale.ROOT, "%s  %,d tasks  %.4f%% offered load%n",
                        file, scenario.tasks().size(), 100 * scenario.offeredLoad());
                }
            }
        }
    }

    private static String taskFilename(final double target, final int run) {
        final String percent = BigDecimal.valueOf(target).movePointRight(2).stripTrailingZeros().toPlainString();
        final String suffix = run == 1 ? "" : "-run-" + run;
        return "workload-" + percent + suffix + ".csv";
    }

    private static void writeVariants(final Path output, final SyntheticCpuWorkloadConfig config,
                                      final String file, final Scenario scenario, final BufferedWriter variants) throws IOException
    {
        final var defaultRule = config.deadline();
        for (final double alpha : config.sensitivityAlphas()) {
            final var rule = new DeadlineRule(alpha, defaultRule.baseSeconds());
            String variantFile = file;
            if (!rule.equals(defaultRule)) {
                variantFile = file.substring(0, file.length() - ".csv".length()) + "-alpha-" + alpha + ".csv";
                writeTasks(output.resolve(variantFile), scenario.tasks().stream().map(task -> task.withDeadline(rule)).toList());
            }
            variants.write(file + "," + variantFile + "," + alpha + "," + rule.baseSeconds() + "\n");
        }
    }

    private static void writeVmShapes(final Path output, final SyntheticCpuWorkloadGenerator generator) throws IOException {
        try (var writer = Files.newBufferedWriter(output.resolve("vm-shapes.csv"))) {
            writer.write("cores,ram_mb,mips_per_core\n");
            for (final var shape : generator.vmShapes()) {
                writer.write(shape.cores() + "," + shape.ramMb() + "," + shape.mipsPerCore() + "\n");
            }
        }
    }

    private static void writeTasks(final Path path, final List<SyntheticCpuTask> tasks) throws IOException {
        try (var writer = Files.newBufferedWriter(path)) {
            writer.write("task_id,task_type,arrival_time_s,required_cores,required_ram_mb,length_mi_per_core,reference_runtime_s,deadline_s\n");
            for (final var task : tasks) {
                writer.write(task.taskId() + "," + task.taskType() + "," + task.arrivalTime() + "," + task.requiredCores()
                    + "," + task.requiredRamMb() + "," + task.lengthMiPerCore() + "," + task.referenceRuntime() + "," + task.deadline() + "\n");
            }
        }
    }

    private static void writeManifestRow(
        final BufferedWriter writer, final String file, final Scenario scenario,
        final SyntheticCpuWorkloadConfig config, final double capacityMips) throws IOException
    {
        writer.write(file + "," + scenario.targetLoad() + "," + scenario.seed() + "," + scenario.attempts() + ","
            + scenario.meanArrivalRate() + "," + scenario.totalMi() + "," + scenario.offeredLoad() + "," + scenario.tasks().size()
            + "," + capacityMips + "," + DAY_SECONDS + "," + config.windowSeconds() + "," + config.referenceMips()
            + "," + config.tolerance() + "," + config.deadline().alpha() + "," + config.deadline().baseSeconds() + "\n");
    }

    private static void writeRateRows(final BufferedWriter writer, final String file, final Scenario scenario) throws IOException {
        for (final var window : scenario.rateWindows()) {
            writer.write(file + "," + window.startSeconds() + "," + window.taskType() + ","
                + window.ratePerSecond() + "," + window.arrivals() + "\n");
        }
    }
}

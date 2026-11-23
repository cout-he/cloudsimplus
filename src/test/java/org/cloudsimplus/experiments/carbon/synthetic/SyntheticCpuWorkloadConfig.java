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

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Arrays;
import java.util.List;
import java.util.Properties;

/// Parameters for a 24-hour synthetic CPU workload.
public record SyntheticCpuWorkloadConfig(
    int referenceMips, int windowSeconds, long seed, List<Double> targets,
    double tolerance, int maxAttempts, int ramStepMb, DeadlineRule deadline,
    List<Double> sensitivityAlphas, List<TaskType> types)
{
    public static final int DAY_SECONDS = 86_400;
    public static final int REFERENCE_MIPS = 2000;

    public SyntheticCpuWorkloadConfig {
        targets = List.copyOf(targets);
        sensitivityAlphas = List.copyOf(sensitivityAlphas);
        types = List.copyOf(types);
        require(referenceMips == REFERENCE_MIPS, "reference_mips must be 2000");
        require(ramStepMb > 0 && maxAttempts > 0, "Invalid RAM step or attempt limit");
        require(windowSeconds > 0 && DAY_SECONDS % windowSeconds == 0, "window_s must divide 86,400 evenly");
        require(tolerance > 0 && tolerance < 1, "tolerance must be in (0, 1)");
        require(!targets.isEmpty() && targets.stream().allMatch(target -> target > 0 && target <= 1),
            "targets must be in (0, 1]");
        require(targets.stream().distinct().count() == targets.size(), "targets must be unique");
        require(!sensitivityAlphas.isEmpty() && sensitivityAlphas.stream().allMatch(DeadlineRule::validAlpha),
            "Deadline sensitivity factors must be finite and positive");
        require(sensitivityAlphas.stream().distinct().count() == sensitivityAlphas.size(), "Deadline sensitivity factors must be unique");
        require(!types.isEmpty() && types.stream().map(TaskType::name).distinct().count() == types.size(),
            "Task type names must be unique");
        requireUnitSum(types.stream().mapToDouble(TaskType::share).sum(), "Task count shares");
        for (final TaskType type : types) {
            require(type.minRamPerCore() % ramStepMb == 0 && type.maxRamPerCore() % ramStepMb == 0,
                "RAM bounds must be multiples of ram_step_mb for " + type.name());
        }
    }

    public record CoreOption(int cores, double probability) {
        public CoreOption {
            require(cores > 0 && probability > 0 && probability <= 1, "Invalid core option");
        }
    }

    public record RuntimeRange(int minimum, int mode, int maximum) {
        public RuntimeRange {
            require(minimum > 0 && minimum <= mode && mode <= maximum, "Invalid triangular runtime");
        }

        double meanSeconds() {
            return ((double) minimum + mode + maximum) / 3;
        }
    }

    public record DeadlineRule(double alpha, double baseSeconds) {
        public DeadlineRule {
            require(validAlpha(alpha) && baseSeconds >= 0 && Double.isFinite(baseSeconds), "Invalid Deadline parameters");
        }

        private static boolean validAlpha(final double alpha) {
            return alpha > 0 && Double.isFinite(alpha);
        }

        double absoluteDeadline(final double arrival, final long runtime) {
            final double deadline = arrival + baseSeconds + alpha * runtime;
            require(Double.isFinite(deadline) && deadline > arrival, "Deadline must be finite and after arrival");
            return deadline;
        }
    }

    public record ArrivalPattern(double amplitude, double periodHours, double peakHour, double sigma) {
        public ArrivalPattern {
            require(amplitude >= 0 && Double.isFinite(amplitude) && sigma >= 0 && Double.isFinite(sigma),
                "Arrival amplitude and sigma must be finite and nonnegative");
            require(periodHours > 0 && Double.isFinite(periodHours) && peakHour >= 0 && peakHour < 24,
                "Invalid arrival period or peak hour");
        }
    }

    public record TaskType(
        String name, double share, List<CoreOption> coreOptions, int minRamPerCore,
        int maxRamPerCore, RuntimeRange runtime, ArrivalPattern arrival)
    {
        public TaskType {
            coreOptions = List.copyOf(coreOptions);
            require(name.matches("[a-z][a-z0-9_-]*") && share > 0 && share <= 1, "Invalid task name or share: " + name);
            require(minRamPerCore > 0 && maxRamPerCore >= minRamPerCore, "Invalid RAM range for " + name);
            require(!coreOptions.isEmpty() && coreOptions.stream().map(CoreOption::cores).distinct().count() == coreOptions.size(),
                "Core options must be unique for " + name);
            requireUnitSum(coreOptions.stream().mapToDouble(CoreOption::probability).sum(), name + " core probabilities");
        }

        double meanMi(final int referenceMips) {
            final double meanCores = coreOptions.stream().mapToDouble(option -> option.cores() * option.probability()).sum();
            return meanCores * runtime.meanSeconds() * referenceMips;
        }
    }

    public static SyntheticCpuWorkloadConfig read(final Path path) throws IOException {
        final var properties = new Properties();
        try (var reader = Files.newBufferedReader(path)) {
            properties.load(reader);
        }

        final List<TaskType> types = tokens(value(properties, "types")).stream()
            .map(name -> readType(properties, name))
            .toList();
        final List<Double> targets = tokens(value(properties, "targets")).stream().map(Double::valueOf).toList();
        final List<Double> alphas = tokens(value(properties, "deadline_sensitivity_alphas")).stream().map(Double::valueOf).toList();
        return new SyntheticCpuWorkloadConfig(
            Integer.parseInt(value(properties, "reference_mips")),
            Integer.parseInt(value(properties, "window_s")),
            Long.parseLong(value(properties, "seed")),
            targets,
            Double.parseDouble(value(properties, "tolerance")),
            Integer.parseInt(value(properties, "max_attempts")),
            Integer.parseInt(value(properties, "ram_step_mb")),
            new DeadlineRule(Double.parseDouble(value(properties, "deadline_alpha")),
                Double.parseDouble(value(properties, "deadline_base_s"))),
            alphas,
            types);
    }

    private static TaskType readType(final Properties properties, final String name) {
        final String prefix = name + ".";
        final List<CoreOption> cores = tokens(value(properties, prefix + "cores")).stream()
            .map(SyntheticCpuWorkloadConfig::parseCoreOption)
            .toList();
        final List<String> ram = values(properties, prefix + "ram_mb_per_core", 2);
        final List<String> runtime = values(properties, prefix + "runtime_s", 3);
        final List<String> arrival = values(properties, prefix + "arrival", 4);
        return new TaskType(
            name, Double.parseDouble(value(properties, prefix + "share")), cores,
            Integer.parseInt(ram.get(0)), Integer.parseInt(ram.get(1)),
            new RuntimeRange(Integer.parseInt(runtime.get(0)), Integer.parseInt(runtime.get(1)), Integer.parseInt(runtime.get(2))),
            new ArrivalPattern(Double.parseDouble(arrival.get(0)), Double.parseDouble(arrival.get(1)),
                Double.parseDouble(arrival.get(2)), Double.parseDouble(arrival.get(3))));
    }

    private static CoreOption parseCoreOption(final String token) {
        final String[] pair = token.split(":", -1);
        require(pair.length == 2, "Expected cores:probability, got " + token);
        return new CoreOption(Integer.parseInt(pair[0].strip()), Double.parseDouble(pair[1].strip()));
    }

    private static List<String> values(final Properties properties, final String key, final int count) {
        final List<String> parts = tokens(value(properties, key));
        require(parts.size() == count, "Expected " + count + " values for " + key);
        return parts;
    }

    private static List<String> tokens(final String raw) {
        return Arrays.stream(raw.split(",", -1)).map(String::strip).toList();
    }

    private static String value(final Properties properties, final String key) {
        final String raw = properties.getProperty(key);
        if (raw == null || raw.isBlank()) {
            throw new IllegalArgumentException("Missing configuration property " + key);
        }
        return raw.strip();
    }

    private static void requireUnitSum(final double sum, final String description) {
        require(Math.abs(sum - 1) < 1e-9, description + " must sum to one");
    }

    private static void require(final boolean condition, final String message) {
        if (!condition) {
            throw new IllegalArgumentException(message);
        }
    }
}

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

import org.cloudsimplus.power.PowerMeasurement;
import org.cloudsimplus.power.models.PowerModelHostAbstract;

import java.util.List;

import static org.cloudsimplus.resources.Processor.totalMips;

/// Interpolates single-node SPECpower watts over the target-load points from 0% to 100%.
final class InterpolatedSpecPowerModel extends PowerModelHostAbstract {
    private final List<Double> watts;

    InterpolatedSpecPowerModel(final List<Double> watts) {
        this.watts = watts;
    }

    @Override
    protected double getPowerInternal(final double utilizationFraction) {
        final int lastIndex = watts.size() - 1;
        final double position = utilizationFraction * lastIndex;
        final int lower = (int) position;
        final int upper = Math.min(lower + 1, lastIndex);
        final double lowerPower = watts.get(lower);
        return lowerPower + (watts.get(upper) - lowerPower) * (position - lower);
    }

    @Override
    public PowerMeasurement getPowerMeasurement() {
        final var host = getHost();
        if (!host.isActive()) {
            return new PowerMeasurement();
        }

        final double capacity = totalMips(host);
        final double utilization = capacity > 0 ? host.getCpuMipsUtilization() / capacity : 0;
        final double boundedUtilization = Math.clamp(utilization, 0, 1);
        final double idlePower = watts.getFirst();
        return new PowerMeasurement(idlePower, getPower(boundedUtilization) - idlePower);
    }
}

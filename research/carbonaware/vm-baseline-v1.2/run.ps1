$ErrorActionPreference = 'Stop'

$project = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../../..')).Path
Push-Location -LiteralPath $project
try {
    if (-not $env:JAVA_HOME) { throw 'Set JAVA_HOME to JDK 25.' }
    $java = Join-Path $env:JAVA_HOME 'bin/java.exe'
    $build = 'target/vm-baseline-v1.2'
    $results = Join-Path $build 'results'
    $experiment = 'research/carbonaware/vm-baseline-v1.2'
    $input = 'research/carbonaware/synthetic-v1/results'
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    New-Item -ItemType Directory -Path $build -Force | Out-Null
    & mvn -B -l (Join-Path $build 'maven-small.log') '-Dtest=VmBaselineReplayTest,CarbonDatacenterIT,SyntheticCpuWorkloadTest' test
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    $report = [xml](Get-Content -Raw -LiteralPath 'target/surefire-reports/TEST-org.cloudsimplus.experiments.carbon.replay.VmBaselineReplayTest.xml')
    $classpath = ($report.testsuite.properties.property | Where-Object name -eq 'surefire.test.class.path').value
    if (-not $classpath) { throw 'Missing test classpath in Maven report.' }
    foreach ($level in 30, 60, 90) {
        $scenario = "baseline-$level"
        $workload = Join-Path $input "workload-$level.csv"
        $output = Join-Path $results $scenario
        $command = 'org.cloudsimplus.experiments.carbon.replay.VmBaselineReplay'
        $elapsed = [System.Diagnostics.Stopwatch]::StartNew()
        $messages = @(& $java -Xmx2g -cp $classpath $command $experiment $workload $output 2>&1)
        $status = $LASTEXITCODE
        $elapsed.Stop()
        $messages | ForEach-Object { Write-Output $_ }
        $log = @("Scenario: $scenario", "Input: $workload", "Java exit code: $status",
            "Wall-clock seconds: $($elapsed.Elapsed.TotalSeconds)") + $messages
        $logPath = Join-Path $project (Join-Path $build "$scenario.txt")
        [System.IO.File]::WriteAllLines($logPath, [string[]]$log, $utf8)
        if ($status -ne 0) { exit $status }
        $totals = Import-Csv -LiteralPath (Join-Path $output 'summary.csv')
        if ([int]$totals.tasks -ne ([int]$totals.completed + [int]$totals.other)) {
            throw "Unreconciled task counts in $scenario"
        }
    }

    python "$experiment/validate_and_plot.py" $results (Join-Path $build 'reports')
    exit $LASTEXITCODE
} finally {
    Pop-Location
}

param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,
    [switch]$NoBrowser,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonPath = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$WebDir = Join-Path $ProjectDir "web"
$DistIndex = Join-Path $WebDir "dist\index.html"
$Url = "http://127.0.0.1:$Port"
$EnvFile = Join-Path $ProjectDir ".env"
function Get-ConfiguredValue {
    param([string]$Name, [string]$Default)
    $processValue = [Environment]::GetEnvironmentVariable($Name)
    if ($processValue) { return $processValue }
    if (Test-Path -LiteralPath $EnvFile) {
        $line = Get-Content -LiteralPath $EnvFile | Where-Object { $_ -match "^\s*$Name\s*=" } | Select-Object -Last 1
        if ($line) { return (($line -split "=", 2)[1].Trim()).Trim('"').Trim("'") }
    }
    return $Default
}
$OllamaHost = (Get-ConfiguredValue "ANALYSE_AGENT_OLLAMA_HOST" "http://127.0.0.1:11434").TrimEnd('/')
$OllamaModel = Get-ConfiguredValue "ANALYSE_AGENT_OLLAMA_MODEL" "qwen3.5:4b"
$OllamaEmbeddingModel = Get-ConfiguredValue "ANALYSE_AGENT_OLLAMA_EMBEDDING_MODEL" "qwen3-embedding:0.6b"
$OllamaFlashAttention = Get-ConfiguredValue "OLLAMA_FLASH_ATTENTION" "1"
$OllamaKvCacheType = Get-ConfiguredValue "OLLAMA_KV_CACHE_TYPE" "q8_0"
$OllamaNumParallel = Get-ConfiguredValue "OLLAMA_NUM_PARALLEL" "1"
Set-Location -LiteralPath $ProjectDir

function Test-TcpPort {
    param([int]$TargetPort)

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $result = $client.BeginConnect("127.0.0.1", $TargetPort, $null, $null)
        if (-not $result.AsyncWaitHandle.WaitOne(300)) {
            return $false
        }
        $client.EndConnect($result)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Open-AgentBrowser {
    if (-not $NoBrowser) {
        Start-Process $Url
    }
}

function Get-AgentProcesses {
    $projectMarker = ((Resolve-Path -LiteralPath $ProjectDir).Path.TrimEnd('\')).ToLowerInvariant()
    @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        if ($_.ProcessId -eq $PID -or $_.Name -notin @("python.exe", "pythonw.exe")) {
            return $false
        }
        $commandLine = ([string]$_.CommandLine).ToLowerInvariant()
        return $commandLine.Contains($projectMarker) -and $commandLine -match "uvicorn(?:\.exe)?\s+main:app"
    })
}

function Stop-StaleAgentProcesses {
    $staleProcesses = @(Get-AgentProcesses)
    foreach ($process in $staleProcesses) {
        Write-Host "Stopping previous Analyse Agent instance PID $($process.ProcessId)..." -ForegroundColor Yellow
        # Another stale worker may have exited between enumeration and stop.
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($staleProcesses.Count -gt 0) {
        Start-Sleep -Milliseconds 500
    }
}

# A previous instance may be listening on another port. Stop only processes
# that unambiguously belong to this project before starting the requested port.
Stop-StaleAgentProcesses

if (Test-TcpPort -TargetPort $Port) {
    $healthOk = $false
    try {
        $health = Invoke-RestMethod -Uri "$Url/api/health" -TimeoutSec 2
        $healthOk = $health.status -eq "ok"
    }
    catch {
        $healthOk = $false
    }
    if ($healthOk) {
        throw "Port $Port is still occupied by an Analyse Agent process that could not be stopped."
    }
    throw "Port $Port is used by another program. Close it or run: .\start.ps1 -Port 8001"
}

function Get-OllamaModels {
    try {
        return Invoke-RestMethod -Uri "$OllamaHost/api/tags" -TimeoutSec 2
    }
    catch {
        return $null
    }
}

$ollamaModels = Get-OllamaModels
if (-not $ollamaModels) {
    $ollamaUri = [Uri]$OllamaHost
    $isLocalOllama = $ollamaUri.Host -in @("127.0.0.1", "localhost", "::1")
    if ($isLocalOllama) {
        $ollamaCommandInfo = Get-Command ollama -ErrorAction SilentlyContinue
        if (-not $ollamaCommandInfo) {
            throw "Ollama is not responding at $OllamaHost and the local ollama command was not found."
        }
        Write-Host "Starting the local Ollama service..." -ForegroundColor Cyan
        $env:OLLAMA_FLASH_ATTENTION = $OllamaFlashAttention
        $env:OLLAMA_KV_CACHE_TYPE = $OllamaKvCacheType
        $env:OLLAMA_NUM_PARALLEL = $OllamaNumParallel
        Start-Process -FilePath $ollamaCommandInfo.Source -ArgumentList "serve" -WindowStyle Hidden | Out-Null
        for ($attempt = 0; $attempt -lt 20 -and -not $ollamaModels; $attempt++) {
            Start-Sleep -Milliseconds 500
            $ollamaModels = Get-OllamaModels
        }
    }
}

if (-not $ollamaModels) {
    throw "Cannot start or connect to the configured Ollama service at $OllamaHost."
}

$hasModel = $ollamaModels.models | Where-Object {
    $_.name -eq $OllamaModel -or $_.model -eq $OllamaModel
}
if (-not $hasModel) {
    throw "The configured model $OllamaModel is missing. Run: ollama pull $OllamaModel"
}
$hasEmbeddingModel = $ollamaModels.models | Where-Object {
    $_.name -eq $OllamaEmbeddingModel -or $_.model -eq $OllamaEmbeddingModel
}
if (-not $hasEmbeddingModel) {
    throw "The configured embedding model $OllamaEmbeddingModel is missing. Run: ollama pull $OllamaEmbeddingModel"
}

if (-not (Test-Path -LiteralPath $PythonPath)) {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "The Python environment is missing and uv was not found. Install uv and run this script again."
    }
    Write-Host "Creating the Python environment and installing dependencies..." -ForegroundColor Cyan
    & uv sync --dev
    if ($LASTEXITCODE -ne 0) {
        throw "Python dependency installation failed."
    }
}
elseif (Get-Command uv -ErrorAction SilentlyContinue) {
    Write-Host "Checking Python dependencies..." -ForegroundColor Cyan
    & uv sync --dev
    if ($LASTEXITCODE -ne 0) {
        throw "Python dependency check failed."
    }
}
else {
    & $PythonPath -c "import fastapi, langgraph, duckdb, polars, ollama, uvicorn"
    if ($LASTEXITCODE -ne 0) {
        throw "The existing .venv is incomplete and uv was not found, so it cannot be repaired automatically."
    }
}

$needsBuild = -not (Test-Path -LiteralPath $DistIndex)
if (-not $needsBuild -and -not $SkipBuild) {
    $buildInputs = @(
        (Join-Path $WebDir "src"),
        (Join-Path $WebDir "index.html"),
        (Join-Path $WebDir "package.json"),
        (Join-Path $WebDir "package-lock.json"),
        (Join-Path $WebDir "vite.config.ts"),
        (Join-Path $WebDir "uno.config.ts"),
        (Join-Path $WebDir "tsconfig.app.json")
    ) | Where-Object { Test-Path -LiteralPath $_ }

    $latestInput = $buildInputs |
        ForEach-Object {
            if ((Get-Item -LiteralPath $_).PSIsContainer) {
                Get-ChildItem -LiteralPath $_ -Recurse -File
            }
            else {
                Get-Item -LiteralPath $_
            }
        } |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1

    if ($latestInput -and $latestInput.LastWriteTimeUtc -gt (Get-Item -LiteralPath $DistIndex).LastWriteTimeUtc) {
        $needsBuild = $true
    }
}

if ($needsBuild -and -not $SkipBuild) {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        throw "The frontend must be built, but Node.js/npm was not found. Install Node.js first."
    }
    Push-Location -LiteralPath $WebDir
    try {
        if (-not (Test-Path -LiteralPath (Join-Path $WebDir "node_modules"))) {
            Write-Host "Installing frontend dependencies..." -ForegroundColor Cyan
            & npm install
            if ($LASTEXITCODE -ne 0) {
                throw "Frontend dependency installation failed."
            }
        }
        Write-Host "Building the frontend..." -ForegroundColor Cyan
        & npm run build
        if ($LASTEXITCODE -ne 0) {
            throw "Frontend build failed."
        }
    }
    finally {
        Pop-Location
    }
}
elseif (-not (Test-Path -LiteralPath $DistIndex)) {
    throw "Frontend build output is missing; -SkipBuild cannot be used."
}

$env:ANALYSE_AGENT_PORT = "$Port"
if (-not $NoBrowser) {
    Start-Job -ScriptBlock {
        param($TargetUrl)
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            try {
                $health = Invoke-RestMethod -Uri "$TargetUrl/api/health" -TimeoutSec 1
                if ($health.status -eq "ok") {
                    Start-Process $TargetUrl
                    return
                }
            }
            catch {
                Start-Sleep -Milliseconds 500
            }
        }
    } -ArgumentList $Url | Out-Null
}

Write-Host "Starting Analyse Agent at $Url" -ForegroundColor Green
Write-Host "Press Ctrl+C to stop the service." -ForegroundColor DarkGray
& $PythonPath -m uvicorn main:app --host 127.0.0.1 --port $Port
exit $LASTEXITCODE

[CmdletBinding()]
param(
    [string]$OpenAiApiKey,
    [string]$PostgresPassword,
    [string]$ServiceToken,
    [string]$Organization
)

$ErrorActionPreference = "Stop"
$region = "iad"
$agentApp = "big-step-01-agent"
$frontendApp = "big-step-01-frontend"
$ticketsApp = "big-step-01-tickets"
$postgresApp = "big-step-01-postgres"

function Get-DotEnvValue([string]$Name) {
    $path = Join-Path $PSScriptRoot ".env"
    if (-not (Test-Path $path)) { return $null }
    $line = Get-Content $path | Where-Object { $_ -match "^$Name=" } | Select-Object -First 1
    if (-not $line) { return $null }
    return $line.Split("=", 2)[1]
}

function New-Secret {
    $bytes = New-Object byte[] 24
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    } finally {
        $generator.Dispose()
    }
    return [BitConverter]::ToString($bytes).Replace("-", "")
}

function Ensure-App([string]$Name) {
    $apps = @(& fly apps list --json | ConvertFrom-Json)
    if ($apps.Name -contains $Name) { return }
    if ($Organization) {
        & fly apps create $Name --org $Organization
    } else {
        & fly apps create $Name
    }
}

function Ensure-Volume {
    $volumes = @(& fly volumes list --app $postgresApp --json | ConvertFrom-Json)
    if ($volumes.Name -contains "postgres_data") { return }
    & fly volumes create postgres_data --app $postgresApp --region $region --size 1 --yes
}

if (-not $OpenAiApiKey) { $OpenAiApiKey = $env:OPENAI_API_KEY }
if (-not $OpenAiApiKey) { $OpenAiApiKey = Get-DotEnvValue "OPENAI_API_KEY" }
if (-not $OpenAiApiKey) {
    throw "Defina OPENAI_API_KEY no .env, no ambiente ou use -OpenAiApiKey."
}
if (-not $PostgresPassword) { $PostgresPassword = $env:POSTGRES_PASSWORD }
if (-not $PostgresPassword) { $PostgresPassword = Get-DotEnvValue "POSTGRES_PASSWORD" }
if (-not $PostgresPassword) { $PostgresPassword = New-Secret }
if (-not $ServiceToken) { $ServiceToken = $env:SERVICE_TOKEN }
if (-not $ServiceToken) { $ServiceToken = Get-DotEnvValue "SERVICE_TOKEN" }
if (-not $ServiceToken) { $ServiceToken = New-Secret }

$openAiBaseUrl = $env:OPENAI_BASE_URL
if (-not $openAiBaseUrl) { $openAiBaseUrl = Get-DotEnvValue "OPENAI_BASE_URL" }
if (-not $openAiBaseUrl) { $openAiBaseUrl = "https://api.groq.com/openai/v1" }
$openAiModel = $env:OPENAI_MODEL
if (-not $openAiModel) { $openAiModel = Get-DotEnvValue "OPENAI_MODEL" }
if (-not $openAiModel) { $openAiModel = "openai/gpt-oss-20b" }
$inputPrice = $env:OPENAI_INPUT_USD_PER_1M
if (-not $inputPrice) { $inputPrice = Get-DotEnvValue "OPENAI_INPUT_USD_PER_1M" }
if (-not $inputPrice) { $inputPrice = "0.075" }
$outputPrice = $env:OPENAI_OUTPUT_USD_PER_1M
if (-not $outputPrice) { $outputPrice = Get-DotEnvValue "OPENAI_OUTPUT_USD_PER_1M" }
if (-not $outputPrice) { $outputPrice = "0.30" }
$toolTimeout = $env:TOOL_TIMEOUT_SECONDS
if (-not $toolTimeout) { $toolTimeout = Get-DotEnvValue "TOOL_TIMEOUT_SECONDS" }
if (-not $toolTimeout) { $toolTimeout = "4" }
$toolRetries = $env:TOOL_RETRY_ATTEMPTS
if (-not $toolRetries) { $toolRetries = Get-DotEnvValue "TOOL_RETRY_ATTEMPTS" }
if (-not $toolRetries) { $toolRetries = "3" }

$escapedPassword = [uri]::EscapeDataString($PostgresPassword)
$databaseUrl = "postgresql://tickets:$escapedPassword@$postgresApp.internal:5432/tickets"

Ensure-App $postgresApp
Ensure-Volume
& fly secrets set "POSTGRES_PASSWORD=$PostgresPassword" --app $postgresApp
& fly deploy --config fly.postgres.toml --app $postgresApp --ha=false

Ensure-App $ticketsApp
& fly secrets set "DATABASE_URL=$databaseUrl" "SERVICE_TOKEN=$ServiceToken" --app $ticketsApp
& fly deploy --config fly.tickets.toml --app $ticketsApp --ha=false

Ensure-App $agentApp
& fly secrets set "OPENAI_API_KEY=$OpenAiApiKey" "TICKET_SERVICE_TOKEN=$ServiceToken" "OPENAI_BASE_URL=$openAiBaseUrl" "OPENAI_MODEL=$openAiModel" "OPENAI_INPUT_USD_PER_1M=$inputPrice" "OPENAI_OUTPUT_USD_PER_1M=$outputPrice" "TOOL_TIMEOUT_SECONDS=$toolTimeout" "TOOL_RETRY_ATTEMPTS=$toolRetries" --app $agentApp
& fly deploy --config fly.agent.toml --app $agentApp --ha=false

Ensure-App $frontendApp
& fly deploy --config fly.frontend.toml --app $frontendApp --ha=false

& fly status --app $postgresApp
& fly status --app $ticketsApp
& fly status --app $agentApp
& fly status --app $frontendApp






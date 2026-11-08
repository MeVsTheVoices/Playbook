$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

kubectl --context=docker-desktop get nodes --request-timeout=10s
if ($LASTEXITCODE -ne 0) { throw 'Enable Kubernetes in Docker Desktop and wait for a Ready node.' }

$config = kubectl config view --context=docker-desktop --minify --flatten --raw -o json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Could not export the Docker Desktop kubeconfig.' }
$endpoint = [Uri]$config.clusters[0].cluster.server
$tlsName = $config.clusters[0].cluster.'tls-server-name'
if (-not $tlsName) { $tlsName = $endpoint.Host }
if ($endpoint.Host -notin @('kubernetes.docker.internal', 'localhost', '127.0.0.1')) {
    throw "Unexpected Docker Desktop API hostname: $($endpoint.Host)"
}
$directory = Join-Path $root '.local'
New-Item -ItemType Directory -Force $directory | Out-Null
$path = Join-Path $directory 'kubeconfig'
[IO.File]::WriteAllText($path, ($config | ConvertTo-Json -Depth 30), [Text.UTF8Encoding]::new($false))

kubectl --kubeconfig=$path config set-cluster $config.clusters[0].name `
    --server="https://host.docker.internal:$($endpoint.Port)" --tls-server-name="$tlsName"
if ($LASTEXITCODE -ne 0) { throw 'Could not configure the container API endpoint.' }

docker compose -f compose.yaml -f compose.kubernetes.yaml up --build -d
if ($LASTEXITCODE -ne 0) { throw 'Could not start the Kubernetes-connected callback site.' }
docker compose -f compose.yaml -f compose.kubernetes.yaml exec -T callback-site kubectl get nodes --request-timeout=10s
if ($LASTEXITCODE -ne 0) { throw 'The callback container cannot authenticate to the Docker Desktop cluster.' }

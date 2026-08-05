# Installateur gaiverland-dl — Windows (clic droit > Exécuter avec PowerShell).
Set-Location $PSScriptRoot
Write-Host "-- gaiverland-dl : installation --"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) { Write-Host "Python 3 manquant : https://www.python.org/downloads/ (coche Add to PATH)"; exit 1 }
& $py.Source -m pip install --user -q -U yt-dlp
& $py.Source -c "import yt_dlp" ; if ($LASTEXITCODE -ne 0) { Write-Host "yt-dlp impossible a installer"; exit 1 }
Write-Host "yt-dlp : ok"
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) { Write-Host "ffmpeg : ok" }
else { Write-Host "ATTENTION ffmpeg manquant -> winget install Gyan.FFmpeg (puis rouvre le terminal)" }
if (-not (Test-Path config.json)) {
  $hote = Read-Host "Hote [https://gaiverland.gaiver-it.fr]"
  if (-not $hote) { $hote = "https://gaiverland.gaiver-it.fr" }
  $jeton = Read-Host "Jeton de la regie (le k= de ton lien /regie)"
  @{hote=$hote; jeton=$jeton} | ConvertTo-Json | Set-Content -Encoding UTF8 config.json
  Write-Host "config.json ecrit"
} else { Write-Host "config.json deja present, conserve" }
Write-Host ""
Write-Host "Termine. Utilisation :  python gaiverland-dl.py --liste"

param(
    [string]$Image,
    [string]$OutputPrefix
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Application]::EnableVisualStyles()

try {
    if (-not $Image) {
        $source = [Windows.Forms.OpenFileDialog]::new()
        try {
            $source.Title = '뉴윅 도표로 변환할 JPG 선택'
            $source.Filter = 'JPEG 이미지 (*.jpg;*.jpeg)|*.jpg;*.jpeg'
            if ($source.ShowDialog() -ne 'OK') { exit 0 }
            $Image = $source.FileName
        } finally {
            $source.Dispose()
        }
    }
    $Image = (Resolve-Path -LiteralPath $Image).Path

    if (-not $OutputPrefix) {
        $destination = [Windows.Forms.SaveFileDialog]::new()
        try {
            $destination.Title = '뉴윅 도표와 문자열 저장'
            $destination.InitialDirectory = [IO.Path]::GetDirectoryName($Image)
            $destination.FileName = [IO.Path]::GetFileNameWithoutExtension($Image) + '_Newick.png'
            $destination.Filter = '뉴윅 도표 (*.png)|*.png'
            $destination.DefaultExt = 'png'
            $destination.OverwritePrompt = $false
            if ($destination.ShowDialog() -ne 'OK') { exit 0 }
            if ([IO.Path]::GetExtension($destination.FileName) -ine '.png') {
                throw '도표 파일 이름을 .png로 끝내세요.'
            }
            $OutputPrefix = $destination.FileName.Substring(0, $destination.FileName.Length - 4)
        } finally {
            $destination.Dispose()
        }
    }
    $png = "$OutputPrefix.png"
    $nwk = "$OutputPrefix.nwk"
    if ((Test-Path -LiteralPath $png) -or (Test-Path -LiteralPath $nwk)) {
        $answer = [Windows.Forms.MessageBox]::Show(
            "같은 이름의 PNG 또는 NWK가 있습니다. 덮어쓸까요?",
            '뉴윅 저장', 'YesNo', 'Question'
        )
        if ($answer -ne 'Yes') { exit 0 }
    }
    $python = (Get-Command 'python.exe').Source
    Write-Host '뉴윅 도표 계산 중…'
    & $python (Join-Path $PSScriptRoot 'feb_newick.py') $Image $OutputPrefix
    if ($LASTEXITCODE -ne 0) { throw "뉴윅 변환 실패 (종료 코드 $LASTEXITCODE)" }
    Write-Host "PNG: $png"
    Write-Host "NWK: $nwk"
    try { Invoke-Item -LiteralPath $png } catch { Write-Warning $_.Exception.Message }
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    [Windows.Forms.MessageBox]::Show($_.Exception.Message, '뉴윅 변환 실패', 'OK', 'Error') | Out-Null
    exit 1
}

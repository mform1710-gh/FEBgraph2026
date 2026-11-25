param([string]$OutputDirectory = $PSScriptRoot)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$engine = Join-Path $PSScriptRoot 'feb_image.py'
$scratch = Join-Path ([IO.Path]::GetTempPath()) ('feb-' + [guid]::NewGuid())
[IO.Directory]::CreateDirectory($scratch) | Out-Null
$previewPath = Join-Path $scratch 'preview.png'
$fieldPath = Join-Path $scratch 'field.npz'
$overlayFiles = @('base', 'tint', 'vectors', 'white', 'yellow', 'edges', 'nodes', 'names')
$newickPrefix = Join-Path $scratch 'newick'
$newickPdf = "$newickPrefix.pdf"
$newickText = "$newickPrefix.nwk"
$state = @{
    Source = $null
    Process = $null
    Output = $null
    ErrorOutput = $null
    Zoom = 1.0
    LayersReady = $false
    AppliedNode = $null
    RunOptions = $null
    ReuseField = $false
    SaveDirectory = [IO.Path]::GetFullPath($OutputDirectory)
}

$form = [Windows.Forms.Form]::new()
$form.Text = 'FEB 평면도'
$form.ClientSize = [Drawing.Size]::new(1240, 650)
$form.MinimumSize = [Drawing.Size]::new(820, 510)
$form.StartPosition = 'CenterScreen'
$form.Font = [Drawing.Font]::new('Malgun Gothic', 9)

function Add-Control ($control, $caption, $x, $y, $width, $height) {
    $control.Text = $caption
    $control.SetBounds($x, $y, $width, $height)
    $form.Controls.Add($control)
    $control
}

$choose = Add-Control ([Windows.Forms.Button]::new()) 'JPG 선택' 14 12 94 30
$run = Add-Control ([Windows.Forms.Button]::new()) 'FEB 실행' 116 12 94 30
$save = Add-Control ([Windows.Forms.Button]::new()) '결과 저장' 218 12 94 30
$filename = Add-Control ([Windows.Forms.Label]::new()) '선택된 JPG 없음' 326 18 480 25
$filename.AutoEllipsis = $true
$filename.Anchor = 'Top, Left, Right'
$run.Enabled = $save.Enabled = $false

$graph = Add-Control ([Windows.Forms.CheckBox]::new()) '그래프' 14 52 180 24
$graph.Checked = $true
$closeLoops = Add-Control ([Windows.Forms.CheckBox]::new()) '끝단 순환 연결' 405 52 150 24
$closeLoops.Checked = $true
$graph.Add_CheckedChanged({ $closeLoops.Enabled = $graph.Checked })
$contours = Add-Control ([Windows.Forms.CheckBox]::new()) '흰색 등고선' 14 78 180 24
$vectors = Add-Control ([Windows.Forms.CheckBox]::new()) 'EB 벡터' 14 104 180 24
$vectors.Checked = $true
$dark = Add-Control ([Windows.Forms.CheckBox]::new()) '어두운 영역을 ground로' 14 156 180 24

Add-Control ([Windows.Forms.Label]::new()) '노드 EB' 202 55 116 22 | Out-Null
Add-Control ([Windows.Forms.Label]::new()) '노란 강조 EB' 202 81 116 22 | Out-Null
Add-Control ([Windows.Forms.Label]::new()) '화살표 배율' 202 107 116 22 | Out-Null
Add-Control ([Windows.Forms.Label]::new()) '화살표 밀도' 202 133 116 22 | Out-Null
Add-Control ([Windows.Forms.Label]::new()) '밝기 기준' 202 159 116 22 | Out-Null
$node = Add-Control ([Windows.Forms.TextBox]::new()) '0.18' 320 52 70 24
$highlight = Add-Control ([Windows.Forms.TextBox]::new()) '' 320 78 70 24
$scale = Add-Control ([Windows.Forms.TrackBar]::new()) '' 315 104 145 24
$scale.AccessibleName = '화살표 배율'
$scale.AutoSize = $false
$scale.TickStyle = 'None'
$scale.Minimum = 1
$scale.Maximum = 100
$scale.LargeChange = 10
$scale.Value = 1
$scaleValue = Add-Control ([Windows.Forms.Label]::new()) '0.1×' 466 107 70 22
$density = Add-Control ([Windows.Forms.TrackBar]::new()) '' 315 130 145 24
$density.AccessibleName = '화살표 밀도'
$density.AutoSize = $false
$density.TickStyle = 'None'
$density.Minimum = 4
$density.Maximum = 100
$density.LargeChange = 10
$density.Value = 100
$densityValue = Add-Control ([Windows.Forms.Label]::new()) '100' 466 133 70 22
$scale.Add_ValueChanged({ $scaleValue.Text = '{0:0.0}×' -f ($scale.Value / 10) })
$density.Add_ValueChanged({ $densityValue.Text = [string]$density.Value })
$threshold = Add-Control ([Windows.Forms.TextBox]::new()) '128' 320 156 70 24
$despeckle = Add-Control ([Windows.Forms.CheckBox]::new()) '노이즈 제거' 405 156 95 24
$noisePixels = Add-Control ([Windows.Forms.NumericUpDown]::new()) '' 500 156 47 24
$noisePixels.AccessibleName = '제거할 노이즈의 최대 픽셀 수'
$noisePixels.Minimum = 1
$noisePixels.Maximum = 100
$noisePixels.Value = 8
$noisePixels.Enabled = $true
$despeckle.Checked = $true
$despeckle.Add_CheckedChanged({ $noisePixels.Enabled = $despeckle.Checked })

$view = Add-Control ([Windows.Forms.Panel]::new()) '' 14 190 710 422
$view.Anchor = 'Top, Bottom, Left, Right'
$view.AutoScroll = $true
$view.BackColor = [Drawing.Color]::FromArgb(34, 39, 44)
$preview = [Windows.Forms.PictureBox]::new()
$preview.SizeMode = 'StretchImage'
$view.Controls.Add($preview)
$zoomIn = Add-Control ([Windows.Forms.Button]::new()) '확대 +' 748 190 58 30
$zoomOut = Add-Control ([Windows.Forms.Button]::new()) '축소 −' 748 227 58 30
$zoomFit = Add-Control ([Windows.Forms.Button]::new()) '맞춤' 748 264 58 30
$zoomLabel = Add-Control ([Windows.Forms.Label]::new()) '' 748 303 58 22
$showNames = Add-Control ([Windows.Forms.CheckBox]::new()) '이름' 731 334 83 24
$showYellow = Add-Control ([Windows.Forms.CheckBox]::new()) '노란 EB선' 731 359 83 24
$showNodes = Add-Control ([Windows.Forms.CheckBox]::new()) '노드' 731 384 83 24
$showEdges = Add-Control ([Windows.Forms.CheckBox]::new()) '에지' 731 409 83 24
$showWhite = Add-Control ([Windows.Forms.CheckBox]::new()) '흰 EB선' 731 434 83 24
$showWhite.Anchor = 'Top, Right'
$showWhite.Checked = $contours.Checked
$showWhite.Add_CheckedChanged({
    if ($contours.Checked -ne $showWhite.Checked) { $contours.Checked = $showWhite.Checked }
})
$contours.Add_CheckedChanged({
    if ($showWhite.Checked -ne $contours.Checked) { $showWhite.Checked = $contours.Checked }
})
foreach ($control in @($showNames, $showYellow, $showNodes, $showEdges)) {
    $control.Anchor = 'Top, Right'
    $control.Checked = $true
}
foreach ($control in @($zoomIn, $zoomOut, $zoomFit, $zoomLabel)) {
    $control.Anchor = 'Top, Right'
}
$zoomIn.Enabled = $zoomOut.Enabled = $zoomFit.Enabled = $false
$nodeList = Add-Control ([Windows.Forms.ListView]::new()) '' 820 190 406 422
$nodeList.Anchor = 'Top, Bottom, Right'
$nodeList.View = 'Details'
$nodeList.FullRowSelect = $true
$nodeList.GridLines = $true
$nodeList.AccessibleName = '노드 최저 EB와 에지 접점 EB 비교'
[void]$nodeList.Columns.Add('노드', 94)
[void]$nodeList.Columns.Add('최저 EB', 66, [Windows.Forms.HorizontalAlignment]::Right)
[void]$nodeList.Columns.Add('에지 / 연결 노드', 128)
[void]$nodeList.Columns.Add('접점 EB', 86)
[void]$nodeList.Columns.Add('상대 접점 EB', 94)
[void]$nodeList.Columns.Add('차이 (뉴윅)', 96)
[void]$nodeList.Columns.Add('접점 x, y', 86)
$status = Add-Control ([Windows.Forms.Label]::new()) 'JPG 평면도를 선택하세요.' 14 620 1212 24
$status.Anchor = 'Bottom, Left, Right'

function Update-Preview {
    if (-not $preview.Image) { return }
    $fit = [Math]::Min($view.ClientSize.Width / $preview.Image.Width,
                       $view.ClientSize.Height / $preview.Image.Height)
    $width = [Math]::Max(1, [int]($preview.Image.Width * $fit * $state.Zoom))
    $height = [Math]::Max(1, [int]($preview.Image.Height * $fit * $state.Zoom))
    $view.AutoScrollPosition = [Drawing.Point]::Empty
    $x = [Math]::Max(0, [int](($view.ClientSize.Width - $width) / 2))
    $y = [Math]::Max(0, [int](($view.ClientSize.Height - $height) / 2))
    $preview.SetBounds($x, $y, $width, $height)
    $zoomLabel.Text = '{0:P0}' -f $state.Zoom
    $zoomIn.Enabled = $state.Zoom -lt 8
    $zoomOut.Enabled = $state.Zoom -gt 0.25
    $zoomFit.Enabled = $state.Zoom -ne 1
}

$view.Add_Resize({ Update-Preview })
$zoomIn.Add_Click({ $state.Zoom = [Math]::Min(8.0, $state.Zoom * 1.25); Update-Preview })
$zoomOut.Add_Click({ $state.Zoom = [Math]::Max(0.25, $state.Zoom / 1.25); Update-Preview })
$zoomFit.Add_Click({ $state.Zoom = 1.0; Update-Preview })

function Show-Image ($path, [switch]$KeepZoom) {
    $image = [Drawing.Image]::FromFile($path)
    try {
        $copy = [Drawing.Bitmap]::new($image)
    } finally {
        $image.Dispose()
    }
    if ($preview.Image) { $preview.Image.Dispose() }
    $preview.Image = $copy
    if (-not $KeepZoom) { $state.Zoom = 1.0 }
    Update-Preview
}

function Refresh-Overlays {
    $visible = @('base')
    if ($showNodes.Checked) { $visible += 'tint' }
    if ($vectors.Checked) { $visible += 'vectors' }
    if ($showWhite.Checked) { $visible += 'white' }
    if ($showYellow.Checked) { $visible += 'yellow' }
    if ($showEdges.Checked) { $visible += 'edges' }
    if ($showNodes.Checked) { $visible += 'nodes' }
    if ($showNames.Checked) { $visible += 'names' }
    $base = [Drawing.Bitmap]::new((Join-Path $scratch 'base.png'))
    try {
        $merged = [Drawing.Bitmap]::new($base.Width, $base.Height)
        try {
            $graphics = [Drawing.Graphics]::FromImage($merged)
            try {
                foreach ($layer in $visible) {
                    $bitmap = [Drawing.Bitmap]::new((Join-Path $scratch "$layer.png"))
                    try { $graphics.DrawImageUnscaled($bitmap, 0, 0) }
                    finally { $bitmap.Dispose() }
                }
            } finally { $graphics.Dispose() }
            $merged.Save($previewPath, [Drawing.Imaging.ImageFormat]::Png)
        } finally { $merged.Dispose() }
    } finally { $base.Dispose() }
    $scroll = $view.AutoScrollPosition
    Show-Image $previewPath -KeepZoom
    $view.AutoScrollPosition = [Drawing.Point]::new(-$scroll.X, -$scroll.Y)
}

foreach ($control in @($showNames, $showYellow, $showNodes, $showEdges, $vectors, $showWhite)) {
    $control.Add_CheckedChanged({
        if ($state.LayersReady -and -not $state.Process) {
            try { Refresh-Overlays } catch { Show-Error $_.Exception.Message }
        }
    })
}

function Show-Error ($message) {
    [Windows.Forms.MessageBox]::Show($form, $message, 'FEB', 'OK', 'Error') | Out-Null
}

$timer = [Windows.Forms.Timer]::new()
$timer.Interval = 150
$nodeRefresh = [Windows.Forms.Timer]::new()
$nodeRefresh.Interval = 500
$timer.Add_Tick({
    if (-not $state.Process.HasExited) { return }
    $timer.Stop()
    try {
        $state.Process.WaitForExit()
        $code = $state.Process.ExitCode
        $output = $state.Output.GetAwaiter().GetResult()
        $errors = $state.ErrorOutput.GetAwaiter().GetResult()
        if ($code -ne 0) {
            $message = $errors.Trim() -split '\r?\n' | Select-Object -Last 1
            if (-not $message) { $message = "Python 종료 코드 $code" }
            throw $message
        }
        if (-not $state.ReuseField) { $state.Zoom = 1.0 }
        Refresh-Overlays
        $nodeList.Items.Clear()
        $nodeData = Get-Content -LiteralPath (Join-Path $scratch 'nodes.json') -Raw -Encoding UTF8 |
            ConvertFrom-Json
        foreach ($entry in $nodeData) {
            $item = [Windows.Forms.ListViewItem]::new([string]$entry.name)
            [void]$item.SubItems.Add(([double]$entry.eb).ToString(
                'F4', [Globalization.CultureInfo]::InvariantCulture))
            [void]$item.SubItems.Add([string]$entry.connection)
            foreach ($value in @($entry.contact, $entry.other, $entry.difference)) {
                $text = if ($null -eq $value) { '' } else {
                    ([double]$value).ToString('F6', [Globalization.CultureInfo]::InvariantCulture)
                }
                [void]$item.SubItems.Add($text)
            }
            [void]$item.SubItems.Add([string]$entry.pixel)
            [void]$nodeList.Items.Add($item)
        }
        $state.LayersReady = $true
        $save.Enabled = $true
        $summary = ($output -split '\r?\n') |
            Where-Object { $_ -match '^(EB <=|FEB range:|Streets:)' }
        $resultText = if ((Test-Path -LiteralPath $newickPdf) -and
                          (Test-Path -LiteralPath $newickText)) {
            'FEB · 뉴윅 계산 완료'
        } elseif ($output -match 'Newick unavailable: ([^\r\n]+)') {
            "FEB 계산 완료 · 뉴윅 생성 불가 ($($Matches[1]))"
        } else {
            'FEB 계산 완료'
        }
        $status.Text = (@($resultText) + $summary) -join ' · '
    } catch {
        $status.Text = 'FEB 계산 실패'
        Show-Error $_.Exception.Message
    } finally {
        $state.Process.Dispose()
        $state.Process = $state.Output = $state.ErrorOutput = $null
        $choose.Enabled = $run.Enabled = $true
        if ($state.LayersReady -and $node.Text.Trim() -ne $state.AppliedNode) {
            $nodeRefresh.Start()
        }
    }
})

$choose.Add_Click({
    $dialog = [Windows.Forms.OpenFileDialog]::new()
    $dialog.Title = 'JPG 평면도 선택'
    $dialog.Filter = 'JPEG 이미지 (*.jpg;*.jpeg)|*.jpg;*.jpeg'
    try {
        if ($dialog.ShowDialog($form) -ne 'OK') { return }
        Show-Image $dialog.FileName
        $nodeRefresh.Stop()
        $state.LayersReady = $false
        $nodeList.Items.Clear()
        $state.Source = $dialog.FileName
        $filename.Text = [IO.Path]::GetFileName($state.Source)
        $status.Text = '원본 미리보기 · FEB 실행을 누르세요.'
        $run.Enabled = $true
        $save.Enabled = $false
    } catch {
        Show-Error $_.Exception.Message
    } finally {
        $dialog.Dispose()
    }
})

function Start-FebRun ([switch]$Reuse) {
    if (-not $state.Source -or $state.Process) { return }
    $nodeValue = $node.Text.Trim()
    $number = 0.0
    if (-not [double]::TryParse($nodeValue, [Globalization.NumberStyles]::Float,
                                [Globalization.CultureInfo]::InvariantCulture, [ref]$number) -or
        -not ($number -ge 0 -and $number -le 1)) {
        if (-not $Reuse) { Show-Error '노드 EB는 0부터 1 사이로 입력하세요.' }
        return
    }
    if ($Reuse) {
        $options = $state.RunOptions
    } else {
        $options = @("--threshold=$($threshold.Text.Trim())")
        if ($dark.Checked) { $options += '--dark-ground' }
        if ($despeckle.Checked) { $options += "--despeckle=$([int]$noisePixels.Value)" }
        if ($graph.Checked) {
            $options += '--graph'
            if ($closeLoops.Checked) { $options += '--close-loops' }
        }
        $options += '--contours'
        $factor = ($scale.Value / 10).ToString('0.0', [Globalization.CultureInfo]::InvariantCulture)
        $longSide = [Math]::Max($preview.Image.Width, $preview.Image.Height)
        $step = [Math]::Max(1, [int][Math]::Round($longSide / $density.Value))
        $options += '--vectors', "--vector-scale=$factor", "--vector-step=$step"
        if ($highlight.Text.Trim()) { $options += "--highlight-eb=$($highlight.Text.Trim())" }
        if ($options | Where-Object { $_ -notmatch '^--[a-z-]+(=[0-9.eE+\-]+)?$' }) {
            Show-Error '옵션을 숫자로 입력하세요. 예: 0.18'
            return
        }
    }
    try {
        $python = (Get-Command python.exe -ErrorAction Stop).Source
        $arguments = @($engine, $state.Source, $previewPath) | ForEach-Object { '"' + $_ + '"' }
        $baseOptions = $options
        $options += '--overlay-dir', ('"' + $scratch + '"')
        $options += "--node-eb=$nodeValue"
        $fieldFlag = if ($Reuse) { '--reuse-field' } else { '--write-field' }
        $options += $fieldFlag, ('"' + $fieldPath + '"')
        [IO.File]::Delete($newickPdf)
        [IO.File]::Delete($newickText)
        if ($baseOptions -contains '--graph') {
            $options += '--newick-prefix', ('"' + $newickPrefix + '"'), '--newick-format', 'pdf'
        }
        $startInfo = [Diagnostics.ProcessStartInfo]::new($python, (($arguments + $options) -join ' '))
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $startInfo.StandardOutputEncoding = [Text.Encoding]::UTF8
        $startInfo.StandardErrorEncoding = [Text.Encoding]::UTF8
        $startInfo.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
        $state.Process = [Diagnostics.Process]::Start($startInfo)
        $state.LayersReady = $false
        $state.AppliedNode = $nodeValue
        $state.RunOptions = $baseOptions
        $state.ReuseField = [bool]$Reuse
        $state.Output = $state.Process.StandardOutput.ReadToEndAsync()
        $state.ErrorOutput = $state.Process.StandardError.ReadToEndAsync()
        $choose.Enabled = $run.Enabled = $save.Enabled = $false
        $status.Text = if ($Reuse) { 'EB 등고선과 그래프 갱신 중…' } else { 'FEB 계산 중…' }
        $timer.Start()
    } catch {
        Show-Error $_.Exception.Message
    }
}

$run.Add_Click({ Start-FebRun })
$node.Add_TextChanged({
    $nodeRefresh.Stop()
    $nodeRefresh.Start()
})
$nodeRefresh.Add_Tick({
    $nodeRefresh.Stop()
    if ($state.LayersReady -and -not $state.Process -and
        $node.Text.Trim() -ne $state.AppliedNode) {
        Start-FebRun -Reuse
    }
})

function Select-OutputFolder {
    $dialog = [Windows.Forms.FolderBrowserDialog]::new()
    $dialog.Description = 'FEB 이미지와 뉴윅 파일을 저장할 폴더를 선택하세요.'
    $dialog.SelectedPath = $state.SaveDirectory
    $dialog.ShowNewFolderButton = $true
    try {
        if ($dialog.ShowDialog($form) -eq 'OK') { return $dialog.SelectedPath }
    } finally {
        $dialog.Dispose()
    }
}

function Save-Results ($directory) {
    $name = [IO.Path]::GetFileNameWithoutExtension($state.Source) + '_feb'
    $destination = Join-Path $directory "$name.png"
    $prefix = Join-Path $directory ($name + '_Newick')
    $treePdf = "$prefix.pdf"
    $treeText = "$prefix.nwk"
    $includeNewick = (Test-Path -LiteralPath $newickPdf) -and
                     (Test-Path -LiteralPath $newickText)
    if ((Test-Path -LiteralPath $destination) -or
        ($includeNewick -and ((Test-Path -LiteralPath $treePdf) -or
                             (Test-Path -LiteralPath $treeText)))) {
        $choice = [Windows.Forms.MessageBox]::Show(
            $form, '같은 이름의 결과 파일이 있습니다. 덮어쓸까요?', '결과 저장', 'YesNo', 'Question'
        )
        if ($choice -ne 'Yes') { return }
    }
    [IO.File]::Copy($previewPath, $destination, $true)
    if ($includeNewick) {
        [IO.File]::Copy($newickPdf, $treePdf, $true)
        [IO.File]::Copy($newickText, $treeText, $true)
        $status.Text = 'FEB PNG · 뉴윅 PDF · NWK 저장됨'
    } else {
        $status.Text = "저장됨 · $([IO.Path]::GetFileName($destination))"
    }
    $state.SaveDirectory = $directory
}

$save.Add_Click({
    try {
        $directory = Select-OutputFolder
        if ($directory) { Save-Results $directory }
    } catch {
        Show-Error $_.Exception.Message
    }
})

try {
    [void]$form.ShowDialog()
} finally {
    $timer.Dispose()
    $nodeRefresh.Dispose()
    if ($state.Process) {
        if (-not $state.Process.HasExited) {
            $state.Process.Kill()
            $state.Process.WaitForExit()
        }
        $state.Process.Dispose()
    }
    if ($preview.Image) { $preview.Image.Dispose() }
    $form.Dispose()
    [IO.File]::Delete($previewPath)
    [IO.File]::Delete($fieldPath)
    [IO.File]::Delete((Join-Path $scratch 'nodes.json'))
    [IO.File]::Delete($newickPdf)
    [IO.File]::Delete($newickText)
    foreach ($layer in $overlayFiles) { [IO.File]::Delete((Join-Path $scratch "$layer.png")) }
    [IO.Directory]::Delete($scratch)
}

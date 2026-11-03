# Rasterize exported prompt_toolkit cells, not a native terminal screenshot.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$font = [System.Drawing.Font]::new('Consolas',16,[System.Drawing.FontStyle]::Regular,[System.Drawing.GraphicsUnit]::Pixel)
$cjkFont = [System.Drawing.Font]::new('Microsoft YaHei',16,[System.Drawing.FontStyle]::Regular,[System.Drawing.GraphicsUnit]::Pixel)
$format = [System.Drawing.StringFormat]::GenericTypographic
try {
    Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.cells.json' | ForEach-Object {
        $frame = Get-Content -LiteralPath $_.FullName -Raw -Encoding utf8 | ConvertFrom-Json
        $bitmap = [System.Drawing.Bitmap]::new([int]($frame.columns*10+24),[int]($frame.rows*25+24))
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
        try {
            $graphics.Clear([System.Drawing.ColorTranslator]::FromHtml('#1e1f22'))
            $graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
            foreach($cell in $frame.cells) {
                $brush = [System.Drawing.SolidBrush]::new([System.Drawing.ColorTranslator]::FromHtml($cell.color))
                $face = if ([int][char]$cell.text[0] -gt 0x2E80) { $cjkFont } else { $font }
                $graphics.DrawString($cell.text,$face,$brush,[single]($cell.x*10+12),[single]($cell.y*25+12),$format)
                $brush.Dispose()
            }
            $bitmap.Save(($_.FullName -replace '\.cells\.json$','.png'),[System.Drawing.Imaging.ImageFormat]::Png)
        } finally {
            $graphics.Dispose()
            $bitmap.Dispose()
        }
    }
} finally {
    $font.Dispose()
    $cjkFont.Dispose()
}

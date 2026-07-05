param(
    [string]$RepositoryName = "pdf-workbench",
    [ValidateSet("private", "public")]
    [string]$Visibility = "private"
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "GitHub CLI (gh) が見つかりません。winget install --id GitHub.cli を実行してください。"
}
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python が見つかりません。Python 3.12 以降をインストールしてください。"
}

gh auth status | Out-Host

if (-not (Test-Path ".git")) {
    git init -b main
}

git add .
$hasChanges = -not [string]::IsNullOrWhiteSpace((git status --porcelain))
if ($hasChanges) {
    git commit -m "chore: initialize PDF Workbench"
}

$existingRemote = git remote get-url origin 2>$null
if (-not $existingRemote) {
    if ($Visibility -eq "private") {
        gh repo create $RepositoryName --private --source . --remote origin --push
    } else {
        gh repo create $RepositoryName --public --source . --remote origin --push
    }
} else {
    git push -u origin main
}

$labels = @(
    @{ Name = "type:feature"; Color = "1D76DB"; Description = "New functionality" },
    @{ Name = "type:bug"; Color = "D73A4A"; Description = "Defect or regression" },
    @{ Name = "type:chore"; Color = "C5DEF5"; Description = "Maintenance and build work" },
    @{ Name = "type:test"; Color = "BFD4F2"; Description = "Testing and quality work" },
    @{ Name = "priority:p0"; Color = "B60205"; Description = "Blocks the current milestone" },
    @{ Name = "priority:p1"; Color = "D93F0B"; Description = "High priority" },
    @{ Name = "priority:p2"; Color = "FBCA04"; Description = "Normal priority" },
    @{ Name = "area:build"; Color = "5319E7"; Description = "Packaging, CI, and releases" },
    @{ Name = "area:ui"; Color = "7057FF"; Description = "Desktop user interface" },
    @{ Name = "area:viewer"; Color = "006B75"; Description = "PDF viewing and navigation" },
    @{ Name = "area:core"; Color = "0E8A16"; Description = "Core architecture and document model" },
    @{ Name = "area:pages"; Color = "2CBE4E"; Description = "Page organization" },
    @{ Name = "area:annotations"; Color = "D4C5F9"; Description = "Annotations and markup" },
    @{ Name = "area:ocr"; Color = "0052CC"; Description = "OCR and scan processing" },
    @{ Name = "area:security"; Color = "B60205"; Description = "Redaction, sanitization, and encryption" },
    @{ Name = "area:optimize"; Color = "C2E0C6"; Description = "Compression and optimization" },
    @{ Name = "area:automation"; Color = "0E8A16"; Description = "Batch and CLI automation" },
    @{ Name = "area:forms"; Color = "F9D0C4"; Description = "AcroForm support" },
    @{ Name = "area:editing"; Color = "E99695"; Description = "Text and image editing" },
    @{ Name = "area:compare"; Color = "BFDADC"; Description = "Document comparison" },
    @{ Name = "area:quality"; Color = "D4C5F9"; Description = "Compatibility and regression testing" }
)
foreach ($label in $labels) {
    gh label create $label.Name --color $label.Color --description $label.Description --force | Out-Null
}

python scripts/create_github_issues.py
Write-Host "GitHub bootstrap completed."

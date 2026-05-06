# Script to fix GitHub Actions failures
# This script will:
# 1. Format the three non-conforming Python files with ruff
# 2. Fix the broken requirements_typing.txt dependency

Write-Host "🔧 Fixing GitHub Actions failures..." -ForegroundColor Green

# Step 1: Format the three Python files that ruff flagged
Write-Host "`n📝 Formatting Python files with ruff..." -ForegroundColor Cyan

ruff format custom_components/hsem/custom_sensors/working_mode_sensor.py
if ($LASTEXITCODE -ne 0) { throw "Failed to format working_mode_sensor.py" }

ruff format custom_components/hsem/time.py
if ($LASTEXITCODE -ne 0) { throw "Failed to format time.py" }

ruff format custom_components/hsem/utils/misc.py
if ($LASTEXITCODE -ne 0) { throw "Failed to format misc.py" }

Write-Host "✅ Code formatting complete!" -ForegroundColor Green

# Step 2: Fix requirements_typing.txt
Write-Host "`n📋 Fixing requirements_typing.txt..." -ForegroundColor Cyan

# Read the file and remove the broken types-all dependency
$typingReqs = Get-Content requirements_typing.txt
$updatedReqs = $typingReqs | Where-Object { $_ -notlike "*types-all*" }
$updatedReqs | Set-Content requirements_typing.txt

Write-Host "✅ Fixed requirements_typing.txt (removed types-all>=1.0.0)" -ForegroundColor Green

# Step 3: Verify changes
Write-Host "`n✔️  Verifying changes..." -ForegroundColor Cyan
Write-Host "`nrequirements_typing.txt content:"
Get-Content requirements_typing.txt

# Step 4: Show git status
Write-Host "`n📊 Git status:" -ForegroundColor Cyan
git status --short

# Step 5: Prepare commit
Write-Host "`n💾 Staging changes for commit..." -ForegroundColor Cyan
git add -A

Write-Host "`n✅ All fixes complete! Ready to commit." -ForegroundColor Green
Write-Host "`nNext step: git commit -m 'fix: format code and fix broken type-checking dependency`n`nFixes:` -ForegroundColor Yellow
Write-Host "- Format 3 Python files to meet ruff standards" -ForegroundColor Yellow
Write-Host "- Remove broken types-all>=1.0.0 dependency from requirements_typing.txt" -ForegroundColor Yellow
Write-Host "  types-all had transitive dependency on non-existent types-pkg-resources`n" -ForegroundColor Yellow

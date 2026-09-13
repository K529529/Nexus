# Day 10 FastAPI release fixture

This is the canonical small repository used for the Nexus V1 release demonstration. It is
deliberately independent from the Nexus package and has one reproducible defect: a premium order
of 100 or more receives 5 percent instead of the required 15 percent discount.

## Reset and validate

Export the tracked fixture files to a disposable location, initialize Git, and install its locked
development environment. Exporting from Git prevents ignored `.venv`, cache, or prior test output
from entering the copy:

```powershell
$fixture = Join-Path $env:TEMP "nexus-day10-fixture"
$archive = Join-Path $env:TEMP "nexus-day10-fixture.zip"
git archive --format=zip --output=$archive HEAD:examples/day10-fastapi-fixture
Expand-Archive -LiteralPath $archive -DestinationPath $fixture
Set-Location $fixture
git init
git add .
git -c user.name=Nexus -c user.email=nexus@example.invalid commit -m "fixture baseline"
uv sync --locked --dev --default-index https://pypi.org/simple
uv run pytest
```

Before the bug fix, `test_premium_large_order_discount` must fail. After the correct one-line
fix, all three tests must pass and `git diff --check` must succeed. Delete the disposable copy
and repeat the commands to reset deterministically.

See [TASKS.md](TASKS.md) for the five frozen demonstration categories. Nexus must use its normal
runtime, Plan/approval, Tool Runtime, validation, and diff flow; this fixture provides no second
execution path.

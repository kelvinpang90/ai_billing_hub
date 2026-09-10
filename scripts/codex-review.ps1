<#
.SYNOPSIS
    跑 Codex 独立审查（只读沙箱），可选把结果发回 PR 或设计 Issue。

.DESCRIPTION
    流程见 docs/WORKFLOW.md，审查清单见 scripts/review_checklist.md。

    Codex 在 read-only 沙箱里跑 —— 「审查者只读」是沙箱强制的，不是约定。
    输出实时打在终端上，同时落盘到 .codex-review-*.md（已被 .gitignore 忽略）。

    退出码：0 = APPROVE，1 = REQUEST_CHANGES，2 = 执行出错或判定缺失。

.PARAMETER Pr
    要审的 Pull Request 编号（实现闸门）。

.PARAMETER Issue
    要审的设计 Issue 编号（设计闸门）。

.PARAMETER Post
    审完把结果作为评论发到该 PR / Issue。不加则只在本地产出。

.EXAMPLE
    .\scripts\codex-review.ps1 -Pr 5
    审 PR #5，结果只留在本地，先自己看。

.EXAMPLE
    .\scripts\codex-review.ps1 -Pr 5 -Post
    审 PR #5 并把结果发到 PR 上。

.EXAMPLE
    .\scripts\codex-review.ps1 -Issue 12 -Post
    审设计 Issue #12 并回帖。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory, ParameterSetName = 'Implementation')]
    [int]$Pr,

    [Parameter(Mandatory, ParameterSetName = 'Design')]
    [int]$Issue,

    [switch]$Post
)

$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot

# codex 可能不在 PATH 里（装完没重开 shell），回退到已知安装位置
$codex = (Get-Command codex -ErrorAction SilentlyContinue).Source
if (-not $codex) {
    $fallback = Join-Path $env:LOCALAPPDATA 'Programs\OpenAI\Codex\bin\codex.exe'
    if (Test-Path $fallback) { $codex = $fallback }
}
if (-not $codex) {
    Write-Error "找不到 codex CLI。PATH 里没有，$env:LOCALAPPDATA\Programs\OpenAI\Codex\bin 下也没有。"
    exit 2
}

$slug = (& git -C $repo remote get-url origin) -replace '^.*github\.com[:/]', '' -replace '\.git$', ''

# Codex 通过 gh 取 diff，但通过 -C $repo 读工作区里的清单与文档。
# 两者不一致时，它会拿着 A 分支的 diff 去对照 B 分支的规则 —— 结论不可信。
if ($PSCmdlet.ParameterSetName -eq 'Implementation') {
    $prHead = (& gh pr view $Pr --repo $slug --json headRefOid --jq '.headRefOid' 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $prHead) {
        Write-Error "取不到 PR #$Pr 的 head SHA。PR 存在吗？gh 登录了吗？"
        exit 2
    }
    $localHead = (& git -C $repo rev-parse HEAD).Trim()
    if ($localHead -ne $prHead.Trim()) {
        Write-Error @"
本地工作区与 PR #$Pr 不一致，审查结论会不可信：
  PR head : $($prHead.Trim())
  本地 HEAD: $localHead

Codex 会用 gh 取 PR 的 diff，却从本地工作区读审查清单与文档。
两者不一致就会拿着一个分支的改动去对照另一个分支的规则。

先切到该 PR 的分支再跑：
  gh pr checkout $Pr
"@
        exit 2
    }
}

$common = @"
你是本仓库的独立审查者，不是开发者。

硬规则：
- 只读。不修改工作区任何文件，不 git add / commit / push，不合并任何东西。
- 只报你能指出具体位置和具体后果的问题。指不出后果的观感问题不要写。
- 不要重复 linter 和 CI 已经能抓的东西（格式、import 顺序、拼写）。
- 没有问题就写「无」，不要为了显得认真而凑数。

先读 scripts/review_checklist.md，那是本项目的审查清单。
需要上下文时读 docs/ARCHITECTURE.md（14 条不变量）、docs/adr/ 下的决策记录，
以及 docs/Acuven_Central_AI_Billing_Platform_Spec_v1.2.md 的相关章节。
"@

if ($PSCmdlet.ParameterSetName -eq 'Design') {
    $target = "Issue #$Issue"
    $out = Join-Path $repo ".codex-review-design-$Issue.md"
    $prompt = @"
$common

本次是【设计闸门】审查，目标是 $slug 的 Issue #$Issue。

步骤：
1. 执行 gh issue view $Issue --repo $slug 读设计文档全文。
2. 走 scripts/review_checklist.md 的「设计审查」一节，明确回答那五个问题。
3. 核对设计闸门的七条判定。

按以下格式输出，最后一行必须是判定：

## 🔍 CODEX REVIEW — 设计闸门

**结论**：<一句话>

### 五问
1. 哪个具体场景会破坏不变量？<答>
2. 哪条失败路径没有定义最终状态或恢复方式？<答>
3. 哪项正确性只靠应用代码、缺少数据库约束？<答>
4. 哪个新增分支没有对应测试？<答>
5. 设计是否与 spec 的具体章节冲突？<答>

### 阻断项
- `章节` — 问题 → 后果
（没有就写「无」）

### 闸门判定
逐条列出七条判定是否满足。

---
APPROVED: design v<该 Issue 顶部标注的版本号>
"@
    $rejected = 'REQUEST_CHANGES'
    $approvedPattern = '^APPROVED: design v'
} else {
    $target = "PR #$Pr"
    $out = Join-Path $repo ".codex-review-$Pr.md"
    $prompt = @"
$common

本次是【实现闸门】审查，目标是 $slug 的 PR #$Pr。

步骤：
1. 执行 gh pr view $Pr --repo $slug --json title,body 拿改动意图。
2. 执行 gh pr diff $Pr --repo $slug 拿完整 diff。
3. 走 scripts/review_checklist.md 的「实现审查」A–E 各节。
4. 若该 PR 关联了设计 Issue，核对实现是否忠于已批准的那一版设计。

按以下格式输出，最后一行必须是判定：

## 🔍 CODEX REVIEW

**结论**：<一句话>

### 阻断项
- `文件:行` — 问题 → 后果
（没有就写「无」）

### 建议项
- `文件:行` — 问题 → 建议
（没有就写「无」）

### 清单核对
- 不变量：<触碰了第几条，是否保住>
- DoD：<哪几条未满足>
- 测试：<改动引入的边界是否被覆盖>

---
VERDICT: APPROVE
"@
    $rejected = 'REQUEST_CHANGES'
    $approvedPattern = '^VERDICT: APPROVE$'
}

Write-Host "审查目标：$target（$slug）" -ForegroundColor Cyan
Write-Host "沙箱：read-only —— Codex 改不了任何文件" -ForegroundColor Cyan
Write-Host ("-" * 60)

& $codex exec -s read-only -C $repo -o $out $prompt
if ($LASTEXITCODE -ne 0) {
    Write-Error "codex exec 失败，退出码 $LASTEXITCODE"
    exit 2
}

if (-not (Test-Path $out)) {
    Write-Error "codex 没有产出结果文件：$out"
    exit 2
}

$verdictLine = (Get-Content $out | Where-Object { $_.Trim() } | Select-Object -Last 1).Trim()

Write-Host ("-" * 60)
Write-Host "结果已保存：$out"
Write-Host "判定：$verdictLine" -ForegroundColor Yellow

if ($Post) {
    if ($PSCmdlet.ParameterSetName -eq 'Design') {
        & gh issue comment $Issue --repo $slug --body-file $out
    } else {
        & gh pr comment $Pr --repo $slug --body-file $out
    }
    if ($LASTEXITCODE -ne 0) { Write-Error "发布评论失败"; exit 2 }
    Write-Host "已发布到 $target" -ForegroundColor Green
} else {
    Write-Host "未发布（加 -Post 可发到 $target）"
}

if ($verdictLine -match $approvedPattern) { exit 0 }
if ($verdictLine -match $rejected) { exit 1 }

Write-Warning "最后一行不是合法判定，Codex 可能没按格式输出。请人工看 $out"
exit 2

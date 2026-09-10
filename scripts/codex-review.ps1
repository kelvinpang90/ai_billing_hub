<#
.SYNOPSIS
    跑 Codex 独立审查（只读沙箱），可选把结果发回 PR 或设计 Issue。

.DESCRIPTION
    流程见 docs/WORKFLOW.md，审查清单见 scripts/review_checklist.md。

    职责切分：
      脚本  取材料（gh）→ 落盘 .codex-input-*.md
      Codex 只读本地文件做分析，不碰网络（read-only 沙箱挡住 gh 的配置）
      脚本  校验判定 → 发布评论

    退出码：0 = 通过，1 = 要改，2 = 执行出错或判定无效。
    这个契约是自动化的基础，不要破坏。

.PARAMETER Pr
    要审的 Pull Request 编号（实现闸门）。

.PARAMETER Issue
    要审的设计 Issue 编号（设计闸门）。

.PARAMETER Post
    审完把结果作为评论发到该 PR / Issue。**判定无效时不会发布。**

.EXAMPLE
    .\scripts\codex-review.ps1 -Pr 5
.EXAMPLE
    .\scripts\codex-review.ps1 -Pr 5 -Post
.EXAMPLE
    .\scripts\codex-review.ps1 -Issue 12 -Post
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
. (Join-Path $PSScriptRoot 'lib\ReviewVerdict.ps1')

# 不能用 Write-Error：ErrorActionPreference=Stop 下它会先终止脚本并返回 1，
# 让「执行出错」和「审查拒绝」变成同一个退出码，破坏契约。
function Fail {
    param([string]$Message)
    Write-Host "错误：$Message" -ForegroundColor Red
    exit 2
}

$isDesign = $PSCmdlet.ParameterSetName -eq 'Design'
$number = if ($isDesign) { $Issue } else { $Pr }

# ---- 定位 codex ----
$codex = (Get-Command codex -ErrorAction SilentlyContinue).Source
if (-not $codex) {
    $fallback = Join-Path $env:LOCALAPPDATA 'Programs\OpenAI\Codex\bin\codex.exe'
    if (Test-Path $fallback) { $codex = $fallback }
}
if (-not $codex) { Fail "找不到 codex CLI。PATH 里没有，$env:LOCALAPPDATA\Programs\OpenAI\Codex\bin 下也没有。" }

$slug = (& git -C $repo remote get-url origin) -replace '^.*github\.com[:/]', '' -replace '\.git$', ''

# ---- 前置校验：Codex 读的是工作区文件，工作区必须就是被审的那一版 ----
$dirty = & git -C $repo status --porcelain
if ($dirty) {
    Fail @"
工作区有未提交改动，审查结论会不可信：

$($dirty -join "`n")

Codex 从工作区读审查清单、架构文档与规格。这些文件若有未提交的本地修改，
它就会依据不属于本次审查对象的规则下判断，而且不会有任何提示。

先提交或 stash 再跑。
"@
}

if (-not $isDesign) {
    $prHead = & gh pr view $Pr --repo $slug --json headRefOid --jq '.headRefOid'
    if ($LASTEXITCODE -ne 0 -or -not $prHead) { Fail "取不到 PR #$Pr 的 head SHA。PR 存在吗？gh 登录了吗？" }
    $localHead = (& git -C $repo rev-parse HEAD).Trim()
    if ($localHead -ne $prHead.Trim()) {
        Fail @"
本地工作区与 PR #$Pr 不是同一版：
  PR head : $($prHead.Trim())
  本地 HEAD: $localHead

先切过去：gh pr checkout $Pr
"@
    }
}

# ---- 取审查材料 ----
$inputFile = Join-Path $repo ".codex-input-$($PSCmdlet.ParameterSetName)-$number.md"
$nl = [Environment]::NewLine
$fence = '```'
$expectedVersion = $null

Write-Host "取审查材料..." -ForegroundColor Cyan

if ($isDesign) {
    $json = & gh issue view $Issue --repo $slug --json title,body
    if ($LASTEXITCODE -ne 0) { Fail "取 Issue #$Issue 失败" }
    $obj = $json | ConvertFrom-Json
    $expectedVersion = Get-DesignVersion $obj.body
    if ($null -eq $expectedVersion) {
        Fail "Issue #$Issue 里读不到设计版本。模板要求顶部写「设计版本：v1」。读不到就无法做批准绑定。"
    }
    $sections = @("# $($obj.title)", '', $obj.body)
} else {
    $json = & gh pr view $Pr --repo $slug --json title,body
    if ($LASTEXITCODE -ne 0) { Fail "取 PR #$Pr 失败" }
    $obj = $json | ConvertFrom-Json
    $diff = & gh pr diff $Pr --repo $slug
    if ($LASTEXITCODE -ne 0) { Fail "取 PR #$Pr 的 diff 失败" }

    # 关联的设计文档必须一起给，否则 prompt 里「核对是否忠于已批准的设计」
    # 是一条无法执行的要求 —— Codex 没有网络，看不到那个 Issue。
    $designIssue = Get-LinkedDesignIssue $obj.body
    if ($designIssue) {
        $dj = & gh issue view $designIssue --repo $slug --json title,body,comments
        if ($LASTEXITCODE -ne 0) { Fail "PR 声明关联设计 Issue #$designIssue，但取不到它" }
        $do = $dj | ConvertFrom-Json
        $designVersion = Get-DesignVersion $do.body

        # 按判定行判断，不能全文子串匹配：一条以「旧的 APPROVED: design v1
        # 已作废」结尾、实为 REQUEST_CHANGES 的评论会被算成批准。
        # 取【最后一条】针对当前版本的判定，不是「历史上出现过批准就算批准」。
        # 先批准后拒绝时，撤回必须生效。
        $records = @()
        $latestStatus = $null
        foreach ($c in $do.comments) {
            $st = Get-CommentDesignVerdict $c.body $designVersion
            if ($st -eq 'NOT_A_REVIEW') { continue }
            $line = Get-VerdictLine ($c.body -split '\r?\n')
            switch ($st) {
                'APPROVE'          { $records += "- ✅ 批准当前版本 v$designVersion"; $latestStatus = 'APPROVE' }
                'REQUEST_CHANGES'  { $records += "- ❌ 拒绝"; $latestStatus = 'REQUEST_CHANGES' }
                'VERSION_MISMATCH' { $records += "- ⚠️ 批准的是**其他版本**（当前是 v$designVersion）：$line" }
                'INVALID'          { $records += "- ⚠️ 判定行格式不合法：$line" }
            }
        }
        $hasCurrentApproval = ($latestStatus -eq 'APPROVE')

        $designSection = @(
            "## 关联设计（Issue #$designIssue）", '',
            "顶部声明的设计版本：**v$designVersion**", '',
            "当前版本是否已获批准：**$(if ($hasCurrentApproval) { '是' } else { '否' })**", '',
            $do.body, '',
            '### 该设计的批准记录', '',
            '（判定依据是每条评论的最后一行，不是正文里是否出现过批准字样）', '',
            $(if ($records) { $records -join $nl } else { '（无有效批准记录 —— 该设计尚未通过闸门）' })
        )
    } else {
        $designSection = @(
            '## 关联设计', '',
            '本 PR 的描述里没有声明关联的设计 Issue（约定写法：设计闸门：#N）。',
            '',
            '按 docs/WORKFLOW.md 第 3 节的准入分档，下面两类**都必须**走设计闸门：',
            '',
            '- 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 —— 填全部章节',
            '- 用量摄取 / 集成认证 / Webhook —— 填 §1–§7',
            '',
            '**只有前端 / 文档 / CI / 脚本类改动才允许没有关联设计。**',
            '本 PR 若属于上面两类之一，缺少设计闸门本身就是一个阻断项。'
        )
    }

    $sections = @("# $($obj.title)", '', $obj.body, '') + $designSection + @(
        '', '## DIFF', '', ($fence + 'diff'), ($diff -join $nl), $fence
    )
}

Set-Content -Path $inputFile -Value ($sections -join $nl) -Encoding UTF8
$sizeKb = [math]::Round((Get-Item $inputFile).Length / 1KB, 1)
Write-Host "审查材料：$inputFile（$sizeKb KB）"
if ($sizeKb -gt 400) { Write-Warning "材料超过 400 KB，可能超出上下文。考虑拆分。" }

$rel = Split-Path -Leaf $inputFile

# ---- 组 prompt ----
$common = @"
你是本仓库的独立审查者，不是开发者。

硬规则：
- 只读。不修改工作区任何文件，不 git add / commit / push，不合并任何东西。
- **不要调用 gh 或任何网络命令**。审查材料已取好放在 $rel 里，直接读那个文件。
- 只报你能指出具体位置和具体后果的问题。指不出后果的观感问题不要写。
- 不要重复 linter 和 CI 已经能抓的东西（格式、import 顺序、拼写）。
- 没有问题就写「无」，不要为了显得认真而凑数。

先读 scripts/review_checklist.md，那是本项目的审查清单。
需要上下文时读工作区里的 docs/ARCHITECTURE.md（14 条不变量）、docs/adr/ 下的决策记录，
以及 docs/Acuven_Central_AI_Billing_Platform_Spec_v1.2.md 的相关章节。
"@

if ($isDesign) {
    $target = "Issue #$Issue"
    $out = Join-Path $repo ".codex-review-design-$Issue.md"
    $prompt = @"
$common

本次是【设计闸门】审查，目标是 $slug 的 Issue #$Issue，**设计版本 v$expectedVersion**。

步骤：
1. 读 $rel —— 设计文档全文已在里面。
2. 走 scripts/review_checklist.md 的「设计审查」一节，明确回答那五个问题。
3. 核对设计闸门的七条判定。

按以下格式输出。最后一行只能是这两种之一，**多一个字都会被判为无效**：
  APPROVED: design v$expectedVersion
  REQUEST_CHANGES

## 🔍 CODEX REVIEW — 设计闸门

**结论**：<一句话>

### 五问
1. 哪个具体场景会破坏不变量？<答>
2. 哪条失败路径没有定义最终状态或恢复方式？<答>
3. 哪项正确性只靠应用代码、缺少数据库约束？<答>
4. 哪个新增分支没有对应测试？<答>
5. 设计是否与 spec 的具体章节冲突？<答>

### 阻断项
- ``章节`` — 问题 → 后果
（没有就写「无」）

### 闸门判定
逐条列出七条判定是否满足。

---
REQUEST_CHANGES
"@
} else {
    $target = "PR #$Pr"
    $out = Join-Path $repo ".codex-review-$Pr.md"
    $prompt = @"
$common

本次是【实现闸门】审查，目标是 $slug 的 PR #$Pr。

步骤：
1. 读 $rel —— PR 描述、关联设计（含批准记录）、完整 diff 都在里面。
2. 走 scripts/review_checklist.md 的「实现审查」A–E 各节。
3. 若材料里有关联设计，核对实现是否忠于**已批准的那一版**；
   若材料说明没有关联设计，按其中的提示判断这是否构成阻断项。

按以下格式输出。最后一行只能是这两种之一，**多一个字都会被判为无效**：
  VERDICT: APPROVE
  VERDICT: REQUEST_CHANGES

## 🔍 CODEX REVIEW

**结论**：<一句话>

### 阻断项
- ``文件:行`` — 问题 → 后果
（没有就写「无」）

### 建议项
- ``文件:行`` — 问题 → 建议
（没有就写「无」）

### 清单核对
- 不变量：<触碰了第几条，是否保住>
- DoD：<哪几条未满足>
- 测试：<改动引入的边界是否被覆盖>

---
VERDICT: REQUEST_CHANGES
"@
}

Write-Host "审查目标：$target（$slug）" -ForegroundColor Cyan
Write-Host "沙箱：read-only —— Codex 改不了任何文件" -ForegroundColor Cyan
Write-Host ("-" * 60)

& $codex exec -s read-only -C $repo -o $out $prompt
if ($LASTEXITCODE -ne 0) { Fail "codex exec 失败，退出码 $LASTEXITCODE" }
if (-not (Test-Path $out)) { Fail "codex 没有产出结果文件：$out" }

# ---- 先校验判定，再发布 ----
# 顺序不能反：畸形的审查结果一旦发出去就永久留在 PR 上了。
$verdictLine = Get-VerdictLine (Get-Content $out)
$result = if ($isDesign) { Get-DesignVerdict $verdictLine $expectedVersion } else { Get-ImplementationVerdict $verdictLine }

Write-Host ("-" * 60)
Write-Host "结果已保存：$out"
Write-Host "判定行：$verdictLine" -ForegroundColor Yellow

switch ($result) {
    'INVALID'          { Fail "最后一行不是合法判定，Codex 没按格式输出。未发布。请人工看 $out" }
    'VERSION_UNKNOWN'  { Fail "批准里没有可比对的设计版本。未发布。" }
    'VERSION_MISMATCH' { Fail "批准的设计版本与 Issue 顶部的 v$expectedVersion 不一致 —— 批准的是另一版设计。未发布。" }
}

if ($Post) {
    if ($isDesign) { & gh issue comment $Issue --repo $slug --body-file $out }
    else           { & gh pr comment $Pr --repo $slug --body-file $out }
    if ($LASTEXITCODE -ne 0) { Fail "发布评论失败" }
    Write-Host "已发布到 $target" -ForegroundColor Green
} else {
    Write-Host "未发布（加 -Post 可发到 $target）"
}

Remove-Item $inputFile -ErrorAction SilentlyContinue

if ($result -eq 'APPROVE') { Write-Host "判定：通过" -ForegroundColor Green; exit 0 }
Write-Host "判定：要改" -ForegroundColor Yellow
exit 1

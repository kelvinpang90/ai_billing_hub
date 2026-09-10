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

# 刻意不用 Mandatory / ParameterSetName：PowerShell 的参数绑定失败发生在脚本
# 代码之前，会以退出码 1 结束 —— 而 1 在本脚本的契约里是「审查要求修改」。
# 自动化会把「命令写错了」误判成「Codex 说要改」。所以自己接管校验，统一走 2。
[CmdletBinding()]
param(
    [int]$Pr = 0,
    [int]$Issue = 0,
    [switch]$Post,

    # 只取材料并做完整性校验就停，不调 Codex。
    # 用来验证取材与编码这条路径，不必每次烧一次审查。
    [switch]$MaterialOnly
)

$ErrorActionPreference = 'Stop'

# ErrorActionPreference=Stop 让任何未捕获的终止性错误结束脚本，而 PowerShell
# 对此的默认退出码是 1 —— 那在本脚本契约里是「审查要求修改」。
# 于是 ConvertFrom-Json 解析失败、磁盘写不进去这类基础设施故障，会被自动化
# 当成 Codex 的有效判定。顶层 trap 把它们统一归到 2。
trap {
    Write-Host "错误：未捕获的异常 —— $($_.Exception.Message)" -ForegroundColor Red
    if ($_.ScriptStackTrace) { Write-Host $_.ScriptStackTrace -ForegroundColor DarkGray }
    exit 2
}

if ($PSVersionTable.PSVersion.Major -lt 7) {
    Write-Host "错误：本脚本需要 PowerShell 7+。当前是 $($PSVersionTable.PSVersion)。请用 pwsh 运行。" -ForegroundColor Red
    exit 2
}

# 仅为了让本脚本打印的中文在 GBK 控制台上不糊。
# 取材料的正确性不靠这个 —— 靠 Invoke-GhUtf8 显式指定的流编码。
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }

$repo = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'lib\ReviewVerdict.ps1')

# 不能用 Write-Error：ErrorActionPreference=Stop 下它会先终止脚本并返回 1，
# 让「执行出错」和「审查拒绝」变成同一个退出码，破坏契约。
function Fail {
    param([string]$Message)
    Write-Host "错误：$Message" -ForegroundColor Red
    exit 2
}

# 不能直接用 & gh：PowerShell 捕获原生命令输出时按 [Console]::OutputEncoding
# 解码，那是控制台的属性，受代码页影响。实测在简中机器上把 gh 的 UTF-8
# 解成了乱码，而且在脚本里改 [Console]::OutputEncoding 也没能纠正。
#
# 这里直接起进程并显式指定 StandardOutputEncoding，不依赖控制台是什么编码。
function Invoke-GhUtf8 {
    param([Parameter(Mandatory)][string[]]$Arguments)

    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = 'gh'
    foreach ($a in $Arguments) { [void]$psi.ArgumentList.Add($a) }
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $psi.StandardOutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $psi.StandardErrorEncoding = [System.Text.UTF8Encoding]::new($false)

    $proc = [System.Diagnostics.Process]::Start($psi)
    $stdout = $proc.StandardOutput.ReadToEnd()
    $stderr = $proc.StandardError.ReadToEnd()
    $proc.WaitForExit()

    if ($proc.ExitCode -ne 0) {
        Fail "gh $($Arguments -join ' ') 失败（退出码 $($proc.ExitCode)）：$stderr"
    }
    return $stdout
}

if ($Pr -le 0 -and $Issue -le 0) { Fail "必须指定 -Pr <PR编号> 或 -Issue <设计Issue编号>。" }
if ($Pr -gt 0 -and $Issue -gt 0) { Fail "-Pr 与 -Issue 不能同时指定。" }

$isDesign = $Issue -gt 0
$number = if ($isDesign) { $Issue } else { $Pr }
$kind = if ($isDesign) { 'Design' } else { 'Implementation' }

# codex 的定位推迟到真要调用它的时候。-MaterialOnly 只取材料，
# 没装 Codex 的机器也该能用。
function Resolve-CodexPath {
    $path = (Get-Command codex -ErrorAction SilentlyContinue).Source
    if (-not $path) {
        $fallback = Join-Path $env:LOCALAPPDATA 'Programs\OpenAI\Codex\bin\codex.exe'
        if (Test-Path $fallback) { $path = $fallback }
    }
    if (-not $path) { Fail "找不到 codex CLI。PATH 里没有，$env:LOCALAPPDATA\Programs\OpenAI\Codex\bin 下也没有。" }
    return $path
}

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

# Codex 读的是工作区文件，所以「工作区停在哪个提交」本身就是审查基线的一部分。
# 光查 status --porcelain 不够：审查期间 checkout 到另一个干净的提交，
# porcelain 依然是空的，只有比对 HEAD 才发现得了。设计闸门同样要查 ——
# 它读的清单、架构文档、spec 也都来自工作区。
$localHead = (& git -C $repo rev-parse HEAD).Trim()

if (-not $isDesign) {
    $prHead = Invoke-GhUtf8 @('pr', 'view', "$Pr", '--repo', $slug, '--json', 'headRefOid', '--jq', '.headRefOid')
    if (-not $prHead.Trim()) { Fail "取不到 PR #$Pr 的 head SHA。" }
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
$inputFile = Join-Path $repo ".codex-input-$kind-$number.md"
$nl = [Environment]::NewLine
$fence = '```'
# 材料按段返回，不是拼成一个大字符串就把结构丢掉。审查跑完要拿它逐段复检，
# 有分段才能指出「哪一段变了」，而不是只说「变了」。
function Get-ReviewMaterial {
    if ($isDesign) {
        $json = Invoke-GhUtf8 @('issue', 'view', "$Issue", '--repo', $slug, '--json', 'title,body')
        $obj = $json | ConvertFrom-Json
        $version = Get-DesignVersion $obj.body
        if ($null -eq $version) {
            Fail "Issue #$Issue 里读不到设计版本。模板要求顶部写「设计版本：v1」。读不到就无法做批准绑定。"
        }
        return [pscustomobject]@{
            Title         = $obj.title
            Version       = $version
            DesignIssue   = $null
            DesignVersion = $null
            HasApproval   = $false
            Parts         = [ordered]@{
                '标题'     = "# $($obj.title)"
                '设计正文' = $obj.body
            }
        }
    }

    $json = Invoke-GhUtf8 @('pr', 'view', "$Pr", '--repo', $slug, '--json', 'title,body')
    $obj = $json | ConvertFrom-Json
    $diff = Invoke-GhUtf8 @('pr', 'diff', "$Pr", '--repo', $slug)

    # 关联的设计文档必须一起给，否则 prompt 里「核对是否忠于已批准的设计」
    # 是一条无法执行的要求 —— Codex 没有网络，看不到那个 Issue。
    $designIssue = Get-LinkedDesignIssue $obj.body
    $designVersion = $null
    $hasCurrentApproval = $false

    if ($designIssue) {
        $dj = Invoke-GhUtf8 @('issue', 'view', "$designIssue", '--repo', $slug, '--json', 'title,body,comments')
        $do = $dj | ConvertFrom-Json
        $designVersion = Get-DesignVersion $do.body

        # 按判定行判断，不能全文子串匹配：一条以「旧的 APPROVED: design v1
        # 已作废」结尾、实为 REQUEST_CHANGES 的评论会被算成批准。
        # 取【最后一条】针对当前版本的判定，不是「历史上出现过批准就算批准」。
        # 先批准后拒绝时，撤回必须生效。
        $records = @()
        foreach ($c in $do.comments) {
            $st = Get-CommentDesignVerdict $c.body $designVersion
            if ($st -eq 'NOT_A_REVIEW') { continue }
            $line = Get-VerdictLine ($c.body -split '\r?\n')
            switch ($st) {
                'APPROVE'          { $records += "- ✅ 批准当前版本 v$designVersion" }
                'REQUEST_CHANGES'  { $records += "- ❌ 拒绝" }
                'VERSION_MISMATCH' { $records += "- ⚠️ 批准的是**其他版本**（当前是 v$designVersion）：$line" }
                'INVALID'          { $records += "- ⚠️ 判定行格式不合法：$line" }
            }
        }
        $hasCurrentApproval = Get-LatestDesignApproval $do.comments $designVersion

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

    return [pscustomobject]@{
        Title         = $obj.title
        Version       = $null
        DesignIssue   = $designIssue
        DesignVersion = $designVersion
        HasApproval   = $hasCurrentApproval
        Parts         = [ordered]@{
            '标题'     = "# $($obj.title)"
            'PR 正文'  = $obj.body
            '关联设计' = ($designSection -join $nl)
            'DIFF'     = ($diff -join $nl)
        }
    }
}

function Format-Material {
    param($Material)
    $p = $Material.Parts
    if ($isDesign) {
        return (@($p['标题'], '', $p['设计正文']) -join $nl)
    }
    return (@(
        $p['标题'], '', $p['PR 正文'], '', $p['关联设计'],
        '', '## DIFF', '', ($fence + 'diff'), $p['DIFF'], $fence
    ) -join $nl)
}

Write-Host "取审查材料..." -ForegroundColor Cyan
$material = Get-ReviewMaterial
$expectedVersion = $material.Version

Set-Content -Path $inputFile -Value (Format-Material $material) -Encoding UTF8

# 材料坏了必须当场炸，不能让 Codex 对着乱码给判定。
$written = Get-Content $inputFile -Raw -Encoding UTF8

# 替换字符必须运行时构造，不能在源码里写字面量 —— 否则本文件自身就含有它，
# 任何改到本文件的 PR，其 diff 都会让这条检查自己触发自己。踩过一次。
$replacementChar = [char]0xFFFD

# 只查元数据部分。diff 里出现任意字节都是合法的（比如本文件这段注释），
# 拿它当乱码证据会误判。
$metaEnd = $written.IndexOf($nl + '## DIFF' + $nl)
$metaText = if ($metaEnd -gt 0) { $written.Substring(0, $metaEnd) } else { $written }

if ($metaText.Contains($replacementChar)) {
    Fail "审查材料的元数据部分有替换字符（U+FFFD），说明 gh 输出的编码没被正确解码。不能对着乱码审查。"
}
if ($material.Title -and -not $written.Contains($material.Title)) {
    Fail "审查材料里找不到 PR/Issue 标题原文，材料可能在写盘时被破坏。标题应为：$($material.Title)"
}
if (-not $isDesign -and $written -notmatch '(?m)^diff --git ') {
    Fail "审查材料里没有一行 diff --git，diff 没取到或已损坏。"
}

$sizeKb = [math]::Round((Get-Item $inputFile).Length / 1KB, 1)
Write-Host "审查材料：$inputFile（$sizeKb KB，完整性校验通过）"
if ($sizeKb -gt 400) { Write-Warning "材料超过 400 KB，可能超出上下文。考虑拆分。" }

if ($MaterialOnly) {
    Write-Host "-MaterialOnly：材料已生成并通过校验，未调用 Codex。" -ForegroundColor Green
    Write-Host "标题：$($material.Title)"
    exit 0
}

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

$codex = Resolve-CodexPath

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

# 署名前缀同样要校验，而且必须用读取方那个函数（见 lib 里 Test-ReviewHeader 的注释）。
# 只看最后一行的话，会发布一条自己判为合法、读取方判为「不是审查」的批准。
if (-not (Test-ReviewHeader (Get-Content $out -Raw) -Design:$isDesign)) {
    $expectedHeader = if ($isDesign) { $DesignReviewPrefix } else { $ImplementationReviewPrefix }
    Fail "审查结果的首行不是约定的署名前缀，未发布。应为「$expectedHeader」。请人工看 $out"
}

switch ($result) {
    'INVALID'          { Fail "最后一行不是合法判定，Codex 没按格式输出。未发布。请人工看 $out" }
    'VERSION_UNKNOWN'  { Fail "批准里没有可比对的设计版本。未发布。" }
    'VERSION_MISMATCH' { Fail "批准的设计版本与 Issue 顶部的 v$expectedVersion 不一致 —— 批准的是另一版设计。未发布。" }
}

# ---- 审查后再校验一次基线 ----
# 开跑前查过不代表跑完还成立：审查期间的新提交或工作区改动，会让这份判定
# 指向的是已经不存在的那一版代码。这种情况下发布出去，等于给未审查的代码
# 盖了个通过的章。
$dirtyAfter = & git -C $repo status --porcelain
if ($dirtyAfter) {
    Fail @"
审查期间工作区被改动，本次判定不可信，未发布：

$($dirtyAfter -join "`n")

Codex 读工作区文件是实时的，中途改动会让它依据一半旧一半新的规则下判断。
"@
}

$localHeadAfter = (& git -C $repo rev-parse HEAD).Trim()
if ($localHeadAfter -ne $localHead) {
    Fail @"
审查期间本地 HEAD 被切换，本次判定不可信，未发布：
  审查时 : $localHead
  现在   : $localHeadAfter

Codex 读的是工作区文件。切到另一个提交后它看到的已经是另一版代码，
而工作区依旧是干净的 —— 只查 git status 发现不了这种情况。
"@
}

if (-not $isDesign) {
    $headAfter = Invoke-GhUtf8 @('pr', 'view', "$Pr", '--repo', $slug, '--json', 'headRefOid', '--jq', '.headRefOid')
    if (-not $headAfter.Trim()) { Fail "审查后取不到 PR head，无法确认基线未变，未发布。" }
    if ($headAfter.Trim() -ne $prHead.Trim()) {
        Fail @"
审查期间 PR 有了新提交，本次判定针对的是旧代码，未发布：
  审查时 : $($prHead.Trim())
  现在   : $($headAfter.Trim())

重新跑一次。
"@
    }
}

# 材料只在开跑前取过一次，之后它的每一个来源都还可能变：设计升版、批准被撤回、
# 设计正文原地改写（版本不变）、PR 正文改成关联另一个设计、base 前进导致 diff 变化。
# 前面那些是「想到了才查得到」的专项检查，漏一项就是一个静默的洞。
# 这里重新取一次材料逐段比对 —— **材料逐字没变，才允许发布判定**。
$after = Get-ReviewMaterial

if ($material.DesignIssue -ne $after.DesignIssue) {
    Fail "审查期间 PR 正文里关联的设计 Issue 变了（#$($material.DesignIssue) → #$($after.DesignIssue)），未发布。"
}

# 专项检查留着不是为了兜底（上面那段才是），是为了给出具体到「升版」还是
# 「撤回」的错误信息 —— 逐段比对只能说「关联设计变了」。
if ($material.DesignIssue) {
    switch (Compare-DesignState $material.DesignVersion $material.HasApproval $after.DesignVersion $after.HasApproval) {
        'VERSION_CHANGED' {
            Fail "审查期间关联设计 Issue #$($material.DesignIssue) 升版（v$($material.DesignVersion) → v$($after.DesignVersion)），本次判定针对的是旧设计，未发布。"
        }
        'APPROVAL_CHANGED' {
            Fail "审查期间关联设计 Issue #$($material.DesignIssue) 的批准状态变了（已批准=$($material.HasApproval) → $($after.HasApproval)），未发布。"
        }
    }
}

$changedParts = Compare-MaterialParts $material.Parts $after.Parts
if ($changedParts) {
    Fail @"
审查期间审查材料发生了变化，本次判定针对的是旧材料，未发布：
  变化的部分：$($changedParts -join '、')

重新跑一次。
"@
}

if (-not $isDesign) {
    # 判定必须携带它审的是哪个提交。没有这行，一次 APPROVE 会被后续提交
    # 沿用下去 —— 未审查的代码就凭旧批准进了正式 PR。
    $reviewed = @()
    $lines = @(Get-Content $out)
    $lastIdx = $lines.Count - 1
    while ($lastIdx -ge 0 -and -not $lines[$lastIdx].Trim()) { $lastIdx-- }
    if ($lastIdx -gt 0) { $reviewed += $lines[0..($lastIdx - 1)] }
    $reviewed += "reviewed-head: $($prHead.Trim())"
    $reviewed += ''
    $reviewed += $lines[$lastIdx]
    Set-Content -Path $out -Value $reviewed -Encoding UTF8
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

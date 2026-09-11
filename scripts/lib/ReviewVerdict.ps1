<#
判定解析的纯函数。抽出来是为了能在不调用 Codex 的前提下测试
（见 scripts/tests/Test-ReviewVerdict.ps1）。

这里的每一条都对应一次真实的审查发现，不要"简化"：
- 前缀匹配会把 prompt 里的占位符判成通过
- 设计批准不比对 Issue 里的版本号，绑定就是摆设
#>

# 从审查输出里取判定行 = 最后一个非空行
function Get-VerdictLine {
    param([string[]]$Lines)
    if (-not $Lines) { return '' }
    $last = $Lines | Where-Object { $_ -and $_.Trim() } | Select-Object -Last 1
    if ($null -eq $last) { return '' }
    return $last.Trim()
}

# 实现闸门：只接受这两行，多一个字都不行
function Get-ImplementationVerdict {
    param([string]$Line)
    # -casesensitive 是必须的：PowerShell 的 switch -regex / -match 默认忽略大小写，
    # 不加的话 'VERDICT: approve' 也会被判成通过。  check-docs:allow
    switch -regex -casesensitive ($Line) {
        '^VERDICT: APPROVE$'         { return 'APPROVE' }
        '^VERDICT: REQUEST_CHANGES$' { return 'REQUEST_CHANGES' }
        default                      { return 'INVALID' }
    }
}

# 从设计 Issue 正文里取版本号。格式见 .github/ISSUE_TEMPLATE/design-gate.md：
#   **设计版本**：`v1`
function Get-DesignVersion {
    param([string]$IssueBody)
    if (-not $IssueBody) { return $null }
    $m = [regex]::Match($IssueBody, '设计版本\D{0,8}v(\d+)')
    if ($m.Success) { return [int]$m.Groups[1].Value }
    return $null
}

# 设计闸门：批准必须带具体数字，且必须等于 Issue 顶部的版本号。
# 版本对不上 = 批准的是另一版设计 = 无效。
function Get-DesignVerdict {
    param(
        [string]$Line,
        [Nullable[int]]$ExpectedVersion
    )
    if ($Line -cmatch '^REQUEST_CHANGES$') { return 'REQUEST_CHANGES' }

    $m = [regex]::Match($Line, '^APPROVED: design v(\d+)$')
    if (-not $m.Success) { return 'INVALID' }

    if ($null -eq $ExpectedVersion) { return 'VERSION_UNKNOWN' }

    $approved = [int]$m.Groups[1].Value
    if ($approved -ne $ExpectedVersion) { return 'VERSION_MISMATCH' }
    return 'APPROVE'
}

# 从 PR 正文里找关联的设计 Issue 编号。
# 约定写法："设计闸门：#12" 或 "设计闸门: #12"
function Get-LinkedDesignIssue {
    param([string]$PrBody)
    if (-not $PrBody) { return $null }
    $m = [regex]::Match($PrBody, '设计闸门\s*[:：]\s*#(\d+)')
    if ($m.Success) { return [int]$m.Groups[1].Value }
    return $null
}

# 判断一条评论是不是对某版设计的有效批准。
#
# 不能用全文子串匹配 'APPROVED: design v\d+' —— 一条以
# 「旧的 APPROVED: design v1 已作废」结尾、判定为 REQUEST_CHANGES 的评论
# 也会被算成批准记录（Codex 实测过这个假阳性）。
# 必须按判定行（最后一个非空行）来判。
#
# 还必须校验开头的固定前缀。Claude 与 Codex 共用同一个 GitHub 账号，
# 评论作者分不出谁是谁，这个前缀是「这条是独立审查」的唯一凭据。
# 不校验的话，Claude 自己写一条以合法批准行结尾的回应就会被算成批准 ——
# 独立审查形同虚设。
$DesignReviewPrefix = '## 🔍 CODEX REVIEW — 设计闸门'
$ImplementationReviewPrefix = '## 🔍 CODEX REVIEW'

function Get-FirstNonEmptyLine {
    param([string[]]$Lines)
    if (-not $Lines) { return '' }
    $first = $Lines | Where-Object { $_ -and $_.Trim() } | Select-Object -First 1
    if ($null -eq $first) { return '' }
    return $first.Trim()
}

# 署名前缀校验。**发布方与读取方必须调用同一个函数** —— 各写一份的话，
# 会发布一条自己认为合法、而读取方判为 NOT_A_REVIEW 的批准：
# 设计闸门显示通过，实现审查却说该设计未批准，两端各说各话且都不报错。
function Test-ReviewHeader {
    param([string]$Body, [switch]$Design)
    $expected = if ($Design) { $DesignReviewPrefix } else { $ImplementationReviewPrefix }
    # -ceq：大小写必须一致，理由同 Get-ImplementationVerdict
    return ((Get-FirstNonEmptyLine ($Body -split '\r?\n')) -ceq $expected)
}

function Get-CommentDesignVerdict {
    param(
        [string]$CommentBody,
        [Nullable[int]]$ExpectedVersion
    )
    if (-not $CommentBody) { return 'NOT_A_REVIEW' }
    if (-not (Test-ReviewHeader $CommentBody -Design)) { return 'NOT_A_REVIEW' }
    return Get-DesignVerdict (Get-VerdictLine ($CommentBody -split '\r?\n')) $ExpectedVersion
}

# 从一串评论里取【最后一条】针对当前版本的判定，返回是否处于已批准状态。
# 不能用「出现过批准就算批准」—— 后续的撤回必须生效。
function Get-LatestDesignApproval {
    param(
        $Comments,
        [Nullable[int]]$ExpectedVersion
    )
    $latest = $null
    foreach ($c in $Comments) {
        $status = Get-CommentDesignVerdict $c.body $ExpectedVersion
        if ($status -eq 'APPROVE' -or $status -eq 'REQUEST_CHANGES') { $latest = $status }
    }
    return ($latest -eq 'APPROVE')
}

# 审查期间设计可能升版，批准也可能被撤回。发布前必须确认这两样都没变，
# 否则会把一份基于旧设计的通过判定发出去 —— 设计版本绑定就白做了。
function Compare-DesignState {
    param(
        [Nullable[int]]$BeforeVersion,
        [bool]$BeforeApproved,
        [Nullable[int]]$AfterVersion,
        [bool]$AfterApproved
    )
    if ($BeforeVersion -ne $AfterVersion) { return 'VERSION_CHANGED' }
    if ($BeforeApproved -ne $AfterApproved) { return 'APPROVAL_CHANGED' }
    return 'UNCHANGED'
}

# 逐段比对审查材料，返回变化了的段名。
#
# 前置的那几项复检（PR head、设计版本、批准状态）都是「猜到了才会查」的专项检查，
# 漏掉一项就是一个静默的洞：PR 正文被改成关联另一个设计、设计正文原地改写而版本
# 不变、base 前进导致 diff 变化 —— 这些都绕得过专项检查。
# 这里做的是兜底：审查材料本身逐字没变，才允许发布判定。
function Compare-MaterialParts {
    param($Before, $After)
    $changed = @()
    foreach ($k in $Before.Keys) {
        if (-not $After.Contains($k)) { $changed += $k; continue }
        if ($Before[$k] -cne $After[$k]) { $changed += $k }
    }
    foreach ($k in $After.Keys) {
        if (-not $Before.Contains($k)) { $changed += $k }
    }
    return $changed
}

# ---- 复审：从评论历史里恢复上下文 ----
# 下面这几个函数原本内联在 codex-review.ps1 的取材逻辑里，无法测试。
# 抽出来的理由和 Get-LatestDesignApproval 一样：判断「上一轮是谁说的、说了什么」
# 出错时是静默的 —— 要么把回应当成审查，要么把预审当成审查。

$ClaudeResponsePrefix = '## 🔧 CLAUDE RESPONSE'

function Test-ResponseHeader {
    param([string]$Body)
    return ((Get-FirstNonEmptyLine ($Body -split '\r?\n')) -ceq $ClaudeResponsePrefix)
}

# 取评论里的 reviewed-head。整行匹配，所以 `> reviewed-head: …` 这种引用上轮的写法
# 自然不算。恰好一处才返回，否则 $null（不猜）。
function Get-ReviewedHead {
    param([string]$Body)
    if (-not $Body) { return $null }
    $found = @()
    foreach ($line in ($Body -split '\r?\n')) {
        if ($line.TrimEnd() -cmatch '^reviewed-head: ([0-9a-f]{40})$') { $found += $Matches[1] }
    }
    if ($found.Count -eq 1) { return $found[0] }
    return $null
}

# 扫一遍评论，得到：最近一轮有效实现审查、它之后的最后一条回应、审查轮数。
# 新一轮审查出现时上一条回应作废（它回应的是更早那轮）。
# 判定行不合法的「审查」不算数 —— 那是发布方校验前的残留或人工贴的草稿。
function Get-ImplementationReviewHistory {
    param($Comments)
    $previous = $null
    $response = $null
    $count = 0
    foreach ($c in $Comments) {
        if (Test-ReviewHeader $c.body) {
            $verdict = Get-ImplementationVerdict (Get-VerdictLine ($c.body -split '\r?\n'))
            if ($verdict -eq 'INVALID') { continue }
            $previous = $c.body
            $response = $null
            $count++
        } elseif (Test-ResponseHeader $c.body) {
            $response = $c.body
        }
    }
    return [pscustomobject]@{ PreviousReview = $previous; Response = $response; ReviewCount = $count }
}

# 第三轮起要求回应里有非空的「### 完整影响面」。
#
# 不能用 `^### 完整影响面[ \t]*\r?\n\s*\S`：`\s*` 会跨过空行，而后面跟的下一个
# 章节标题本身就是 `\S`，于是「本节留空、紧接另一个章节」也返回真 —— 这条要求
# 可以被一个空章节静默绕过，拦不住继续逐项补洞。
# 必须在下一个标题之前找到真正的内容行。
function Test-ImpactSection {
    param([string]$Body)
    if (-not $Body) { return $false }
    $inSection = $false
    foreach ($line in ($Body -split '\r?\n')) {
        if (-not $inSection) {
            if ($line.TrimEnd() -ceq '### 完整影响面') { $inSection = $true }
            continue
        }
        if ($line -match '^#{1,6}\s') { return $false }   # 下一个标题 = 本节到此为止
        if ($line.Trim()) { return $true }
    }
    return $false
}

# ---- 唯一一处要起 git 进程的地方 ----
# 上面都是纯函数。下面两个要调 git，但仍放在 lib 里：增量的稳定性是能测的，
# 而且必须测 —— 它一旦不稳定，复审就永远发不出判定，且失败信息只说「材料变了」。

# git 的输出经 PowerShell 管道会按控制台代码页解码，中文会糊（实测）。这里显式
# 指定流编码，不依赖控制台是什么代码页。
# stderr 丢弃：--no-index 会为 CRLF 转换打 warning，那不是错误，也不该进审查材料。
# 不在这里抛错：--no-index 用退出码 1 表示「有差异」，由调用方解释。
function Invoke-GitCommand {
    param(
        [Parameter(Mandatory)][string[]]$Arguments,
        [Parameter(Mandatory)][string]$WorkingDirectory
    )
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = 'git'
    foreach ($a in (@('-C', $WorkingDirectory) + $Arguments)) { [void]$psi.ArgumentList.Add($a) }
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $psi.StandardOutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $psi.StandardErrorEncoding = [System.Text.UTF8Encoding]::new($false)
    $proc = [System.Diagnostics.Process]::Start($psi)
    $stdout = $proc.StandardOutput.ReadToEnd()
    [void]$proc.StandardError.ReadToEnd()
    $proc.WaitForExit()
    return [pscustomobject]@{ Output = $stdout; ExitCode = $proc.ExitCode }
}

# 两版「PR 相对 base 的 diff」之间的差异。
#
# 文件名必须是固定的、且要以所在目录为工作目录用相对名调 git。
# `git diff --no-index` 把命令行上给的路径原样写进输出（`--stat` 里还会截断成
# `...abc.tmp`，事后做字符串替换救不回来）。用随机临时名的话，这段文本进了
# 「复审上下文」，审查前后两次取材就会逐字不等，Compare-MaterialParts 判为
# 材料变化 —— 只要增量非空，所有正常的修复复审都发不出判定。
# 相对名调用时输出里只会出现 previous.diff / current.diff，两次调用逐字相同。
function Get-DiffOfDiffs {
    param(
        [Parameter(Mandatory)][AllowEmptyString()][string]$Previous,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Current
    )
    # $PID 只为避免并发跑的两个进程互相踩；它不进输出，所以不影响逐段比对。
    $dir = Join-Path ([System.IO.Path]::GetTempPath()) "codex-review-delta-$PID"
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    try {
        [void](New-Item -ItemType Directory -Force -Path $dir)
        [System.IO.File]::WriteAllText((Join-Path $dir 'previous.diff'), $Previous, $utf8)
        [System.IO.File]::WriteAllText((Join-Path $dir 'current.diff'), $Current, $utf8)
        # --no-index：0 = 两版逐字相同，1 = 有差异，其它 = 出错
        $stat  = Invoke-GitCommand @('diff', '--no-index', '--stat', 'previous.diff', 'current.diff') $dir
        $delta = Invoke-GitCommand @('diff', '--no-index', 'previous.diff', 'current.diff') $dir
    } finally {
        Remove-Item -Recurse -Force -LiteralPath $dir -ErrorAction SilentlyContinue
    }
    if ($stat.ExitCode -gt 1 -or $delta.ExitCode -gt 1) {
        return [pscustomobject]@{ Ok = $false; Identical = $false; Stat = ''; Delta = '' }
    }
    return [pscustomobject]@{
        Ok        = $true
        Identical = ($stat.ExitCode -eq 0)
        Stat      = $stat.Output
        Delta     = $delta.Output
    }
}

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

function Get-CommentDesignVerdict {
    param(
        [string]$CommentBody,
        [Nullable[int]]$ExpectedVersion
    )
    if (-not $CommentBody) { return 'NOT_A_REVIEW' }
    $lines = $CommentBody -split '\r?\n'
    $first = $lines | Where-Object { $_.Trim() } | Select-Object -First 1
    if ($null -eq $first -or $first.Trim() -ne $DesignReviewPrefix) { return 'NOT_A_REVIEW' }
    return Get-DesignVerdict (Get-VerdictLine $lines) $ExpectedVersion
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

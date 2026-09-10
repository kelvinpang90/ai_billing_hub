<#
scripts/lib/ReviewVerdict.ps1 的测试。不依赖 Pester，直接跑：

    pwsh -File scripts\tests\Test-ReviewVerdict.ps1

退出码 0 = 全过，1 = 有失败。

每个用例对应一个真实的失败模式，不是凑覆盖率。
#>

$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path -Parent $PSScriptRoot) 'lib\ReviewVerdict.ps1')

$script:failed = 0
$script:passed = 0

function Assert-Equal {
    param($Expected, $Actual, [string]$Because)
    if ($Expected -eq $Actual) {
        $script:passed++
    } else {
        $script:failed++
        Write-Host "  FAIL  $Because" -ForegroundColor Red
        Write-Host "        期望 [$Expected]，实得 [$Actual]"
    }
}

Write-Host "Get-VerdictLine"
Assert-Equal 'VERDICT: APPROVE' (Get-VerdictLine @('## R', '', 'VERDICT: APPROVE')) '取最后一个非空行'
Assert-Equal 'VERDICT: APPROVE' (Get-VerdictLine @('VERDICT: APPROVE', '', '   ')) '尾部空白行不算'
Assert-Equal '' (Get-VerdictLine @()) '空输入返回空串'
Assert-Equal '' (Get-VerdictLine @('', '  ')) '全空白返回空串'

Write-Host "Get-ImplementationVerdict"
Assert-Equal 'APPROVE' (Get-ImplementationVerdict 'VERDICT: APPROVE') '标准通过'
Assert-Equal 'REQUEST_CHANGES' (Get-ImplementationVerdict 'VERDICT: REQUEST_CHANGES') '标准拒绝'
# 真实风险：Codex 在判定行后多写了字，前缀匹配会误判成通过
Assert-Equal 'INVALID' (Get-ImplementationVerdict 'VERDICT: APPROVE 但有保留') '判定行有尾随文本 = 无效'
Assert-Equal 'INVALID' (Get-ImplementationVerdict 'VERDICT: approve') '大小写不符 = 无效'  # check-docs:allow
Assert-Equal 'INVALID' (Get-ImplementationVerdict '结论：VERDICT: APPROVE') '判定行有前缀 = 无效'
Assert-Equal 'INVALID' (Get-ImplementationVerdict '') '空行 = 无效'

Write-Host "Get-DesignVersion"
Assert-Equal 3 (Get-DesignVersion '**设计版本**：`v3`') '标准写法'
Assert-Equal 1 (Get-DesignVersion "状态：READY`n**设计版本**：``v1``  (每次实质修改 +1)") '混在正文里'
Assert-Equal $null (Get-DesignVersion '没有版本信息') '找不到返回 null'
Assert-Equal $null (Get-DesignVersion '') '空正文返回 null'

Write-Host "Get-DesignVerdict"
Assert-Equal 'APPROVE' (Get-DesignVerdict 'APPROVED: design v2' 2) '版本一致 = 通过'
# 真实风险：这正是 prompt 模板里的占位符，前缀匹配会把它判成通过
Assert-Equal 'INVALID' (Get-DesignVerdict 'APPROVED: design v<该 Issue 顶部标注的版本号>' 2) '占位符未替换 = 无效'
Assert-Equal 'VERSION_MISMATCH' (Get-DesignVerdict 'APPROVED: design v1' 2) '批准的是旧版本 = 不通过'
Assert-Equal 'VERSION_UNKNOWN' (Get-DesignVerdict 'APPROVED: design v2' $null) 'Issue 里读不到版本 = 不通过'
Assert-Equal 'REQUEST_CHANGES' (Get-DesignVerdict 'REQUEST_CHANGES' 2) '拒绝'
Assert-Equal 'INVALID' (Get-DesignVerdict 'APPROVED: design v2 已确认' 2) '尾随文本 = 无效'
Assert-Equal 'INVALID' (Get-DesignVerdict 'approved: design v2' 2) '大小写不符 = 无效'
Assert-Equal 'INVALID' (Get-DesignVerdict 'request_changes' 2) '拒绝行大小写不符 = 无效'

Write-Host "Get-CommentDesignVerdict"
$prefix = '## 🔍 CODEX REVIEW — 设计闸门'

# Codex 实测出的假阳性：正文里出现批准字样，但判定是拒绝
Assert-Equal 'REQUEST_CHANGES' (Get-CommentDesignVerdict "$prefix`n旧的 APPROVED: design v1 已作废`n`nREQUEST_CHANGES" 1) '正文提到批准但判定是拒绝 = 不算批准'
Assert-Equal 'APPROVE' (Get-CommentDesignVerdict "$prefix`n无问题`n`nAPPROVED: design v2" 2) '判定行是批准 = 算批准'
Assert-Equal 'VERSION_MISMATCH' (Get-CommentDesignVerdict "$prefix`n`nAPPROVED: design v1" 2) '批准的是旧版本 = 不算当前批准'
Assert-Equal 'INVALID' (Get-CommentDesignVerdict "$prefix`n只是随口提了 APPROVED: design v2 这串字" 2) '批准字样不在最后一行 = 无效'
Assert-Equal 'NOT_A_REVIEW' (Get-CommentDesignVerdict '' 2) '空评论 = 不是审查'

# 自批准漏洞：两边共用同一个 GitHub 账号，作者分不出来，
# 前缀是「这条是独立审查」的唯一凭据。不校验的话 Claude 能自己批准自己。
Assert-Equal 'NOT_A_REVIEW' (Get-CommentDesignVerdict "## 🔧 CLAUDE RESPONSE`n已按意见修改`n`nAPPROVED: design v2" 2) 'Claude 回应以批准行结尾 = 不算批准'
Assert-Equal 'NOT_A_REVIEW' (Get-CommentDesignVerdict "随便一条评论`n`nAPPROVED: design v2" 2) '无审查前缀 = 不算批准'
Assert-Equal 'NOT_A_REVIEW' (Get-CommentDesignVerdict "## 🔍 CODEX REVIEW`n`nAPPROVED: design v2" 2) '实现闸门前缀 ≠ 设计闸门前缀'

Write-Host "Get-LinkedDesignIssue"
Assert-Equal 12 (Get-LinkedDesignIssue '## 任务`n设计闸门：#12`n其余') '中文冒号'
Assert-Equal 7 (Get-LinkedDesignIssue '设计闸门: #7') '英文冒号'
Assert-Equal $null (Get-LinkedDesignIssue '本 PR 不涉及设计闸门') '没写编号返回 null'
Assert-Equal $null (Get-LinkedDesignIssue '') '空正文返回 null'

Write-Host ""
if ($script:failed -gt 0) {
    Write-Host "$script:passed 通过，$script:failed 失败" -ForegroundColor Red
    exit 1
}
Write-Host "$script:passed 通过，0 失败" -ForegroundColor Green
exit 0

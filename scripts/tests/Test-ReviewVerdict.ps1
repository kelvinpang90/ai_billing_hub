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
# 发布前会在判定行之前插入 reviewed-head，插入后判定仍必须是最后一个非空行
Assert-Equal 'VERDICT: APPROVE' (Get-VerdictLine @('## R', '', 'reviewed-head: abc1234', '', 'VERDICT: APPROVE')) 'reviewed-head 不影响判定行定位'

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

Write-Host "Test-ReviewHeader"
# 发布方与读取方共用这一个判断。少了它，脚本会发布一条自己认为合法、
# 而 Get-CommentDesignVerdict 判为 NOT_A_REVIEW 的批准 —— 两端各说各话。
Assert-Equal $true  (Test-ReviewHeader "$prefix`n正文`n`nAPPROVED: design v2" -Design) '设计审查前缀正确'
Assert-Equal $true  (Test-ReviewHeader "`n`n$prefix`n正文" -Design) '前导空行不影响'
Assert-Equal $false (Test-ReviewHeader "**结论**：无问题`n`nAPPROVED: design v2" -Design) '缺前缀 = 不合法'
Assert-Equal $false (Test-ReviewHeader "## 🔍 CODEX REVIEW`n正文" -Design) '实现前缀不能用于设计闸门'
Assert-Equal $true  (Test-ReviewHeader "## 🔍 CODEX REVIEW`n正文") '实现审查前缀正确'
Assert-Equal $false (Test-ReviewHeader "$prefix`n正文") '设计前缀不能用于实现闸门'
Assert-Equal $false (Test-ReviewHeader "## 🔍 codex review`n正文") '大小写不符 = 不合法'  # check-docs:allow
Assert-Equal $false (Test-ReviewHeader '') '空结果 = 不合法'

Write-Host "Compare-MaterialParts"
$before = [ordered]@{ '标题' = '# T'; 'PR 正文' = 'body'; 'DIFF' = 'diff --git a b' }
Assert-Equal 0 (@(Compare-MaterialParts $before ([ordered]@{ '标题' = '# T'; 'PR 正文' = 'body'; 'DIFF' = 'diff --git a b' })).Count) '逐字相同 = 无变化'
Assert-Equal 'PR 正文' (@(Compare-MaterialParts $before ([ordered]@{ '标题' = '# T'; 'PR 正文' = 'body2'; 'DIFF' = 'diff --git a b' })))[0] 'PR 正文被改动'
Assert-Equal 'DIFF' (@(Compare-MaterialParts $before ([ordered]@{ '标题' = '# T'; 'PR 正文' = 'body'; 'DIFF' = 'diff --git a c' })))[0] 'base 前进导致 diff 变化'
# 关联设计从无到有（PR 正文补上「设计闸门：#N」）会让材料多出一段
Assert-Equal '关联设计' (@(Compare-MaterialParts $before ([ordered]@{ '标题' = '# T'; 'PR 正文' = 'body'; 'DIFF' = 'diff --git a b'; '关联设计' = 'x' })))[0] '多出一段也算变化'
Assert-Equal 'DIFF' (@(Compare-MaterialParts $before ([ordered]@{ '标题' = '# T'; 'PR 正文' = 'body' })))[0] '少一段也算变化'
# 大小写变化必须被认出来 —— 判定行、批准记录都是大小写敏感的
Assert-Equal '标题' (@(Compare-MaterialParts $before ([ordered]@{ '标题' = '# t'; 'PR 正文' = 'body'; 'DIFF' = 'diff --git a b' })))[0] '只改大小写也算变化'

Write-Host "Get-LinkedDesignIssue"
Assert-Equal 12 (Get-LinkedDesignIssue '## 任务`n设计闸门：#12`n其余') '中文冒号'
Assert-Equal 7 (Get-LinkedDesignIssue '设计闸门: #7') '英文冒号'
Assert-Equal $null (Get-LinkedDesignIssue '本 PR 不涉及设计闸门') '没写编号返回 null'
Assert-Equal $null (Get-LinkedDesignIssue '') '空正文返回 null'

Write-Host "Get-LatestDesignApproval"
$approve = @{ body = "$prefix`n无问题`n`nAPPROVED: design v2" }
$reject  = @{ body = "$prefix`n有问题`n`nREQUEST_CHANGES" }
$chatter = @{ body = "## 🔧 CLAUDE RESPONSE`n已修" }
$oldVer  = @{ body = "$prefix`n`nAPPROVED: design v1" }

Assert-Equal $true  (Get-LatestDesignApproval @($approve) 2) '只有批准 = 已批准'
Assert-Equal $false (Get-LatestDesignApproval @($approve, $reject) 2) '先批准后拒绝 = 撤回生效'
Assert-Equal $true  (Get-LatestDesignApproval @($reject, $approve) 2) '先拒绝后批准 = 已批准'
Assert-Equal $false (Get-LatestDesignApproval @($chatter) 2) '非审查评论不算数'
Assert-Equal $false (Get-LatestDesignApproval @($oldVer) 2) '批准的是旧版本 = 当前未批准'
Assert-Equal $false (Get-LatestDesignApproval @() 2) '无评论 = 未批准'
Assert-Equal $true  (Get-LatestDesignApproval @($approve, $chatter) 2) '批准后的闲聊不影响结论，批准仍然成立'

Write-Host "Compare-DesignState"
Assert-Equal 'UNCHANGED' (Compare-DesignState 2 $true 2 $true) '版本与批准都没变'
Assert-Equal 'VERSION_CHANGED' (Compare-DesignState 2 $true 3 $true) '审查期间设计升版'
Assert-Equal 'APPROVAL_CHANGED' (Compare-DesignState 2 $true 2 $false) '审查期间批准被撤回'
Assert-Equal 'APPROVAL_CHANGED' (Compare-DesignState 2 $false 2 $true) '审查期间才拿到批准'
Assert-Equal 'VERSION_CHANGED' (Compare-DesignState 2 $true $null $true) '设计版本读不到了'

Write-Host "Test-ResponseHeader"
Assert-Equal $true  (Test-ResponseHeader "## 🔧 CLAUDE RESPONSE`n表") '标准回应'
Assert-Equal $true  (Test-ResponseHeader "`n## 🔧 CLAUDE RESPONSE `n表") '前导空行与尾随空格不影响（与 Python 侧同一规则）'
Assert-Equal $false (Test-ResponseHeader "## 🔍 CODEX REVIEW`n表") '审查不是回应'
Assert-Equal $false (Test-ResponseHeader '') '空 = 不是回应'

Write-Host "Get-ReviewedHead"
$sha  = 'a' * 40
$sha2 = 'b' * 40
Assert-Equal $sha  (Get-ReviewedHead "## 🔍 CODEX REVIEW`nreviewed-head: $sha`n`nVERDICT: APPROVE") '恰好一处'
Assert-Equal $sha  (Get-ReviewedHead "reviewed-head: $sha   `nVERDICT: APPROVE") '行尾空格不影响'
Assert-Equal $null (Get-ReviewedHead "## 🔍 CODEX REVIEW`nVERDICT: APPROVE") '没有 = null，不猜'
Assert-Equal $null (Get-ReviewedHead "reviewed-head: $sha`nreviewed-head: $sha2") '两处 = null，不猜'
# 回应里引用上轮那一行是正常写法，不能算成第二处
Assert-Equal $sha  (Get-ReviewedHead "## 🔧 CLAUDE RESPONSE`n> reviewed-head: $sha`n`nreviewed-head: $sha") '引用行不计'
Assert-Equal $null (Get-ReviewedHead "reviewed-head: $($sha.Substring(0,12))") '短 SHA 不收'

Write-Host "Get-ImplementationReviewHistory"
$rev1 = @{ body = "## 🔍 CODEX REVIEW`n意见`nreviewed-head: $sha`n`nVERDICT: REQUEST_CHANGES" }
$rev2 = @{ body = "## 🔍 CODEX REVIEW`n意见`nreviewed-head: $sha2`n`nVERDICT: APPROVE" }
$resp = @{ body = "## 🔧 CLAUDE RESPONSE`n`nreviewed-head: $sha`n表" }
$pre  = @{ body = "## 🧪 CLAUDE 预审`n`nPRE-REVIEW: 有阻断项" }
$bad  = @{ body = "## 🔍 CODEX REVIEW`n草稿`n`nVERDICT: approve" }  # check-docs:allow

$h = Get-ImplementationReviewHistory @()
Assert-Equal $null $h.PreviousReview '无评论 = 无上轮'
Assert-Equal 0 $h.ReviewCount '无评论 = 0 轮'

$h = Get-ImplementationReviewHistory @($rev1, $resp)
Assert-Equal $rev1.body $h.PreviousReview '取到上轮审查'
Assert-Equal $resp.body $h.Response '取到其后的回应'
Assert-Equal 1 $h.ReviewCount '一轮'

$h = Get-ImplementationReviewHistory @($rev1, $resp, $rev2)
Assert-Equal $rev2.body $h.PreviousReview '新一轮审查覆盖上一轮'
Assert-Equal $null $h.Response '新一轮出现后旧回应作废'
Assert-Equal 2 $h.ReviewCount '两轮'

$h = Get-ImplementationReviewHistory @($pre, $rev1)
Assert-Equal 1 $h.ReviewCount '预审不算审查轮次'
$h = Get-ImplementationReviewHistory @($bad, $rev1)
Assert-Equal 1 $h.ReviewCount '判定行不合法的审查不算数'
$h = Get-ImplementationReviewHistory @($resp)
Assert-Equal $null $h.PreviousReview '只有回应没有审查 = 无上轮'

Write-Host "Test-ImpactSection"
Assert-Equal $true  (Test-ImpactSection "## 🔧 CLAUDE RESPONSE`n`n### 完整影响面`n- 改动覆盖 X、Y") '有内容'
Assert-Equal $false (Test-ImpactSection "## 🔧 CLAUDE RESPONSE`n`n### 完整影响面`n`n") '标题下面是空的 = 没写'
Assert-Equal $false (Test-ImpactSection "## 🔧 CLAUDE RESPONSE`n表") '没有这一节'
Assert-Equal $true  (Test-ImpactSection "### 完整影响面  `n内容") '标题尾随空格不影响'

Write-Host ""
if ($script:failed -gt 0) {
    Write-Host "$script:passed 通过，$script:failed 失败" -ForegroundColor Red
    exit 1
}
Write-Host "$script:passed 通过，0 失败" -ForegroundColor Green
exit 0

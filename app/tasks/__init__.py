"""Celery tasks. Queue loss must never destroy persisted work (Invariant 14).

两条硬规矩，写在这里是因为它们只能靠写任务的人遵守：

1. **任务参数只传标识符，不传值。**`event_id` / `tenant_id` / `payment_id`
   可以，密钥、token、AI prompt / response、请求体一律不行 —— 要用就在任务里
   按 id 去库里取。理由是实测的：**Celery 会把任务的 args / kwargs 写进日志**
   （`celery.worker.strategy` 那条 "Task ... received" 就带着它们）。日志脱敏
   按字段名兜底，而 `args` 这个键名不敏感，兜不住里面是什么。

2. **任务必须幂等。**`task_acks_late` + `task_reject_on_worker_lost` 意味着
   worker 被 kill 时消息会回到队列重投 —— 那是刻意选的（丢消息比重做一次更糟），
   代价就是同一条消息可能被执行两次（Invariant 2/3）。
"""

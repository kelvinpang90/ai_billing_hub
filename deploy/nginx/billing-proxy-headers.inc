# 所有转发到 api 的 location 共用的请求头。单独成文件是为了**不可能漏**：
# 三处 location 各抄一遍，早晚会有一处少一行，而少的那一行通常是
# X-Request-ID —— 丢了它，那一条链路在日志里就断了。
#
# 扩展名是 .inc 而不是 .conf：nginx 自动 include 的是 conf.d/*.conf，
# 叫 .conf 会被当成一个独立的 http 块再加载一次。

proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Request-ID      $billing_request_id;

proxy_http_version 1.1;
proxy_set_header Connection "";

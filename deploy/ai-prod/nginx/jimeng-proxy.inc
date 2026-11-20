# jimeng2api 反代（回环 39076）
#
# ⚠️ 任务轮询 / 异步取图是长请求，proxy_buffering 必须 off，
#    否则前端拿不到实时进度；proxy_read_timeout 给足（上游单任务可跑数分钟）。
proxy_pass http://127.0.0.1:39076;
proxy_http_version 1.1;
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header Connection        "";      # 与 upstream keep-alive 对齐

proxy_buffering off;
proxy_cache off;
proxy_read_timeout 600s;
proxy_send_timeout 600s;
client_max_body_size 64m;   # i2i：参考图以 base64 进 body

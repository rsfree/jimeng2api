FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    JIMENG2API_DATA=/data \
    JIMENG2API_HOST=0.0.0.0 \
    JIMENG2API_PORT=8300

WORKDIR /srv/jimeng2api

COPY requirements.txt requirements-login.txt ./
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# 可选：扫码登录助手（Playwright + Chromium，镜像会大 ~300MB）
ARG WITH_LOGIN=0
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN if [ "$WITH_LOGIN" = "1" ]; then \
      pip install --no-cache-dir -r requirements-login.txt && \
      playwright install --with-deps chromium && \
      chmod -R a+rX /ms-playwright; \
    fi

COPY app ./app
COPY web ./web
COPY gunicorn_conf.py ./

RUN useradd --system --uid 10001 --home /srv/jimeng2api jimeng \
    && mkdir -p /data \
    && chown -R jimeng:jimeng /data /srv/jimeng2api

USER jimeng
VOLUME ["/data"]
EXPOSE 8300

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8300/healthz', timeout=3).status==200 else 1)"

# 工厂形态：写成 app.main:app 会 Failed to find attribute 'app'
CMD ["gunicorn", "-c", "gunicorn_conf.py", "app.main:create_app()"]

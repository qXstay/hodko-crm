# Образ в два этапа. Без строки «# syntax=…»: сборке не нужен внешний фронтенд Docker Hub плавающей версии —
# встроенного в BuildKit достаточно, сборка повторяемая и без сети до базового образа.
# 1. Обычный python:3.12-slim: ставим зависимости, собираем байткод того, что импортируется при запуске,
#    убираем лишнее для работы — pip, apt/dpkg, perl, документацию, модули для разработки, отладочные символы.
# 2. FROM scratch с этой одной файловой системой: образ меньше, запуск и поведение те же.
# База закреплена версией и хешем: сборка через полгода — на той же системе и том же Python.
FROM python:3.12.14-slim@sha256:fa48eefe2146644c2308b909d6bb7651a768178f84fc9550dcd495e4d6d84d01 AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends binutils \
    # Список закреплён целиком (requirements.txt): ставим его без добора зависимостей и проверяем, что он полный.
    && pip install --no-cache-dir --no-deps -r requirements.txt \
    && pip check \
    # в работе не нужны: клиент для тестов, перезапуск при правке, веб-сокеты, YAML-конфиг uvicorn
    && pip uninstall -y httpx httpcore watchfiles websockets pyyaml \
    && find /usr/local/lib/python3.12/site-packages -name '*.so' -exec strip --strip-unneeded {} + \
    && pip uninstall -y pip \
    && apt-get purge -y --auto-remove binutils \
    && rm -rf /var/lib/apt/lists/*

COPY . .
COPY scripts/start.sh scripts/start.sh

# Байткод: приложение, миграции и всё, что импортируется при старте (в slim-образе .pyc стандартной
# библиотеки удалены, а в работе PYTHONDONTWRITEBYTECODE=1 — иначе каждый старт компилировал бы заново).
RUN chmod +x scripts/start.sh \
    && python -m compileall -q app migrations \
    && SECRET_KEY=build DATABASE_URL=sqlite:// python -c "import app.main, app.utils.seed_demo, app.utils.wait_for_db, alembic.config, alembic.command, uvicorn.main, uvicorn.loops.uvloop, uvicorn.protocols.http.httptools_impl, uvicorn.lifespan.on, logging.config" \
    && useradd --uid 10001 --home-dir /app --no-create-home --shell /usr/sbin/nologin crm

# Урезаем систему: пакетный менеджер, perl, документация и модули для разработки в работе не нужны.
RUN rm -rf /var/lib/apt /var/lib/dpkg /var/cache/* /var/log/* /etc/apt /etc/dpkg \
        /usr/share/doc /usr/share/man /usr/share/info /usr/share/locale /usr/share/lintian \
        /usr/share/bash-completion /usr/share/perl5 /usr/share/dpkg /usr/share/common-licenses \
        /usr/lib/*-linux-gnu/perl-base /usr/bin/perl* /usr/bin/apt* /usr/lib/apt /usr/lib/*-linux-gnu/libapt-* \
        /usr/bin/dpkg* /usr/sbin/dpkg* \
        /usr/local/lib/python3.12/ensurepip /usr/local/lib/python3.12/idlelib /usr/local/lib/python3.12/tkinter \
        /usr/local/lib/python3.12/turtledemo /usr/local/lib/python3.12/lib2to3 /usr/local/lib/python3.12/pydoc_data \
        /usr/local/bin/idle3* /usr/local/bin/pydoc3* /usr/local/bin/2to3*

FROM scratch

COPY --from=build / /

ENV PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WARMUP_URL=http://127.0.0.1:8000

WORKDIR /app
USER crm

EXPOSE 8000

# /health отвечает 200, только если приложение и база в порядке (и мир витрины собран).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

CMD ["scripts/start.sh"]

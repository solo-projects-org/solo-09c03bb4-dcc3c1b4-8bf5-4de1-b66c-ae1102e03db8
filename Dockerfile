FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY mock_imap ./mock_imap
COPY mock_imap/mails ./mails
COPY scripts ./scripts

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin qcapp \
    && mkdir -p /data \
    && chown -R qcapp:qcapp /data

EXPOSE 8000 1143 8081

USER qcapp

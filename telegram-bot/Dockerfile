FROM python:3.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY bot.py /app/bot.py
RUN mkdir /data && chown 10001:10001 /data
USER 10001:10001
ENTRYPOINT ["python", "/app/bot.py"]

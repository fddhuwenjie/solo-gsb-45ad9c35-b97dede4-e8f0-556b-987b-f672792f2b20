FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    APP_PORT=8080 \
    APP_DB=/data/app.db

WORKDIR /app
COPY app.py .

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8080

CMD ["python", "app.py"]

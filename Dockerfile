FROM python:3.11-slim

WORKDIR /app
COPY app.py /app/app.py

ENV DB_PATH=/data/repair.db \
    PORT=8080

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).status==200 else 1)"

CMD ["python", "-u", "/app/app.py"]

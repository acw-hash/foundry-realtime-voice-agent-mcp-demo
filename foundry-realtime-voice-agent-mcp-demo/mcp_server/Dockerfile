FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/app PORT=8000 \
    CONTOSO_DATA_DIR=/app/data CONTOSO_CUSTOMERS_FILE=/app/customers.json \
    CONTOSO_STATE_BACKEND=azure_table
WORKDIR /app
COPY mcp_server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mcp_server/app ./app
COPY mcp_server/data/customers.json ./customers.json
COPY mcp_server/LICENSE ./LICENSE
COPY data/officers_20k.json ./data/officers_20k.json
RUN groupadd --system mcp && useradd --system --gid mcp --home-dir /app mcp
USER mcp
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
CMD ["python", "-m", "contoso_mcp.server"]

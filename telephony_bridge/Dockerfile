FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /app
COPY telephony_bridge/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY telephony_bridge/__init__.py telephony_bridge/app.py telephony_bridge/media.py ./telephony_bridge/
RUN groupadd --system bridge && useradd --system --gid bridge --home-dir /app bridge
USER bridge
EXPOSE 8000
CMD ["python", "-m", "telephony_bridge.app"]

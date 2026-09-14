# The provisioner runs as its own service because it holds the docker socket, which is
# root on the host. The server that runs the agent's code must never have that.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# No ports published beyond the compose network; no volumes beyond the socket.
EXPOSE 8002
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8002/health').read()"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8002", "--log-level", "warning"]

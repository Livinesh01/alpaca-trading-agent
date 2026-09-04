FROM python:3.12-slim

# The orchestrator invokes the Cline CLI as its agent loop, so the image needs
# Node.js + the `cline` npm package in addition to the Python stack.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g cline \
    && apt-get purge -y curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create non-root user for container security
RUN groupadd --system sentinel && useradd --system --gid sentinel --home /home/sentinel sentinel \
    && mkdir -p /home/sentinel/.journal /home/sentinel/.cline \
    && chown -R sentinel:sentinel /app /home/sentinel

USER sentinel

EXPOSE 8000
EXPOSE 9108

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=15s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=5)"

# Safe default: start the read-only API server, NOT the trading orchestrator.
# The orchestrator (which can execute trades) must be invoked explicitly.
# Override with: docker run ... yourimage python src/worker.py
CMD ["uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8000"]

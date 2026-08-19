FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    QAGENT_JOBS_DIR=/data/qagent/jobs

COPY pyproject.toml README.md AGENT.md ./
COPY qagent ./qagent
COPY templates ./templates
COPY skills ./skills

RUN pip install --no-cache-dir -e . pypdf python-docx \
    && mkdir -p /data/qagent/jobs

EXPOSE 8765
CMD ["qagent", "serve", "--host", "0.0.0.0", "--port", "8765", "--no-browser"]

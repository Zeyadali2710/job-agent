FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd -r app --gid 10001 && useradd -r -g app --uid 10001 app

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Created in the image so the named volume mounted here inherits app:app.
# Docker seeds an empty named volume from the image directory, ownership
# included; without this the volume lands root-owned and the renderer, which
# runs as uid 10001 with cap_drop ALL, cannot write a PDF into it.
RUN mkdir -p /app/artifacts && chown app:app /app/artifacts

COPY src/ /app/src/
COPY pytest.ini /app/pytest.ini
ENV PYTHONPATH=/app/src

USER app
EXPOSE 8000
CMD ["uvicorn", "job_agent.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
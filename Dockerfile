FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd -r app --gid 10001 && useradd -r -g app --uid 10001 app

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src/ /app/src/
COPY pytest.ini /app/pytest.ini
ENV PYTHONPATH=/app/src

USER app
EXPOSE 8000
CMD ["uvicorn", "job_agent.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
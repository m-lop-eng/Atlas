FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY config ./config
COPY data ./data
COPY research ./research
COPY strategies ./strategies
COPY backtesting ./backtesting
COPY validation ./validation
COPY portfolio ./portfolio
COPY risk ./risk
COPY execution ./execution
COPY brokers ./brokers
COPY monitoring ./monitoring
COPY database ./database
COPY reporting ./reporting
COPY deployment ./deployment

RUN pip install --upgrade pip \
    && pip install .[dev]

CMD ["python", "-m", "pytest", "tests"]
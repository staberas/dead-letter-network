FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY dln ./dln
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home dln && mkdir /data && chown dln:dln /data
USER 10001:10001
ENV DLN_DB=/data/dln.sqlite3
EXPOSE 8000 8001
CMD ["python", "-m", "dln"]

FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 app
COPY . ./
RUN python -c "import main"
USER app
EXPOSE 8080
CMD ["python", "main.py"]

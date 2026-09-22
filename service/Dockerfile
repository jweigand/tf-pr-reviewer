FROM python:3.12-slim

WORKDIR /app

# Dependencies first, so editing the code does not reinstall them on every build.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY reviewer/ ./reviewer/

# -u keeps stdout unbuffered, otherwise logs sit in a buffer and docker compose logs
# shows nothing until the process exits.
CMD ["python", "-u", "-m", "reviewer.main"]

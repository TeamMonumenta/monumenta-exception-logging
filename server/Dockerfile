FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tracker/ tracker/
COPY fix_exception_prompt.md server.py bot.py ./

EXPOSE 8080

CMD ["python", "server.py"]

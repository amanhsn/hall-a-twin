# Runs the full server version anywhere Docker does:
#   docker build -t hall-a-twin . && docker run -p 7860:7860 hall-a-twin
FROM python:3.12-slim

# A non-root user that owns the app folder, since the server writes its live layers to disk.
RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH"
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=user . .

ENV HOST=0.0.0.0 PORT=7860 PYTHONUNBUFFERED=1
EXPOSE 7860
CMD ["python", "server.py", "--no-browser"]

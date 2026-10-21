FROM python:3.13-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
RUN python -c "import subprocess,sys,tomllib; dependencies=tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']; subprocess.check_call([sys.executable,'-m','pip','install','--no-cache-dir',*dependencies])" && useradd --uid 10001 --create-home agentledger
COPY app ./app
RUN pip install --no-cache-dir --no-deps .
COPY seeds ./seeds
COPY reports ./reports
COPY logs ./logs
COPY scripts ./scripts
RUN chown -R agentledger:agentledger /app
USER agentledger
EXPOSE 8080
CMD ["python", "-m", "app"]

FROM nousresearch/hermes-sandbox:desktop
COPY --chown=1000:1000 home/ /home/
ENV HOME=/state/home HERMES_HOME=/state PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /workspace
ENTRYPOINT []
CMD ["sleep", "infinity"]

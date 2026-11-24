FROM mcr.microsoft.com/playwright/python:v1.64.1-noble@sha256:330d401ae23c905896a0c26c66c36fc971b621519e7b11b435a8328d7bf6e031
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp RENTFLOW_ROOT=/check
WORKDIR /check
COPY security/check/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY scripts/security/common.py scripts/security/policy.py scripts/security/check.py ./
COPY scripts/security/inventory_check.py ./
COPY scripts/security/pricing_check.py scripts/security/pricing_observer.py ./
COPY scripts/security/smoke_proxy.py ./
COPY scripts/security/reservation_check.py ./
COPY security/keycloak/ ./security/keycloak/
ENTRYPOINT ["python3", "/check/check.py"]

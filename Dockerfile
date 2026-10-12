# Hosted image for both Railway services. The AEGIS_ROLE environment
# variable set on each service decides what the container does — see
# aegis_bioquant/entrypoint.py:
#     AEGIS_ROLE=web       the website
#     AEGIS_ROLE=scanner   the daily scan
#
# Built on Microsoft's Playwright image because ingestion/fda_scraper.py
# drives a headless Chromium to read the FDA AdCom calendar: this image
# already contains that browser and its system libraries. The tag must
# match the playwright version pinned in requirements.txt. The website
# doesn't need the browser, but sharing one image keeps both services on
# identical code and library versions, with nothing per-service to
# configure on the host.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY aegis_bioquant ./aegis_bioquant

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

CMD ["python", "-m", "aegis_bioquant.entrypoint"]

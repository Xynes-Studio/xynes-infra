FROM python:3.13.7-alpine3.22
COPY fixture.py /fixture.py
USER 65534:65534
HEALTHCHECK --interval=1s --timeout=1s --start-period=2s --start-interval=100ms --retries=1 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ready')"
CMD ["python", "/fixture.py"]

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt ./
# 国内网络默认走清华镜像；海外可直接传 --build-arg PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" -r requirements.txt \
    && groupadd --gid 10001 jinyao \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin jinyao \
    && install -d -o 10001 -g 10001 /data
COPY app ./app

USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

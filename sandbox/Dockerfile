# 阶段6 沙箱镜像：Python + Node + Git 的最小执行环境，非 root 运行。
# 资源限制、网络开关、环境变量白名单都在运行时由 sandbox runner 注入，不写进镜像。
# 本机无法访问 Docker Hub 时，用已存在的 alpine 基座构建：
#   docker build --build-arg BASE_IMAGE=postgres:16-alpine -t blank-sandbox:dev sandbox
ARG BASE_IMAGE=node:24-alpine
FROM ${BASE_IMAGE}

RUN apk add --no-cache python3 py3-pip py3-pytest nodejs npm git bash tini \
  && adduser -D -u 1000 -h /workspace agent \
  && mkdir -p /workspace && chown -R agent:agent /workspace

ENV HOME=/workspace \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /workspace
USER agent
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["sh", "-c", "echo sandbox-ready"]

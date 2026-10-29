# 阶段6 沙箱镜像：Python + Node + Git 的最小执行环境，非 root 运行。
# 资源限制、网络开关、环境变量白名单都在运行时由 sandbox runner 注入，不写进镜像。
# 本机无法访问 Docker Hub 时，用已存在的 alpine 基座构建：
#   docker build --build-arg BASE_IMAGE=postgres:16-alpine -t blank-sandbox:dev sandbox
ARG BASE_IMAGE=node:24-alpine
FROM ${BASE_IMAGE}

RUN apk add --no-cache python3 py3-pip py3-pytest nodejs npm git bash tini \
  && (adduser -D -u 1000 -h /workspace agent || adduser -D -h /workspace agent) \
  && mkdir -p /workspace && chown -R agent:agent /workspace
# 为什么带 || ：node:24-alpine 自带一个 uid 1000 的 node 用户，写死 -u 1000 会以
# "adduser: uid '1000' in use" 直接失败（2026-10-29 CI 第一次真建镜像就撞上）。
# 先试 1000（与既有产物一致），撞了就让 adduser 自己挑下一个空闲 uid——
# 要紧的从来不是这个数字，而是"非 root 且 /workspace 归它"。

ENV HOME=/workspace \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /workspace
USER agent
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["sh", "-c", "echo sandbox-ready"]

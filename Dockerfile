# BuddyCron —— 运行镜像。
#
# 只跑 Python 服务，界面用仓库里已编译的 web/dist，所以镜像里**不需要 Node**。
# 基础镜像用 slim：完整版会多出几十 MB 的编译工具链，这里一个都用不上。
FROM python:3.13-slim

# 不要写 .pyc、不要缓冲输出 —— 前者省磁盘，后者让 journald 能实时看到日志。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 版本号。CI 会通过 build-arg 传进来，和 core/version.py 保持一致。
# 本地构建不传也能跑 —— 代码里的常量就是权威，这个 arg 只是让镜像
# 元数据（label）能和 tag 对上。
ARG BUDDYCRON_VERSION=dev
ENV BUDDYCRON_VERSION=${BUDDYCRON_VERSION}

WORKDIR /app

# 项目本身零第三方依赖，所以没有 requirements.txt 可装。
# 只把需要的文件拷进来，tests/ 和 node_modules/ 不进镜像。
COPY cli.py ./
COPY core/ ./core/
COPY web/ ./web/
COPY deploy/entrypoint.sh /entrypoint.sh

# 数据目录：账号库、配置、日志都在这里，运行时挂载进来。
RUN mkdir -p /data && chmod +x /entrypoint.sh

# **故意不创建专用用户**。挂载出来的数据目录属主是宿主机上的你，
# 容器内换成别的 UID 就写不了 —— 这是 Docker 部署最常见的坑。
# 所以默认以宿主机用户的 UID/GID 运行，由 compose 的 user: 指定。
# 这里只留一个非 root 的默认值兜底，直接 docker run 时也不至于以 root 跑。
USER 1000:1000

# 固定端口 8787。
ENV BUDDYCRON_HOME=/data
EXPOSE 8787

# 健康检查：未登录时首页会 302 到 /login，所以直接判 /login 的 200。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request,sys; \
r=urllib.request.urlopen('http://127.0.0.1:8787/login',timeout=4); \
sys.exit(0 if r.status==200 else 1)"

# 入口脚本同时拉起 Web 服务与容器内定时任务。
# 容器内绑 0.0.0.0：127.0.0.1 在容器网络里只有容器自己能访问，端口映射也转不进来。
# 对外暴露面由 docker 的 -p 127.0.0.1:8787:8787 控制（只映射到宿主机回环）。
ENTRYPOINT ["/entrypoint.sh"]

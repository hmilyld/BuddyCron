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
#
# **必须在这里就 chown**。命名卷首次挂载到一个空的挂载点时，Docker 会把镜像里
# 该目录的内容和属主一起复制过去 —— 不 chown 的话属主是 root:root，而下面
# USER 1000 是运行时才切换的，晚于卷初始化，于是容器写不了 /data。
#
# 这一步是「命名卷 + 非 root 用户」能工作的前提。之前漏了，导致所有用命名卷的
# 部署都报 PermissionError: /data/.config.json。
RUN mkdir -p /data \
    && chown 1000:1000 /data \
    && chmod +x /entrypoint.sh

# 以非 root 运行。
#
# 为什么不是 root：这个服务持有你所有账号的 token，一旦有文件写入或命令执行的
# 漏洞，root 权限会让影响面扩大到整个容器。非 root 是纵深防御。
#
# UID 1000 是绝大多数 Linux 发行版第一个普通用户的 UID。绑定挂载时如果宿主机
# 用户不是 1000，用 compose 的 `user:` 覆盖即可。
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

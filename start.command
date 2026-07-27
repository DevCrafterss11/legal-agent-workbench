#!/bin/bash

set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_HOST="127.0.0.1"
WEB_PORT="${LAWBENCH_PORT:-5180}"
WEB_URL="http://${WEB_HOST}:${WEB_PORT}/"
WEB_LABEL="com.fuhao.legal-agent-workbench.web"
FEISHU_LABEL="com.fuhao.legal-agent-workbench.feishu"
STATE_DIR="${PROJECT_DIR}/.lawbench/run"
LOG_DIR="${PROJECT_DIR}/.lawbench/logs"
WEB_PID_FILE="${STATE_DIR}/web.pid"
WEB_LOG_FILE="${LOG_DIR}/web.log"
FEISHU_PID_FILE="${STATE_DIR}/feishu.pid"
FEISHU_LOG_FILE="${LOG_DIR}/feishu.log"
PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
MILVUS_STARTED=0

info() { printf '\033[1;34m[Legal Workbench]\033[0m %s\n' "$*"; }
ok() { printf '\033[1;32m[完成]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[提示]\033[0m %s\n' "$*"; }

on_error() {
  local exit_code=$?
  printf '\n\033[1;31m[启动失败]\033[0m 请查看上方信息或日志：%s\n' "${WEB_LOG_FILE}" >&2
  if [[ -t 0 ]]; then
    read -r -p "按回车键关闭窗口…" _
  fi
  exit "${exit_code}"
}
trap on_error ERR

web_ready() {
  curl --silent --fail --max-time 3 "${WEB_URL}" >/dev/null 2>&1
}

port_open() {
  nc -z 127.0.0.1 "$1" >/dev/null 2>&1
}

launch_agent_loaded() {
  launchctl print "gui/$(id -u)/$1" >/dev/null 2>&1
}

launch_agent_running() {
  launchctl list | awk -v label="$1" '$3 == label && $1 ~ /^[0-9]+$/ { found=1 } END { exit !found }'
}

valid_pid_file() {
  local pid_file="$1"
  [[ -f "${pid_file}" ]] || return 1
  local pid
  pid="$(tr -cd '0-9' < "${pid_file}")"
  [[ -n "${pid}" ]] && kill -0 "${pid}" >/dev/null 2>&1
}

cd "${PROJECT_DIR}"
mkdir -p "${STATE_DIR}" "${LOG_DIR}"

info "项目目录：${PROJECT_DIR}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  info "未发现虚拟环境，正在创建 .venv…"
  command -v python3 >/dev/null 2>&1 || { warn "请先安装 Python 3.10 或更高版本。"; exit 1; }
  python3 -m venv "${PROJECT_DIR}/.venv"
fi

export PYTHONPATH="${PROJECT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"

if ! "${PYTHON_BIN}" -c "import fastapi, uvicorn, legalworkbench" >/dev/null 2>&1; then
  info "项目依赖不完整，正在安装…"
  "${PYTHON_BIN}" -m pip install -e "${PROJECT_DIR}"
fi

if ! port_open 19530; then
  if command -v docker >/dev/null 2>&1; then
    if ! docker info >/dev/null 2>&1; then
      if [[ "$(uname -s)" == "Darwin" ]] && [[ -d "/Applications/Docker.app" ]]; then
        info "正在启动 Docker Desktop…"
        open -a Docker
        for _ in {1..60}; do
          docker info >/dev/null 2>&1 && break
          sleep 2
        done
      fi
    fi
    if docker info >/dev/null 2>&1; then
      info "正在启动 Milvus 知识库…"
      docker compose -f "${PROJECT_DIR}/docker-compose.milvus.yml" up -d
      MILVUS_STARTED=1
      for _ in {1..60}; do
        port_open 19530 && break
        sleep 2
      done
      port_open 19530 || warn "Milvus 尚未就绪，Web 会先使用降级检索模式。"
    else
      warn "Docker Desktop 未就绪，Web 将使用内存向量库降级运行。"
    fi
  else
    warn "未安装 Docker，Web 将使用内存向量库降级运行。"
  fi
else
  ok "Milvus 已在 19530 端口运行"
fi

if launch_agent_loaded "${WEB_LABEL}"; then
  if ! web_ready || [[ "${MILVUS_STARTED}" == "1" ]] || [[ "${1:-}" == "--restart" ]]; then
    info "正在通过 LaunchAgent 启动 Web 服务…"
    launchctl kickstart -k "gui/$(id -u)/${WEB_LABEL}"
  else
    ok "Web 服务已经运行"
  fi
else
  if [[ "${1:-}" == "--restart" ]] && valid_pid_file "${WEB_PID_FILE}"; then
    old_pid="$(tr -cd '0-9' < "${WEB_PID_FILE}")"
    old_command="$(ps -p "${old_pid}" -o command= 2>/dev/null || true)"
    if [[ "${old_command}" == *"legalworkbench.web:create_app"* ]]; then
      info "正在重启 Web 服务…"
      kill "${old_pid}"
      for _ in {1..20}; do
        kill -0 "${old_pid}" >/dev/null 2>&1 || break
        sleep 0.25
      done
    fi
  fi
  if ! web_ready; then
    info "正在从源码启动 Web 服务…"
    nohup "${PYTHON_BIN}" -m uvicorn legalworkbench.web:create_app \
      --factory --host "${WEB_HOST}" --port "${WEB_PORT}" \
      >>"${WEB_LOG_FILE}" 2>&1 &
    printf '%s\n' "$!" > "${WEB_PID_FILE}"
  else
    ok "Web 服务已经运行"
  fi
fi

info "等待 Web 健康检查…"
for _ in {1..60}; do
  web_ready && break
  sleep 1
done

if ! web_ready; then
  warn "Web 服务未能在 60 秒内启动。最近日志："
  tail -n 30 "${WEB_LOG_FILE}" 2>/dev/null || true
  exit 1
fi

if launch_agent_loaded "${FEISHU_LABEL}"; then
  if ! launch_agent_running "${FEISHU_LABEL}"; then
    info "正在启动飞书长连接…"
    launchctl kickstart -k "gui/$(id -u)/${FEISHU_LABEL}" || warn "飞书监听未启动，不影响 Web 使用。"
  else
    ok "飞书长连接已经运行"
  fi
elif [[ "${LAWBENCH_START_FEISHU:-0}" == "1" ]] && ! valid_pid_file "${FEISHU_PID_FILE}"; then
  info "正在启动飞书长连接…"
  nohup "${PYTHON_BIN}" -c 'from legalworkbench.cli import app; app()' \
    feishu-listen --cwd "${PROJECT_DIR}" >>"${FEISHU_LOG_FILE}" 2>&1 &
  printf '%s\n' "$!" > "${FEISHU_PID_FILE}"
fi

ok "Legal Workbench 已启动：${WEB_URL}"

if [[ "${LAWBENCH_NO_OPEN:-0}" != "1" ]]; then
  if [[ "$(uname -s)" == "Darwin" ]]; then
    open "${WEB_URL}"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "${WEB_URL}" >/dev/null 2>&1 || true
  fi
fi

printf '\n常用方式：\n  双击 start.command\n  终端运行 ./start.command\n  强制重启 ./start.command --restart\n\n'

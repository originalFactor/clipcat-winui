#!/bin/sh
# 安装 / 卸载 Win+V 风格剪贴板面板。
#
#   ./install.sh          安装（覆盖已存在的文件）
#   ./install.sh --uninstall  卸载
#
# 依赖（需自行准备）：
#   clipcatd / clipcatctl    https://github.com/xrelkd/clipcat
#   python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1
#   xdotool, x11-utils (xwininfo)
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

BIN_DIR="${HOME}/.local/bin"
PANEL_DIR="${HOME}/.local/share/clipcat-winui"
CONFIG_DIR="${HOME}/.config/clipcat"
UNIT_DIR="${HOME}/.config/systemd/user"
EXT_DIR="${HOME}/.local/share/gnome-shell/extensions/caretlocator@local"

say() { printf '%s\n' "$*"; }

uninstall() {
    say "== 卸载 =="
    systemctl --user disable --now clipcatd 2>/dev/null || true
    rm -f "${UNIT_DIR}/clipcatd.service"
    systemctl --user daemon-reload 2>/dev/null || true

    gnome-extensions disable caretlocator@local 2>/dev/null || true
    rm -rf "${EXT_DIR}"

    rm -f "${BIN_DIR}/clipcat-winui"
    rm -rf "${PANEL_DIR}"

    say "配置保留在 ${CONFIG_DIR}（如不再需要可手动删除）"
    say "完成。注销后扩展才彻底卸载。"
}

install_all() {
    say "== 检查依赖 =="
    for cmd in xdotool xwininfo; do
        command -v "$cmd" >/dev/null 2>&1 || say "缺少命令: $cmd"
    done
    # 必须用系统 python：conda/nvm 之类的前置 python 通常没有 gi 绑定
    if [ ! -x /usr/bin/python3 ]; then
        say "缺少 /usr/bin/python3"
    elif ! /usr/bin/python3 -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1"); from gi.repository import Gtk, Adw' 2>/dev/null; then
        say "缺少 PyGObject 的 GTK4/Adw 绑定（apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1）"
    fi
    command -v clipcatd >/dev/null 2>&1 || say "缺少 clipcatd（见 README）"

    say "== 安装面板 =="
    mkdir -p "${BIN_DIR}" "${PANEL_DIR}" "${CONFIG_DIR}" "${UNIT_DIR}"
    install -m 0644 "${REPO_DIR}/panel/clipcat-winui.py" "${PANEL_DIR}/clipcat-winui.py"

    # 启动器里的 @PANEL_DIR@ 换成真实路径
    sed "s|@PANEL_DIR@|${PANEL_DIR}|g" "${REPO_DIR}/bin/clipcat-winui" > "${BIN_DIR}/clipcat-winui"
    chmod 0755 "${BIN_DIR}/clipcat-winui"

    say "== 安装 GNOME Shell 扩展 =="
    mkdir -p "${EXT_DIR}"
    install -m 0644 "${REPO_DIR}/extension/extension.js" "${EXT_DIR}/extension.js"
    install -m 0644 "${REPO_DIR}/extension/metadata.json" "${EXT_DIR}/metadata.json"

    say "== 安装配置（已存在则保留）=="
    for f in clipcatd.toml clipcat-menu.toml; do
        if [ -f "${CONFIG_DIR}/${f}" ]; then
            say "  跳过 ${f}（已存在）"
        else
            install -m 0644 "${REPO_DIR}/config/${f}" "${CONFIG_DIR}/${f}"
        fi
    done

    say "== 安装 systemd 单元 =="
    sed -e "s|@BIN_DIR@|${BIN_DIR}|g" -e "s|@CONFIG_DIR@|${CONFIG_DIR}|g" \
        "${REPO_DIR}/config/clipcatd.service.in" > "${UNIT_DIR}/clipcatd.service"
    systemctl --user daemon-reload
    systemctl --user enable --now clipcatd

    say "== 启用扩展 =="
    # 首次安装时 Shell 还没扫描到这个新扩展，这条命令通常无效但不报错，
    # 所以下面把必须手动做的事明确打印出来。
    gnome-extensions enable caretlocator@local 2>/dev/null || true

    say ""
    say "完成。还需要以下手动步骤："
    say ""
    say "  1) 装扩展管理器（若未安装）"
    say "       sudo apt install gnome-shell-extension-manager"
    say ""
    say "  2) 注销并重新登录"
    say "       GNOME Shell 有 ESM 模块缓存，扩展无法热重载"
    say "       Wayland 下也没有 Alt+F2 -> r，只能注销"
    say ""
    say "  3) 重新登录后打开 Extension Manager -> Installed"
    say "       找到 Caret Locator，手动把开关打开"
    say "       （脚本里的 gnome-extensions enable 首次安装往往无效）"
    say ""
    say "  4) 设置 -> 键盘 -> 自定义快捷键"
    say "       把 Super+V 绑到 ${BIN_DIR}/clipcat-winui"
    say ""
    say "  验证扩展是否真的加载："
    say "    gdbus call --session --dest org.gnome.Shell \\"
    say "      --object-path /io/github/clipcat/CaretLocator \\"
    say "      --method io.github.clipcat.CaretLocator.GetDebug"
    say ""
    say "  守护进程状态: systemctl --user status clipcatd"
}

case "${1:-}" in
    --uninstall|-u) uninstall ;;
    ""|--install|-i) install_all ;;
    *) say "用法: $0 [--install|--uninstall]"; exit 1 ;;
esac

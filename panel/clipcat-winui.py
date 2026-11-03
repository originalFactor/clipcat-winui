#!/usr/bin/python3
"""Win+V 风格的 clipcat 剪贴板历史面板。

数据源：clipcatd 的 D-Bus 接口 org.clipcat.clipcat.Manager
  - List(preview_length) -> a(tsuxs)   id, mime, kind, timestamp, preview
  - Get(id)              -> (tayusx)   id, bytes, mime, kind, timestamp
  - Mark(id, kind)       -> 把某条写回剪贴板
  - Remove(id)           -> 删除

界面：GTK4 + libadwaita，卡片网格，文本卡片显示多行内容，图片卡片显示缩略图，
顶部搜索框，方向键移动、回车选中、Delete 删除、Esc 关闭。
"""

import json
import os
import re
import subprocess
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
gi.require_version("Adw", "1")
from gi.repository import (  # noqa: E402
    Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango, PangoCairo,
)

BUS_NAME = "org.clipcat.clipcat"
OBJ_PATH = "/org/clipcat/clipcat/manager"
IFACE = "org.clipcat.clipcat.Manager"

# 窗口尺寸记忆文件（放在 XDG_STATE_HOME 下，随用户状态走）
def _state_path():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "clipcat-winui", "window.json")


CARET_BUS = "org.gnome.Shell"
CARET_PATH = "/io/github/clipcat/CaretLocator"
CARET_IFACE = "io.github.clipcat.CaretLocator"


_caret_iface_missing = False


def query_pointer_pos():
    """问 GNOME Shell 扩展要鼠标指针的屏幕坐标，失败返回 None。

    为什么用指针而不是文本光标：本机 IBus 的 org.freedesktop.IBus.Panel
    服务名无人持有，IBusManager 的 set-cursor-location 在外部应用打字时
    从不触发（只有面板自己获得焦点时才有值），所以那条路拿不到数据。
    global.get_pointer() 是合成器内部 API，实测准确且始终可用。
    """
    global _caret_iface_missing
    if _caret_iface_missing:
        return None
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        res = bus.call_sync(
            CARET_BUS, CARET_PATH, CARET_IFACE, "GetPointer",
            None, GLib.VariantType("((ii))"),
            Gio.DBusCallFlags.NONE, 600, None,
        )
        x, y = res.unpack()[0]
        if x < 0 or y < 0:
            return None
        return {"x": x, "y": y, "w": 0, "h": 0}
    except GLib.Error as err:
        # 只有「接口真的不存在」才永久记住，避免每次开面板都探 D-Bus。
        # 超时、忙之类的临时错误不缓存，下次仍会重试——
        # 否则扩展后加载上来也会一直读不到。
        if "UnknownMethod" in err.message or "UnknownInterface" in err.message \
                or "ServiceUnknown" in err.message or "NameHasNoOwner" in err.message:
            _caret_iface_missing = True
        return None


# xdotool windowmove 的目标坐标在 X 轴上就是内容区坐标（实测无偏移），
# 但 Y 轴会被 mutter 按标题栏高度整体下移 37px（实测 100->137、300->337）。
# 这里把这段偏移补回去。
FRAME_OFFSET_Y = 37


def _frame_size(xid):
    """取内容窗口所在外框（mutter-x11-frames）的宽高；失败返回 (0, 0)。

    xwininfo -tree 是多层缩进的树，内容窗口的父节点就是装饰框。
    """
    try:
        tree = subprocess.run(["xwininfo", "-root", "-tree"],
                              capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return (0, 0)

    lines = tree.splitlines()
    for idx, line in enumerate(lines):
        if "0x" not in line:
            continue
        # 找到内容窗口那一行
        toks = line.split()
        if not toks or not toks[0].startswith("0x"):
            continue
        if toks[0].lower() != xid.lower():
            continue
        indent = len(line) - len(line.lstrip())
        # 往上找缩进更小的那一行，就是父框
        for back in range(idx - 1, -1, -1):
            pline = lines[back]
            if not pline.strip():
                continue
            pindent = len(pline) - len(pline.lstrip())
            if pindent < indent:
                # 形如 283x503+683+364，取尺寸部分
                import re as _re
                mm = _re.search(r"(\d+)x(\d+)\+", pline)
                if mm:
                    return (int(mm.group(1)), int(mm.group(2)))
                break
    return (0, 0)


def move_window_to_caret(win, caret, win_w, win_h):
    """把窗口摆到光标附近；空间不够时自动翻到另一侧。

    只有跑在 Xwayland 上才有效——Wayland 下应用不能给自己定位，
    这也是启动脚本要设 GDK_BACKEND=x11 的原因。
    """
    import subprocess

    screen_w, screen_h = 2720, 1024
    try:
        out = subprocess.run(["xrandr", "--current"], capture_output=True,
                             text=True, timeout=1).stdout
        for line in out.splitlines():
            if " current " in line:
                part = line.split(" current ")[1].split(",")[0]
                screen_w = int(part)
                rest = line.split(" current ")[1].split("x")[1]
                screen_h = int(rest.split()[0])
                break
    except (OSError, subprocess.SubprocessError, ValueError):
        pass

    gap = 8
    px, py = caret["x"], caret["y"]

    # 外框比内容大（标题栏 + 边框），翻转判断必须按外框算，
    # 否则贴着右/下边缘时会算得过窄而溢出屏幕。
    # 首次打开时还没量过装饰尺寸，用实测常量兜底，
    # 否则翻转判断会按内容宽度算，贴着右边缘会溢出。
    frame_dx = getattr(win, "_frame_extra_w", 50)
    frame_dy = getattr(win, "_frame_extra_h", 87)
    outer_w = win_w + frame_dx
    outer_h = win_h + frame_dy

    # 以指针为角点向右下展开（类似右键菜单），
    # 右下放不下就往反方向翻，最后夹回屏幕内。
    x = px + gap
    y = py + gap

    if x + outer_w > screen_w:
        x = px - outer_w - gap
    if y + outer_h > screen_h:
        y = py - outer_h - gap

    x = max(0, min(x, screen_w - outer_w))
    y = max(0, min(y, screen_h - outer_h))

    try:
        # 优先用 GTK 自己报告的 X11 窗口号：不用去猜哪个窗口才是面板，
        # 也不会误拿到 1x1 的辅助窗或别的进程的同名窗口。
        best = None
        try:
            surface = win.get_surface()
            if surface is not None and hasattr(surface, "get_xid"):
                best = str(surface.get_xid())
        except Exception:
            best = None

        if best is None:
            # 拿不到 xid 时退回按 pid 找，取面积最大的那个
            out = subprocess.run(
                ["xdotool", "search", "--pid", str(os.getpid())],
                capture_output=True, text=True, timeout=2,
            ).stdout.split()
            best_area = -1
            for wid in out:
                geo = subprocess.run(["xwininfo", "-id", wid],
                                     capture_output=True, text=True, timeout=2).stdout
                iw = ih = 0
                for line in geo.splitlines():
                    t = line.strip()
                    if t.startswith("Width:"):
                        iw = int(t.split()[1])
                    elif t.startswith("Height:"):
                        ih = int(t.split()[1])
                if iw * ih > best_area:
                    best, best_area = wid, iw * ih
        if best is None:
            return False

        def _content_xy():
            """回读内容窗口的实际左上角坐标。"""
            geo = subprocess.run(["xwininfo", "-id", best],
                                 capture_output=True, text=True, timeout=2).stdout
            cx = cy = None
            for line in geo.splitlines():
                t = line.strip()
                if t.startswith("Absolute upper-left X:"):
                    cx = int(t.split(":")[1])
                elif t.startswith("Absolute upper-left Y:"):
                    cy = int(t.split(":")[1])
            return cx, cy

        def _frame_xy():
            """回读外框（父窗口）左上角坐标。

            xwininfo -tree -id 会给出 Parent window id，就是 mutter 的装饰框。
            """
            head = subprocess.run(["xwininfo", "-tree", "-id", best],
                                  capture_output=True, text=True, timeout=2).stdout
            parent = None
            for line in head.splitlines():
                t = line.strip()
                if t.startswith("Parent window id:"):
                    parent = t.split(":")[1].split()[0]
                    break
            if parent is None:
                return _content_xy()

            geo = subprocess.run(["xwininfo", "-id", parent],
                                 capture_output=True, text=True, timeout=2).stdout
            fx = fy = None
            for line in geo.splitlines():
                t = line.strip()
                if t.startswith("Absolute upper-left X:"):
                    fx = int(t.split(":")[1])
                elif t.startswith("Absolute upper-left Y:"):
                    fy = int(t.split(":")[1])
            if fx is None or fy is None:
                return _content_xy()
            return fx, fy

        def _move(tx, ty):
            subprocess.run(["xdotool", "windowmove", "--sync", best,
                            str(tx), str(ty)], timeout=2, check=False)

        # x 轴无偏移；y 轴被 mutter 按标题栏高度下移，先减掉再移动。
        _move(x, y - FRAME_OFFSET_Y)
        ax, ay = _content_xy()

        # 顺便量出外框相对内容多出多少，下次翻转判断要用
        try:
            fw, fh = _frame_size(best)
            if fw and fh and ax is not None and ay is not None:
                cx, cy = ax, ay
                win._frame_extra_w = max(fw - win_w, 0)
                win._frame_extra_h = max(fh - win_h, 0)
        except Exception:
            pass

        ok = ax is not None and ay is not None
        return ok and abs((ax or 0) - x) <= 4 and abs((ay or 0) - y) <= 4
    except (OSError, subprocess.SubprocessError):
        return False


def load_window_state():
    """读上次窗口尺寸，失败时返回 (默认宽, 默认高)。"""
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        w, h = int(data.get("width", 0)), int(data.get("height", 0))
        # 太小的尺寸没有可用性（卡片挤成一列还放不下），
        # 低于下限就当作没存过，回到默认尺寸。
        if w >= MIN_WINDOW_W and h >= MIN_WINDOW_H:
            return w, h
    except (OSError, ValueError, TypeError):
        pass
    return DEFAULT_W, DEFAULT_H


def save_window_state(width, height, maximized=False):
    """记住当前窗口尺寸。"""
    path = _state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"width": int(width), "height": int(height),
                       "maximized": bool(maximized)}, f)
    except OSError:
        pass


DEFAULT_W = 1020
DEFAULT_H = 620
# 允许记住的窗口尺寸下限。
# 只拦明显异常的数值（布局算不出列数会崩），不做"最小可用尺寸"限制——
# 用户把小窗口当习惯就该被尊重。
MIN_WINDOW_W = 180
MIN_WINDOW_H = 150

CARD_MIN_W = 150
# 卡片高度按内容自然撑开，这里只是最小高度下限
CARD_MIN_H = 96
# 文本卡片最多显示多少行，超出就截断（保证卡片不会无限长）
TEXT_MAX_LINES = 6
TEXT_MAX_CHARS = 180
# 卡片最多显示多少「视觉行」（换行后），控制卡片高度上限
MAX_VISUAL_LINES = 5
# 列数不再写死：窗口变窄时允许减到 1 列，变宽时自动增列。
MIN_COLS = 1
MAX_COLS = 5
THUMB_MAX = 72
PREVIEW_LEN = 400


class ClipcatClient:
    """对 clipcatd D-Bus 接口的薄封装。"""

    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def _call(self, method, params, reply_type, timeout=4000):
        return self.bus.call_sync(
            BUS_NAME, OBJ_PATH, IFACE, method,
            params, reply_type, Gio.DBusCallFlags.NONE, timeout, None,
        )

    def list(self):
        """返回 [(id, mime, kind, ts, preview), ...]，最近的在前。

        注意：metadata 里的 kind 是「最近一次被 mark 到哪种选区」，不是固定
        属性，同一个 clip 可能先被 mark 为 clipboard 再被 mark 为 primary，
        所以这里不过滤 kind，只按时间倒序并去重。
        """
        res = self._call("List", GLib.Variant("(t)", (PREVIEW_LEN,)),
                         GLib.VariantType("(a(tsuxs))"))
        rows = res.unpack()[0]
        seen = set()
        clips = []
        for r in sorted(rows, key=lambda r: r[3], reverse=True):
            if r[0] in seen:
                continue
            seen.add(r[0])
            clips.append(r)
        return clips

    def get(self, clip_id):
        # D-Bus 结构是 (tayusx)，即 (id, data, kind, mime, timestamp)：
        # kind 是 u，排在 mime 的 s 前面，别按 proto 里的字段顺序想当然。
        res = self._call("Get", GLib.Variant("(t)", (clip_id,)),
                         GLib.VariantType("(tayusx)"))
        cid, data, kind, mime, ts = res.unpack()
        return {"id": cid, "data": bytes(data), "mime": mime, "kind": kind, "ts": ts}

    def mark(self, clip_id):
        self._call("Mark", GLib.Variant("(tu)", (clip_id, 0)),
                   GLib.VariantType("(b)"))

    def remove(self, clip_id):
        self._call("Remove", GLib.Variant("(t)", (clip_id,)),
                   GLib.VariantType("(b)"))

    def length(self):
        res = self._call("List", GLib.Variant("(t)", (1,)),
                         GLib.VariantType("(a(tsuxs))"))
        return len(res.unpack()[0])


def _wrap_lines(text, width_px, pango_ctx=None, font_size_pt=11.0):
    """用 Pango 在指定像素宽度下折行，返回 (各行文本, 是否被截断)。

    之前按固定字数折行，宽度变化时既不合理，又因为「折过行」被误判成截断，
    导致内容完整显示时也挂个省略号。这里交给 Pango 按真实字体度量算。
    """
    if width_px <= 0:
        width_px = 188
    if pango_ctx is None:
        pango_ctx = Gtk.Label().get_pango_context()
    layout = Pango.Layout.new(pango_ctx)
    layout.set_width(int(width_px * Pango.SCALE))
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    desc = Pango.FontDescription()
    desc.set_size(int(font_size_pt * Pango.SCALE))
    layout.set_font_description(desc)

    # 逐条原始行按宽度折行，再累计到行数上限。
    # 保留原有换行结构（代码片段、列表等不会被压成一整段），
    # 只有真正放不下时才标记为截断。
    sources = text.splitlines()
    out_lines = []
    consumed_all = True
    for idx, raw in enumerate(sources):
        if not raw:
            out_lines.append("")
        else:
            layout.set_text(raw, -1)
            total = len(raw)
            for i in range(layout.get_line_count()):
                line = layout.get_line(i)
                begin = min(line.start_index, total)
                finish = min(begin + line.length, total)
                out_lines.append(raw[begin:finish])
        if len(out_lines) >= MAX_VISUAL_LINES:
            # 这一条源行还没处理完，或者后面还有源行没轮到，都算截断
            consumed_all = (idx == len(sources) - 1) and len(out_lines) <= MAX_VISUAL_LINES
            break
    truncated = not consumed_all or len(out_lines) > MAX_VISUAL_LINES
    return out_lines[:MAX_VISUAL_LINES], truncated


def trim_preview(text, width_px=188, pango_ctx=None, font_size_pt=11.0):
    """按可用宽度折行并限制行数；只有真的超出才加省略号。"""
    text = text.rstrip()
    if not text:
        return ""
    lines, truncated = _wrap_lines(text, width_px, pango_ctx, font_size_pt)
    shown = "\n".join(lines)
    if len(shown) > TEXT_MAX_CHARS:
        shown = shown[:TEXT_MAX_CHARS].rstrip()
        truncated = True
    if truncated:
        shown = shown.rstrip() + "…"
    return shown


def relative_time(ts):
    """把 D-Bus 的 unix 秒转成「刚刚 / 12 分钟前 / 3 小时前」。"""
    import time as _time

    try:
        delta = int(_time.time()) - int(ts)
    except (TypeError, ValueError):
        return ""
    if delta < 0:
        delta = 0
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{delta // 60} 分钟前"
    if delta < 86400:
        return f"{delta // 3600} 小时前"
    return f"{delta // 86400} 天前"


def human_size(n):
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


class ClipCard(Gtk.Button):
    """单张剪贴板卡片。"""

    def __init__(self, clip_id, mime, preview, timestamp, on_activate, on_delete):
        super().__init__()
        self.clip_id = clip_id
        self.mime = mime
        self.preview = preview
        # 完整正文（Get 拿到真实字节后填这里），宽度变化时用它重新折行
        self.full_text = preview
        self._text_width = 0
        self.timestamp = timestamp
        self.on_activate = on_activate
        self.on_delete = on_delete

        self.add_css_class("clip-card")
        # 最小宽度必须所有卡片一致，否则 Gtk.Box 会按最小宽度比例分配剩余空间，
        # 导致同排卡片宽窄不一。
        self.set_size_request(CARD_MIN_W, CARD_MIN_H)
        # 理想宽度：限制超长文本把卡片撑得过高
        self.set_size_request(CARD_MIN_W, CARD_MIN_H)
        self.set_hexpand(True)
        self.set_vexpand(True)
        # FILL：填满所在行的高度，同一行卡片因此等高；
        # 行与行之间仍各自按内容决定高度（Grid 的 row_homogeneous=False）。
        self.set_valign(Gtk.Align.FILL)
        self.set_halign(Gtk.Align.FILL)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)
        # FlowBox 会把同行卡片拉成等高，内容自己贴顶才不会出现大片空白
        # 内容贴顶，卡片被拉高时脚注仍留在顶部区块之下
        box.set_valign(Gtk.Align.FILL)
        box.set_vexpand(True)
        self.set_child(box)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        # 正文区吃掉剩余高度，脚注因此被推到卡片底部
        self.stack.set_vexpand(True)
        self.stack.set_valign(Gtk.Align.FILL)
        box.append(self.stack)

        if mime == "image/png":
            self._build_image_side()
        else:
            self._build_text_side()

        meta = Gtk.Label(label=self.footer_text())
        meta.add_css_class("dim-label")
        meta.add_css_class("clip-meta")
        meta.set_vexpand(False)
        meta.set_valign(Gtk.Align.END)
        meta.set_xalign(0)
        box.append(meta)

        # 宽度变化后按新宽度重新折行，resize 时内容跟着调整
        self.connect("notify::width", self._on_width_changed)

        # Delete 键删除当前卡片（冒泡阶段即可，不抢窗口的按键）
        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)

    def _on_width_changed(self, *_args):
        if self.mime == "image/png":
            return
        width = self._text_area_width()
        if abs(width - self._text_width) < 8:
            return
        self._text_width = width
        label = self.stack.get_child_by_name("text")
        if label is not None:
            label.set_text(trim_preview(self.full_text, width,
                                        self.get_pango_context()))

    def footer_text(self):
        """卡片脚注：左侧类型/长度，右侧相对时间，观感对齐 Win+V。"""
        if self.mime == "image/png":
            kind = "图片"
        else:
            kind = f"{len(self.preview)} 字符"
        ago = relative_time(self.timestamp)
        return f"{kind} · {ago}" if ago else kind

    def _text_area_width(self):
        """正文区可用宽度：卡片宽 - 左右内边距（12*2）。"""
        w = self.get_width()
        if w <= 1:
            w = getattr(self, "_planned_width", CARD_MIN_W)
        return max(w - 24, 60)

    def _build_text_side(self):
        label = Gtk.Label(
            label=trim_preview(self.preview, self._text_area_width(),
                               self.get_pango_context()))
        label.set_wrap(True)
        label.set_wrap_mode(2)      # PANGO_WRAP_WORD_CHAR
        label.set_xalign(0)
        label.set_yalign(0)
        # 行数上限在 trim_preview 里预先算好，标签本身只管换行，
        # 高度就会随内容自然变化（短内容矮、长内容高但有上限）。
        label.set_vexpand(False)
        label.set_valign(Gtk.Align.START)
        label.add_css_class("clip-text")
        self.stack.add_named(label, "text")

    def _build_image_side(self):
        # 用 Gtk.Picture 而不是固定 72px 的 Gtk.Image：
        # Picture 会按分配到的空间缩放并保持比例，内容填满卡片。
        pic = Gtk.Picture()
        pic.set_can_shrink(True)
        pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        pic.set_halign(Gtk.Align.FILL)
        pic.set_valign(Gtk.Align.FILL)
        pic.set_hexpand(True)
        pic.set_vexpand(True)
        # 占位高度，图片真正加载前卡片不会塌成一条
        pic.set_size_request(-1, THUMB_MAX)
        self.stack.add_named(pic, "image")
        self._thumb = pic

    def set_real_text(self, text):
        """用 Get 拿到的真实字节覆盖正文，修掉 preview 里字面 \\n 的问题。"""
        if self.mime == "image/png":
            return
        label = self.stack.get_child_by_name("text")
        if label is None:
            return
        self.full_text = text
        label.set_text(trim_preview(text, self._text_area_width(),
                                    self.get_pango_context()))

    def load_thumbnail(self, data):
        """把图片字节交给 Gtk.Picture，缩放由控件自己按可用空间处理。"""
        if self.mime != "image/png":
            return
        try:
            loader = GdkPixbuf.PixbufLoader.new_with_type("png")
            loader.write(data)
            loader.close()
            pix = loader.get_pixbuf()
            if pix is None:
                raise ValueError("no pixbuf")
            # 先降到合理分辨率，避免超大图每次重绘都全尺寸缩放
            w, h = pix.get_width(), pix.get_height()
            if max(w, h) > 512:
                scale = 512 / max(w, h)
                pix = pix.scale_simple(max(1, int(w * scale)),
                                       max(1, int(h * scale)),
                                       GdkPixbuf.InterpType.BILINEAR)
            texture = Gdk.Texture.new_for_pixbuf(pix)
            self._thumb.set_paintable(texture)
        except Exception:
            # 解码失败就什么都不放，字号脚注仍会显示「图片」
            self._thumb.set_paintable(None)

    def _on_key(self, controller, keyval, keycode, state):
        if keyval == Gdk.KEY_Delete:
            self.on_delete(self.clip_id)
            return True
        return False


class ClipboardWindow(Gtk.ApplicationWindow):
    def __init__(self, app, client: ClipcatClient):
        super().__init__(application=app)
        self.client = client
        self.cards = []
        self.selected = 0
        self._thumb_pending = []

        self.set_title("剪贴板历史")
        # 恢复上次的窗口尺寸
        w, h = load_window_state()
        self.set_default_size(w, h)
        self.add_css_class("clip-window")
        # 保留系统边框：去掉装饰后 mutter 不再把这个窗口当受管窗口，
        # notify::is-active 等焦点通知就断了，失焦自动关闭会失效。
        # 有边框时窗口由 mutter 正常管理，焦点事件正常。
        self.set_decorated(True)
        self._size_timer = 0
        self._pending_size = None
        # 失去焦点自动关闭。等窗口真正激活后才开始监听，
        # 否则弹出的瞬间 is-active 还是 false，会立刻被关掉。
        self._focus_watch_id = 0
        self._closing = False
        self._got_focus = False
        # 焦点检测在 Xwayland 下没有可靠通路：
        # notify::is-active 不触发、EventControllerFocus 只跟踪窗口内部、
        # _NET_ACTIVE_WINDOW 看不到 Xwayland 窗口。
        # 这里保留 is-active（原生 Wayland 下有效），X11 下退化为不自动关闭。
        self.connect("notify::is-active", self._on_active_changed)
        self.connect("notify::width", self._remember_size)
        self.connect("notify::height", self._remember_size)
        self.connect("close-request", self._on_close_request)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_top(14)
        header.set_margin_bottom(8)
        header.set_margin_start(16)
        header.set_margin_end(16)

        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("搜索剪贴板历史…")
        self.search.set_hexpand(True)
        self.search.connect("search-changed", self._on_search)
        header.append(self.search)

        # 无边框窗口的拖动把手。
        # 不用 Gtk.WindowHandle：它会把拖动语义铺满整个窗口，
        # 导致鼠标在任何位置都显示拖动光标。这里只给这一小块装控制器。
        self.count_label = Gtk.Label()
        self.count_label.add_css_class("dim-label")
        header.append(self.count_label)
        root.append(header)

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_vexpand(True)
        self.scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        root.append(self.scroller)

        # 用 Gtk.Grid 排版：
        #  - column_homogeneous=True  列宽均分，所有卡片一样宽
        #  - row_homogeneous=False    行高由该行最高的卡片决定，不额外留白
        # FlowBox 做不到这两点的组合（homogeneous 会连行高一起拉平）。
        self.grid = Gtk.Grid()
        self.grid.set_valign(Gtk.Align.START)
        self.grid.set_hexpand(True)
        self.grid.set_column_homogeneous(True)
        self.grid.set_row_homogeneous(False)
        self.grid.set_column_spacing(10)
        self.grid.set_row_spacing(10)
        self.grid.set_margin_top(4)
        self.grid.set_margin_bottom(16)
        self.grid.set_margin_start(16)
        self.grid.set_margin_end(16)
        self.scroller.set_child(self.grid)
        self._rows = []

        self.set_child(root)

        # 无边框窗口在 X11 下拿不到系统提供的拉边缩放，
        # 边缘的点击又被合成器当作窗口管理区域吃掉，收不到事件。
        # 因此改用 Ctrl+滚轮 调整窗口尺寸：可靠且不依赖窗口管理器。
        scroll = Gtk.EventControllerScroll()
        scroll.set_flags(Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        scroll.connect("scroll", self._on_scroll_resize)
        root.add_controller(scroll)

        # CAPTURE 阶段先拿到按键：否则焦点在搜索框时，Esc 会被
        # Gtk.SearchEntry 自己消费掉（清空文字并释放焦点），窗口收不到。
        key = Gtk.EventControllerKey()
        key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)

        # 窗口宽度变化时按新宽度重排列数
        self._last_cols = None
        self.connect("notify::default-width", self._on_resize)
        self.scroller.connect("notify::width", self._on_resize)
        self.connect("map", self._on_mapped)

        self._install_css()
        self.reload()

    def _on_scroll_resize(self, controller, dx, dy):
        """Ctrl+滚轮 缩放窗口；不按 Ctrl 时保持滚轮原本的滚动行为。"""
        state = controller.get_current_event_state()
        if not (state & Gdk.ModifierType.CONTROL_MASK):
            return False
        step = 40
        w, h = self.get_width(), self.get_height()
        if dy < 0:
            w, h = w + step, h + step
        elif dy > 0:
            w, h = w - step, h - step
        else:
            return False
        w = max(MIN_WINDOW_W, min(w, 2400))
        h = max(MIN_WINDOW_H, min(h, 1600))
        self.set_default_size(w, h)
        return True

    def _on_active_changed(self, *_args):
        """窗口失去键盘焦点时关闭，行为对齐 Win+V。"""
        if self.is_active():
            # 已经激活，之后任何失去焦点都算用户点到了别处
            self._got_focus = True
            return
        if not getattr(self, "_got_focus", False):
            # 还没真正激活过，忽略刚弹出时的波动
            return
        if getattr(self, "_closing", False):
            return
        self._closing = True
        self.close()

    def _remember_size(self, *_args):
        """窗口尺寸变化时记住它（带防抖，拖拽时不会每像素都写盘）。"""
        if self.is_maximized():
            return
        w, h = self.get_width(), self.get_height()
        if w < MIN_WINDOW_W or h < MIN_WINDOW_H:
            # 布局尚未稳定时会短暂出现极小尺寸，不值得写盘
            return
        self._pending_size = (w, h)
        if getattr(self, "_size_timer", 0):
            GLib.source_remove(self._size_timer)
        self._size_timer = GLib.timeout_add(400, self._flush_size)

    def _flush_size(self):
        self._size_timer = 0
        pending = getattr(self, "_pending_size", None)
        if pending:
            save_window_state(*pending)
        return False

    def _on_close_request(self, *_args):
        if getattr(self, "_size_timer", 0):
            GLib.source_remove(self._size_timer)
            self._size_timer = 0
        pending = getattr(self, "_pending_size", None)
        if pending and not self.is_maximized():
            save_window_state(*pending)
        elif not self.is_maximized():
            w, h = self.get_width(), self.get_height()
            if w >= MIN_WINDOW_W and h >= MIN_WINDOW_H:
                save_window_state(w, h)
        return False

    def _on_resize(self, *_args):
        if getattr(self, "_in_relayout", False):
            return
        cols = self._cols()
        if cols != self._last_cols:
            self._last_cols = cols
            self._relayout()

    def _on_mapped(self, *_args):
        # 窗口真正拿到尺寸后再排一次，避免用兜底宽度分行
        self._on_resize()
        # 部分合成器/时序下 is-active 的通知可能没到，
        # 这里主动补一次，确保 _got_focus 被置上，否则失焦关闭永不生效。
        def _ensure_focus_flag():
            if self.is_active():
                self._got_focus = True
            return False
        GLib.timeout_add(250, _ensure_focus_flag)

        # 在 map 阶段（窗口尚未真正显示）就设定位置，
        # 直接开在目标处，用户看不到"先出现在默认位置再被挪走"的跳动。
        self._place_near_caret()

    def _place_near_caret(self):
        """把窗口摆到打开前的鼠标指针附近。

        用打开前的快照而不是实时查询：面板 present() 后焦点转移到搜索框，
        若此时再取位置，锚点可能已经变成面板自身。
        """
        caret = getattr(self, "pointer_snapshot", None)
        if caret is None:
            return False

        # map 阶段控件尚未分配尺寸，退回 set_default_size 的值，
        # 否则会因 w/h <= 1 直接放弃定位。
        w, h = self.get_width(), self.get_height()
        if w <= 1 or h <= 1:
            w, h = self.get_default_size()
        if w <= 1 or h <= 1:
            w, h = 1020, 620

        move_window_to_caret(self, caret, w, h)
        return False

    def _install_css(self):
        css = """
        .clip-window { background-color: @window_bg_color; }
        .clip-card {
            background-color: alpha(@card_bg_color, 0.9);
            border: 1px solid alpha(@borders, 0.55);
            border-radius: 10px;
            padding: 0;
            box-shadow: 0 1px 2px rgba(0,0,0,0.10);
        }
        .clip-card:hover {
            background-color: alpha(@accent_bg_color, 0.14);
            border-color: alpha(@accent_bg_color, 0.55);
        }
        .clip-card.selected-card, .clip-card:focus {
            border-color: @accent_bg_color;
            box-shadow: 0 0 0 2px alpha(@accent_bg_color, 0.55);
            outline: none;
        }
        .clip-text { font-size: 11pt; }
        .clip-meta { font-size: 8pt; opacity: 0.6; }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    # ---------- 数据加载 ----------

    def reload(self):
        # 清空旧的行与卡片
        child = self.grid.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.grid.remove(child)
            child = nxt
        self._rows = []
        self.cards = []
        self.selected = 0
        self._thumb_pending = []

        try:
            clips = self.client.list()
        except GLib.Error:
            clips = []

        for clip_id, mime, _kind, ts, preview in clips:
            card = ClipCard(clip_id, mime, preview, ts, self._activate, self._delete)
            card.connect("clicked", lambda _b, cid=clip_id: self._activate(cid))
            self.cards.append(card)
            self._thumb_pending.append(card)

        self._relayout()
        self.count_label.set_text(f"{len(self.cards)} 项" if self.cards else "暂无记录")
        if self.cards:
            self._set_selected(0)
            GLib.idle_add(self._drain_thumbs)

    def _cols(self):
        """按当前可用宽度算列数：卡片至少 CARD_MIN_W 宽。"""
        width = self.scroller.get_width()
        if width <= 1:
            width = self.get_width()
        if width <= 1:
            width = self.get_default_size()[0] or 1020
        usable = max(width - 32, CARD_MIN_W)      # 减掉左右 margin
        cols = int((usable + 10) // (CARD_MIN_W + 10))
        return max(MIN_COLS, min(MAX_COLS, cols))

    def _relayout(self):
        """把卡片重新分到各行：每行一个横向 Box，卡片等宽分配。"""
        # 增删子控件会触发 notify::width -> _on_resize -> _relayout，
        # 不加锁会无限递归。
        if getattr(self, "_in_relayout", False):
            return
        self._in_relayout = True
        try:
            self._relayout_inner()
        finally:
            self._in_relayout = False

    def _relayout_inner(self):
        visible = [c for c in self.cards if c.get_visible()]

        # 清空 Grid
        child = self.grid.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.grid.remove(child)
            child = nxt
        self._rows = []

        if not visible:
            return

        cols = self._cols()
        for i, card in enumerate(visible):
            card.set_hexpand(True)
            card.set_halign(Gtk.Align.FILL)
            card.set_valign(Gtk.Align.FILL)
            card.set_vexpand(True)
            self.grid.attach(card, i % cols, i // cols, 1, 1)
        self._rows = [None] * ((len(visible) + cols - 1) // cols)

    def _drain_thumbs(self):
        """逐个异步加载图片缩略图，一次事件循环处理一张。"""
        if not self._thumb_pending:
            return False
        card = self._thumb_pending.pop(0)
        try:
            entry = self.client.get(card.clip_id)
            if card.mime == "image/png":
                card.load_thumbnail(entry["data"])
            else:
                card.set_real_text(entry["data"].decode("utf-8", "replace"))
        except GLib.Error:
            pass
        return bool(self._thumb_pending)

    # ---------- 交互 ----------

    def _visible_cards(self):
        """当前可见的卡片，顺序与界面一致。"""
        return [c for c in self.cards if c.get_visible()]

    def _selected_card(self):
        cards = self._visible_cards()
        if not cards:
            return None
        idx = max(0, min(self.selected, len(cards) - 1))
        return cards[idx]

    def _set_selected(self, idx):
        """更新选中态：只改 CSS 类，不依赖 FlowBox 的 selection。"""
        cards = self._visible_cards()
        if not cards:
            self.selected = 0
            return
        self.selected = max(0, min(idx, len(cards) - 1))
        for i, card in enumerate(cards):
            if i == self.selected:
                card.add_css_class("selected-card")
            else:
                card.remove_css_class("selected-card")

    def _move_selection(self, delta):
        cards = self._visible_cards()
        if not cards:
            return
        cols = max(1, self._cols())
        self._set_selected(self.selected + delta)
        card = self._selected_card()
        if card is not None:
            # 让选中卡片进入可视区域
            adj = self.scroller.get_vadjustment()
            alloc = card.compute_bounds(self.grid)[1] if card.get_parent() else None
            if alloc is not None:
                top, height = alloc.origin.y, alloc.size.height
                if top < adj.get_value():
                    adj.set_value(top)
                elif top + height > adj.get_value() + adj.get_page_size():
                    adj.set_value(top + height - adj.get_page_size())

    def _on_search(self, entry):
        needle = entry.get_text().strip().lower()
        for card in self.cards:
            card.set_visible((not needle) or needle in card.preview.lower())
        self._relayout()
        matched = len(self._visible_cards())
        total = len(self.cards)
        if needle:
            self.count_label.set_text(f"{matched} / {total} 项")
        else:
            self.count_label.set_text(f"{total} 项" if total else "暂无记录")
        self._set_selected(0)

    def _activate(self, clip_id):
        try:
            self.client.mark(clip_id)
        except GLib.Error as err:
            print(f"mark failed: {err}", file=sys.stderr)
            return
        self.close()

    def _delete(self, clip_id):
        try:
            self.client.remove(clip_id)
        except GLib.Error:
            return
        self.reload()

    def _on_key(self, controller, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            # 搜索框里有内容时先清空，再按一次才关闭，
            # 免得误按 Esc 把整个面板关掉。
            if self.search.get_text():
                self.search.set_text("")
                return True
            self.close()
            return True
        if keyval == Gdk.KEY_Delete:
            card = self._selected_card()
            if card:
                self._delete(card.clip_id)
                return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            card = self._selected_card()
            if card:
                self._activate(card.clip_id)
                return True
        cols = max(1, self._cols())
        if keyval == Gdk.KEY_Left:
            self._move_selection(-1)
            return True
        if keyval == Gdk.KEY_Right:
            self._move_selection(1)
            return True
        if keyval == Gdk.KEY_Up:
            self._move_selection(-cols)
            return True
        if keyval == Gdk.KEY_Down:
            self._move_selection(cols)
            return True
        if keyval == Gdk.KEY_slash and not self.search.get_text():
            self.search.grab_focus()
            return True
        return False


class ClipcatWinUI(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.clipcat.WinUI",
                         flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.client = ClipcatClient()
        self.win = None

    def do_activate(self):
        # Win+V 行为：面板已经开着就再按一次收起，否则新建/刷新并前置。
        if self.win is not None and self.win.get_visible():
            self.win.close()
            self.win = None
            return
        # 关键顺序：先问光标，再显示窗口。
        # 反过来就会读到面板自己抢到焦点后的位置。
        caret = query_pointer_pos()

        if self.win is None:
            self.win = ClipboardWindow(self, self.client)
        else:
            self.win.reload()
            self.win.search.set_text("")
        self.win.pointer_snapshot = caret
        self.win.present()

    def do_shutdown(self):
        Adw.Application.do_shutdown(self)


def main():
    Adw.init()
    app = ClipcatWinUI()
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())

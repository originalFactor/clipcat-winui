# AGENTS.md

给在本仓库里工作的 agent 的说明。人类读者看 [README.md](README.md)。

## 这个项目是什么

一个 GTK4/Adw 剪贴板面板，作为 [clipcat](https://github.com/xrelkd/clipcat) 的前端，
外加一个 GNOME Shell 扩展给面板提供鼠标指针坐标。

**它的绝大部分复杂度不是业务逻辑，而是绕开平台限制。** 改动前先读
[README 的「为什么必须跑在 Xwayland 上」](README.md#为什么必须跑在-xwayland-上)。

## 动手前必须知道的三件事

这三条都是实测结论，违反任何一条都会引入难查的回归。

### 1. 定位用的是「目标坐标 = 内容坐标 − 37px(y)」

`xdotool windowmove` 的目标在 X 轴上就是内容区坐标，但 mutter 会按标题栏高度把
Y 整体下移 **37px**。所以：

```python
FRAME_OFFSET_Y = 37
_move(x, y - FRAME_OFFSET_Y)
```

**不要试图用「外框比内容高多少」去推导这个值。** 我试过：外框 503、内容 416，
差值 87，据此算出来的偏移是错的。只有 `target → 实际落点` 的实测映射是可信的。
换主题 / 换 GNOME 版本后要重新测，方法见下文「验证」。

### 2. 窗口必须保留系统边框

`set_decorated(False)` 会让 mutter 不再把窗口当受管窗口，`notify::is-active`
**完全不再触发**，失焦自动关闭随之失效。

以下替代方案全部实测不可行，不要再试：

| 方案 | 结果 |
|---|---|
| `_NET_ACTIVE_WINDOW` | 只返回 mutter 的 guard window（`0x600003`），看不到 Xwayland 窗口 |
| `Gtk.EventControllerFocus` | 只跟踪窗口内控件焦点，`leave` 从不触发 |
| `GdkSurface` 的 `notify::state` | 无变化通知 |
| `Gtk.EventControllerLegacy` | 回调的 event 参数是 `None` |
| `IBusManager` 的 `set-cursor-location` | 本机 `org.freedesktop.IBus.Panel` 无持有者，只有面板自己聚焦时才有数据 |

窗口边缘的**点击**也收不到（被合成器当窗口管理区吃掉，窗口级和容器级的
`GestureClick` 都不触发），所以不要试图自己做边缘 resize。

### 3. 拿不到文本插入符，只有鼠标指针

目标是像输入法候选窗那样贴在光标旁，但本机做不到：

- IBus 通道基本无数据（原因同上表）
- AT-SPI 的 `get_range_extents` 返回 `Unsupported coordinate space`
- `component.get_extents()` 对 GTK4 应用返回过期的 `(0,0)`

现在的方案是 `global.get_pointer()`（扩展里），通过 D-Bus 交给面板。
**不要**把它换回 caret 查询——那会重现「面板把挪到自己搜索框下面」的自指循环。

## 关键顺序：先取指针，再 present

```python
def do_activate(self):
    caret = query_pointer_pos()   # 必须在前
    ...
    self.win.pointer_snapshot = caret
    self.win.present()            # 必须在后
```

反过来，面板 `present()` 后焦点到搜索框，再取的坐标可能就是面板自身。
`_place_near_caret()` 读的是 `pointer_snapshot` 快照，不是实时查询。

## clipcat D-Bus 接口

- 对象路径 `/org/clipcat/clipcat/manager`（**不是** `/org/clipcat/clipcat`）
- 接口 `org.clipcat.clipcat.Manager`
- `List(t) -> a(tsuxs)`、`Get(t) -> (tayusx)`、`Mark(tu) -> b`、`Remove(t) -> b`

`Get` 的元组顺序是 **`(id, data, kind, mime, timestamp)`** —— `kind` 是 `u`，
排在 `mime` 的 `s` 前面。按 proto 字段顺序猜会解错。

`kind` 表示「最近一次被 mark 到哪种选区」，**不是固定属性**，同一个 clip 可能
先被 mark 成 clipboard 再被 mark 成 primary。不要按 `kind` 过滤。

## 一些容易踩的实现细节

- **`Gtk.Label` 默认不接收指针事件**，要 `set_can_target(True)` 才能加手势
- **`Gtk4` 的 `notify::width`/`height` 在 resize 时不触发**，用
  `notify::default-width`/`default-height`
- **`Gtk.Grid` + `column_homogeneous=True` + `row_homogeneous=False`** 才能得到
  等宽列 + 内容驱动行高（`FlowBox` 会给不等宽和多余行高）
- **`_relayout()` 有重入保护**（`_in_relayout`），去掉会因
  `notify::width → _on_resize → _relayout` 无限递归卡死
- **图片用 `Gtk.Picture`**（`set_content_fit(CONTAIN)`），`Gtk.Image` +
  `set_pixel_size` 只会显示固定小方块
- **`Gtk.Window` 上挂 `GestureClick` 收不到点击**，要挂到根容器上
- 用 `Gtk.DrawingArea.set_draw_func` 需要真的 `import cairo`，只
  `gi.require_version` 会报 `foreign struct converter`

## 验证

这台机器上 `xdotool` 的合成按键**到不了** GNOME Shell（Wayland 会话），
必须用 `ydotool` 走 uinput。几个要点：

```sh
# 启动 ydotoold（socket 路径要显式指定）
ydotoold -p /run/user/1000/.ydotool.sock &
export YDOTOOL_SOCKET=/run/user/1000/.ydotool.sock

# 左键是 0x00，不是 0xC0（0xC0 = 0x40|0x80，缺按钮号，点了没反应）
ydotool click 0x00

# Super+V
ydotool key 125:1 47:1 47:0 125:0

# 绝对定位必须先关鼠标加速，否则落点不准
gsettings set org.gnome.desktop.peripherals.mouse accel-profile flat
ydotool mousemove --absolute -x 500 -y 350
```

**收尾必做**：`flat` 会改掉用户真实的手感（线性无加速），不是无害的测试开关。

```sh
gsettings set org.gnome.desktop.peripherals.mouse accel-profile default
gsettings get org.gnome.desktop.peripherals.mouse accel-profile   # 必须是 'default'
```

这条容易漏：如果测试脚本是分多步跑的，后面的步骤会再把它设回 `flat`，
覆盖掉前面已经做过的恢复。**恢复只在最后一步做一次，并且要回读确认。**

验证定位：

```sh
# 指针坐标
gdbus call --session --dest org.gnome.Shell \
  --object-path /io/github/clipcat/CaretLocator \
  --method io.github.clipcat.CaretLocator.GetPointer

# 窗口实际落点（内容是 clipcat-winui.py 那个窗口，不是 mutter-x11-frames 外壳）
xwininfo -id <xid> | grep "Absolute upper-left"
```

期望：内容窗口左上角 == 指针位置 `+8, +8`。贴着屏幕边缘时应翻转。

截图方式（`grim` 在此环境不可用，没有 wlr-screencopy）：

```sh
xwd -id <xid> -out /tmp/x.xwd
/usr/bin/python3 tools/xwd2png.py /tmp/x.xwd out.png
```

`tools/xwd2png.py` 是手写的 XWD → PNG 转换器（`grim` 在本环境不可用）。

**改任何与焦点/定位相关的代码后，必须跑完整生命周期验证**：

1. `Super+V` 打开 → 窗口出现且位置正确
2. 面板内打字 → **不能**被误关
3. `Alt+Tab` → 应自动关闭

## 环境

- Ubuntu 26.04, GNOME Shell 50.1, mutter 18, Wayland + Xwayland
- 双显示器 2720×1024（主屏 1440×900 在左，副屏 1280×1024 在右）
- 系统 Python 在 `/usr/bin/python3`；**默认 `python3` 可能是 conda 的，没有 `gi`**
- 无 root（`sudo` 要交互密码）；依赖靠提取 `.deb` 到 `~/.local/bin`

## 改完自检

```sh
/usr/bin/python3 -m py_compile panel/clipcat-winui.py
sh -n install.sh
./install.sh        # 幂等，已存在的配置会跳过
```

安装脚本用占位符模板（`@BIN_DIR@` 等），不要在仓库里写死绝对路径。
面板代码里可以用 `$XDG_STATE_HOME`（见 `_state_path()`），不要写死 `~`。

## 不要做的事

- 不要去掉 `GDK_BACKEND=x11` —— 会彻底失去定位能力
- 不要提交时带上 `__pycache__/`（已在 `.gitignore`）
- 不要把实测常量（37、50、87）当成「显然正确的魔数」随手改小改大，
  改动前先按上文方法重新测
- 不要在未验证的情况下声称焦点/定位已修好 —— 这两块出问题的表现很隐蔽

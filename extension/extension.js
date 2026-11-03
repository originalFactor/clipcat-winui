import * as IBusManager from 'resource:///org/gnome/shell/misc/ibusManager.js';
import Gio from 'gi://Gio';

const IFACE = `
<node>
  <interface name="io.github.clipcat.CaretLocator">
    <method name="GetPointer">
      <arg type="(ii)" direction="out" name="pos"/>
    </method>
    <method name="GetCaretRect">
      <arg type="(iiii)" direction="out" name="rect"/>
    </method>
    <method name="GetDebug">
      <arg type="s" direction="out" name="info"/>
    </method>
  </interface>
</node>`;

const PANEL_WM_CLASS = 'clipcat-winui';

export default class CaretLocatorExtension {
    enable() {
        // 保留 caret 追踪（IBus 通道在本机基本无数据，留作次要来源）
        this._caret = {x: -1, y: -1, w: 0, h: 0};
        this._updates = 0;
        this._skipped = 0;

        try {
            const mgr = IBusManager.getIBusManager();
            this._sigId = mgr.connect('set-cursor-location', (manager, rect) => {
                if (this._focusIsPanel()) {
                    this._skipped++;
                    return;
                }
                this._caret = {
                    x: rect.x, y: rect.y,
                    w: rect.width, h: rect.height,
                };
                this._updates++;
            });
        } catch (e) {
            console.error('[CARETLOC] ibus connect: ' + e);
            this._sigId = 0;
        }

        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._dbus.export(Gio.DBus.session, '/io/github/clipcat/CaretLocator');
        console.log('[CARETLOC] enabled (pointer mode)');
    }

    _focusIsPanel() {
        try {
            const w = global.display.focus_window;
            if (!w)
                return false;
            return (w.get_wm_class() || '').toLowerCase().includes(PANEL_WM_CLASS);
        } catch (e) {
            return false;
        }
    }

    disable() {
        try {
            if (this._sigId)
                IBusManager.getIBusManager().disconnect(this._sigId);
        } catch (e) {
            console.error('[CARETLOC] disconnect: ' + e);
        }
        this._sigId = 0;
        if (this._dbus) {
            this._dbus.unexport();
            this._dbus = null;
        }
    }

    // 鼠标指针的真实屏幕坐标（合成器内部 API，始终准确）
    GetPointer() {
        const [px, py] = global.get_pointer();
        return [Math.round(px), Math.round(py)];
    }

    GetCaretRect() {
        const c = this._caret;
        if (c.x >= 0 && c.y >= 0)
            return [Math.round(c.x), Math.round(c.y), Math.round(c.w), Math.round(c.h)];
        return [-1, -1, 0, 0];
    }

    GetDebug() {
        const [px, py] = global.get_pointer();
        return `updates=${this._updates} skipped=${this._skipped} ` +
               `pointer=(${Math.round(px)},${Math.round(py)}) ` +
               `caret=${JSON.stringify(this._caret)}`;
    }
}

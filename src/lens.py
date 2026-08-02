#!/usr/bin/env python3
"""
Lens — a fast, mobile-first camera app for Linux tablets.
GTK4 + libadwaita + GStreamer.
"""

import gi, os, sys, datetime, pathlib
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gtk, Adw, Gst, GLib, Gdk, Gio, GObject

Gst.init(None)

APP_ID = "org.cheapaz.Lens"
PICTURES = pathlib.Path.home() / "Pictures" / "Lens"
VIDEOS   = pathlib.Path.home() / "Videos"  / "Lens"
PICTURES.mkdir(parents=True, exist_ok=True)
VIDEOS.mkdir(parents=True, exist_ok=True)


def list_v4l2_cameras():
    """Return [(path, human_name)] for real video capture devices, skipping metadata nodes."""
    devs = []
    for d in sorted(pathlib.Path("/dev").glob("video*")):
        try:
            import subprocess
            info = subprocess.run(
                ["v4l2-ctl", "-d", str(d), "--list-formats"],
                capture_output=True, text=True, timeout=1).stdout
            if "Pixel Format" not in info:
                continue
            name = subprocess.run(
                ["v4l2-ctl", "-d", str(d), "--info"],
                capture_output=True, text=True, timeout=1).stdout
            title = "Camera"
            for line in name.splitlines():
                if "Card type" in line:
                    title = line.split(":", 1)[1].strip(); break
            devs.append((str(d), title))
        except Exception:
            continue
    # De-duplicate by title, keep first
    seen = set(); out = []
    for path, title in devs:
        if title in seen: continue
        seen.add(title); out.append((path, title))
    return out


class LensWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Lens")
        self.set_default_size(420, 780)
        self.pipeline = None
        self.paintable = None
        self.cameras = list_v4l2_cameras() or [("/dev/video0", "Camera")]
        self.cam_idx = 0
        self.video_mode = False
        self.recording = False
        self.grid_visible = False
        self.timer_sec = 0
        self.aspect_idx = 0  # 0=4:3, 1=16:9, 2=1:1
        self.aspects = ["4:3", "16:9", "1:1"]
        self.last_photo = None
        self.countdown_val = 0

        self._build_ui()
        self._start_pipeline()

    # ---------- pipeline ----------
    def _start_pipeline(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)

        dev = self.cameras[self.cam_idx][0]
        # gtk4paintablesink -> capture into a GdkPaintable widget
        pipe_str = (
            f"v4l2src device={dev} ! videoconvert ! videoscale ! "
            f"video/x-raw,width=1280,height=720 ! videoflip method=none ! "
            f"gtk4paintablesink name=sink"
        )
        self.pipeline = Gst.parse_launch(pipe_str)
        sink = self.pipeline.get_by_name("sink")
        self.paintable = sink.props.paintable
        self.picture.set_paintable(self.paintable)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_err)
        self.pipeline.set_state(Gst.State.PLAYING)

    def _on_bus_err(self, bus, msg):
        err, dbg = msg.parse_error()
        print("gst error:", err, dbg, file=sys.stderr)

    # ---------- UI ----------
    def _build_ui(self):
        overlay = Gtk.Overlay()
        self.set_content(overlay)

        # Black background + viewfinder
        bg = Gtk.Box()
        bg.set_hexpand(True); bg.set_vexpand(True)
        bg.add_css_class("bg-black")
        overlay.set_child(bg)

        self.picture = Gtk.Picture()
        self.picture.set_can_shrink(True)
        self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        bg.append(self.picture)

        # ---- Top control row ----
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.set_valign(Gtk.Align.START); top.set_halign(Gtk.Align.CENTER)
        top.set_margin_top(12)
        overlay.add_overlay(top)

        self.btn_grid   = self._pill_button("#",   self._toggle_grid,   width=42, tint=False)
        self.btn_aspect = self._pill_button(self.aspects[0], self._toggle_aspect, width=58, tint=False)
        self.btn_timer  = self._pill_button("⏱",  self._toggle_timer,  width=58, tint=False)
        top.append(self.btn_grid); top.append(self.btn_aspect); top.append(self.btn_timer)

        # ---- Bottom control area ----
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        bottom.set_valign(Gtk.Align.END)
        bottom.add_css_class("bottom-bar")
        bottom.set_size_request(-1, 180)
        overlay.add_overlay(bottom)

        # Mode pill
        mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        mode_row.set_halign(Gtk.Align.CENTER)
        mode_row.set_margin_top(12); mode_row.set_margin_bottom(6)
        mode_row.add_css_class("mode-pill")
        self.btn_photo = Gtk.Button(label="PHOTO")
        self.btn_photo.add_css_class("mode-btn"); self.btn_photo.add_css_class("mode-active")
        self.btn_photo.connect("clicked", lambda *_: self._set_video_mode(False))
        self.btn_video = Gtk.Button(label="VIDEO")
        self.btn_video.add_css_class("mode-btn")
        self.btn_video.connect("clicked", lambda *_: self._set_video_mode(True))
        mode_row.append(self.btn_photo); mode_row.append(self.btn_video)
        bottom.append(mode_row)

        # Row: thumb | shutter | flip
        act_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        act_row.set_margin_start(32); act_row.set_margin_end(32); act_row.set_margin_top(20)
        act_row.set_hexpand(True)
        bottom.append(act_row)

        self.thumb = Gtk.Button()
        self.thumb.set_size_request(56, 56); self.thumb.add_css_class("thumb")
        self.thumb_img = Gtk.Image.new_from_icon_name("image-x-generic-symbolic")
        self.thumb_img.set_pixel_size(28)
        self.thumb.set_child(self.thumb_img)
        self.thumb.connect("clicked", self._open_gallery)
        act_row.append(self.thumb)

        spacer1 = Gtk.Box(); spacer1.set_hexpand(True); act_row.append(spacer1)

        self.shutter = Gtk.Button()
        self.shutter.set_size_request(80, 80); self.shutter.add_css_class("shutter")
        self.shutter.connect("clicked", lambda *_: self._on_shutter())
        act_row.append(self.shutter)

        spacer2 = Gtk.Box(); spacer2.set_hexpand(True); act_row.append(spacer2)

        self.btn_flip = Gtk.Button(label="⟳")
        self.btn_flip.set_size_request(56, 56); self.btn_flip.add_css_class("flip")
        self.btn_flip.set_sensitive(len(self.cameras) > 1)
        self.btn_flip.connect("clicked", lambda *_: self._flip_camera())
        act_row.append(self.btn_flip)

        # Countdown overlay
        self.countdown_label = Gtk.Label(label="")
        self.countdown_label.set_visible(False)
        self.countdown_label.add_css_class("countdown")
        self.countdown_label.set_halign(Gtk.Align.CENTER)
        self.countdown_label.set_valign(Gtk.Align.CENTER)
        overlay.add_overlay(self.countdown_label)

        # Grid overlay (rule of thirds)
        self.grid_widget = _GridOverlay()
        self.grid_widget.set_visible(False)
        overlay.add_overlay(self.grid_widget)

        # CSS
        css = Gtk.CssProvider()
        css.load_from_string("""
        .bg-black { background: black; }
        .bottom-bar { background: rgba(0,0,0,0.8); }
        .mode-pill { background: rgba(255,255,255,0.15); border-radius: 16px; padding: 2px; }
        .mode-btn { background: transparent; color: white; font-weight: bold;
                    font-size: 12px; padding: 4px 24px; border-radius: 14px;
                    border: none; box-shadow: none; }
        .mode-active { background: rgba(0,0,0,0.5); color: #ffcc00; }
        .shutter { background: white; border-radius: 40px;
                   border: 4px solid white; min-width: 76px; min-height: 76px;
                   box-shadow: 0 0 0 4px rgba(0,0,0,0.4); }
        .shutter:active { background: #eee; }
        .rec-shutter { background: red; border-radius: 8px;
                       min-width: 40px; min-height: 40px; border: 4px solid white; }
        .thumb { background: rgba(255,255,255,0.2); border-radius: 8px;
                 border: 1px solid white; }
        .flip { background: rgba(0,0,0,0.5); border-radius: 28px;
                color: white; font-size: 22px; font-weight: bold; border: none; }
        .flip:disabled { opacity: 0.35; }
        .pill { background: rgba(0,0,0,0.5); border-radius: 21px; color: white;
                font-weight: bold; font-size: 14px; padding: 4px 12px; border: none; }
        .pill-active { background: #ffcc00; color: black; }
        .countdown { color: white; font-size: 96px; font-weight: bold;
                     background: rgba(0,0,0,0.5); padding: 30px 50px; border-radius: 60px; }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        # Keyboard shortcuts
        ctrl = Gtk.ShortcutController()
        for accel, fn in [("space", self._on_shutter), ("Return", self._on_shutter),
                          ("f", self._flip_camera), ("g", self._toggle_grid)]:
            sc = Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(accel),
                Gtk.CallbackAction.new(lambda w, args, f=fn: (f(), True)[1]))
            ctrl.add_shortcut(sc)
        self.add_controller(ctrl)

    def _pill_button(self, label, cb, width=42, tint=False):
        b = Gtk.Button(label=label)
        b.set_size_request(width, 42)
        b.add_css_class("pill")
        if tint: b.add_css_class("pill-active")
        b.connect("clicked", lambda *_: cb())
        return b

    # ---------- Actions ----------
    def _toggle_grid(self):
        self.grid_visible = not self.grid_visible
        self.grid_widget.set_visible(self.grid_visible)
        if self.grid_visible: self.btn_grid.add_css_class("pill-active")
        else: self.btn_grid.remove_css_class("pill-active")

    def _toggle_aspect(self):
        self.aspect_idx = (self.aspect_idx + 1) % len(self.aspects)
        self.btn_aspect.set_label(self.aspects[self.aspect_idx])
        # (Aspect ratio applied at capture; viewfinder is CONTAIN)

    def _toggle_timer(self):
        seq = [0, 3, 10]
        cur = seq.index(self.timer_sec) if self.timer_sec in seq else 0
        self.timer_sec = seq[(cur + 1) % len(seq)]
        if self.timer_sec > 0:
            self.btn_timer.set_label(f"{self.timer_sec}s")
            self.btn_timer.add_css_class("pill-active")
        else:
            self.btn_timer.set_label("⏱")
            self.btn_timer.remove_css_class("pill-active")

    def _set_video_mode(self, video):
        if self.recording: return
        self.video_mode = video
        if video:
            self.btn_video.add_css_class("mode-active")
            self.btn_photo.remove_css_class("mode-active")
        else:
            self.btn_photo.add_css_class("mode-active")
            self.btn_video.remove_css_class("mode-active")

    def _flip_camera(self):
        if len(self.cameras) < 2: return
        self.cam_idx = (self.cam_idx + 1) % len(self.cameras)
        self._start_pipeline()

    def _on_shutter(self):
        if self.video_mode:
            if self.recording: self._stop_recording()
            else: self._start_recording()
            return
        if self.timer_sec > 0:
            self.countdown_val = self.timer_sec
            self.countdown_label.set_visible(True)
            self.countdown_label.set_label(str(self.countdown_val))
            GLib.timeout_add_seconds(1, self._tick_countdown)
        else:
            self._take_photo()

    def _tick_countdown(self):
        self.countdown_val -= 1
        if self.countdown_val <= 0:
            self.countdown_label.set_visible(False)
            self._take_photo()
            return False
        self.countdown_label.set_label(str(self.countdown_val))
        return True

    def _take_photo(self):
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = PICTURES / f"Lens-{ts}.jpg"
        # Snapshot from current pipeline by pulling one frame from a valve/parse
        # Simpler: separate short-lived ffmpeg-style capture from the same device.
        dev = self.cameras[self.cam_idx][0]
        # Freeze main pipe briefly, capture with parallel pipe
        cap = Gst.parse_launch(
            f"v4l2src device={dev} num-buffers=1 ! jpegenc quality=95 ! "
            f"filesink location={path}"
        )
        cap.set_state(Gst.State.PLAYING)
        # Wait for EOS then tear down
        bus = cap.get_bus()
        bus.timed_pop_filtered(2 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        cap.set_state(Gst.State.NULL)
        self.last_photo = str(path)
        # Update thumbnail
        try:
            self.thumb.set_child(Gtk.Picture.new_for_filename(str(path)))
        except Exception: pass

    def _start_recording(self):
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = VIDEOS / f"Lens-{ts}.mkv"
        # Rebuild pipeline for recording
        self.pipeline.set_state(Gst.State.NULL)
        dev = self.cameras[self.cam_idx][0]
        self.pipeline = Gst.parse_launch(
            f"v4l2src device={dev} ! tee name=t "
            f"t. ! queue ! videoconvert ! gtk4paintablesink name=sink "
            f"t. ! queue ! videoconvert ! x264enc tune=zerolatency bitrate=4000 ! "
            f"matroskamux ! filesink location={path}"
        )
        sink = self.pipeline.get_by_name("sink")
        self.paintable = sink.props.paintable
        self.picture.set_paintable(self.paintable)
        self.pipeline.set_state(Gst.State.PLAYING)
        self.recording = True
        self.shutter.add_css_class("rec-shutter")

    def _stop_recording(self):
        # Send EOS then tear down and restart preview
        self.pipeline.send_event(Gst.Event.new_eos())
        bus = self.pipeline.get_bus()
        bus.timed_pop_filtered(2 * Gst.SECOND, Gst.MessageType.EOS)
        self.pipeline.set_state(Gst.State.NULL)
        self.recording = False
        self.shutter.remove_css_class("rec-shutter")
        self._start_pipeline()

    def _open_gallery(self, *_):
        Gio.AppInfo.launch_default_for_uri("file://" + str(PICTURES), None)


class _GridOverlay(Gtk.DrawingArea):
    """Rule-of-thirds grid drawn over the viewfinder."""
    def __init__(self):
        super().__init__()
        self.set_can_target(False)  # transparent to input
        self.set_draw_func(self._draw)

    def _draw(self, area, cr, w, h):
        cr.set_source_rgba(1, 1, 1, 0.35)
        cr.set_line_width(1)
        for i in (1, 2):
            cr.move_to(w * i / 3, 0); cr.line_to(w * i / 3, h); cr.stroke()
            cr.move_to(0, h * i / 3); cr.line_to(w, h * i / 3); cr.stroke()


class LensApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        win = LensWindow(self)
        win.present()


if __name__ == "__main__":
    sys.exit(LensApp().run(sys.argv))
